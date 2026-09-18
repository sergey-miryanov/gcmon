"""Tests for reconstructing what a poll could not observe.

Two kinds of input here. The synthetic runs below carry a cumulative
``duration`` the way a real target does, so they can check the arithmetic
against ground truth: build a full run, show the monitor only what survives a
ring of a given size, and compare what it reconstructs to what actually
happened. The capture fixture from ``test_monitor_cursor`` carries no
durations, so it checks gap counts against real slot data instead.

Everything goes through ``EventsMonitor._ingest`` itself rather than a mirror
of it, so nothing here can pass against a copy of the logic that has drifted
from the one that ships.
"""

from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from itertools import pairwise
from typing import override

import msgspec.structs
import pytest

from gcmon.exporters.exporter import EventsExporter
from gcmon.model.data import GCStatsInfo
from gcmon.model.loss import RingAccumulator
from gcmon.model.process import Process
from gcmon.model.protocol import TGCStatsInfo, TGenLoss, TInstantMsg, TLossMsg
from gcmon.monitoring.monitor import EventsMonitor
from gcmon.monitoring.target_process import ExternalProcess
from gcmon.monitoring.wait_policy import no_wait_policy
from gcmon.stats.streaming_stats import StreamingStats
from tests.helpers import (
    SPACING_NS,
    TS0,
    FakeEventsReader,
    build_run,
    create_mock_stats_item,
    polled,
    proc,
    true_pause_ns,
    varied_pause,
)
from tests.monitoring.captured_polls import POLL_0, POLL_1, build_batch

PID = 12345

# How long after the newest record it returned a poll is taken to have run,
# when a test does not say. A read happens after the records it finds, and
# before the ones it does not.
POLL_LAG_NS = 1_000


def ring_polls(events: Sequence[GCStatsInfo], capacity: int, per_tick: int) -> Iterator[Sequence[GCStatsInfo]]:
    """What a monitor reads: at each tick, the newest *capacity* records done so far."""
    for end in range(per_tick, len(events) + 1, per_tick):
        yield events[max(0, end - capacity) : end]


RING_CAPACITY = {0: 11, 1: 3, 2: 3}  # GC_YOUNG_STATS_SIZE, then GC_OLD_STATS_SIZE twice

# An older generation walks more of the heap. `varied_pause` gives gen 0 the
# ~100-180 us the capture measured, so gen 1 pauses for about a millisecond and
# a full collection for tens of them, which is the shape that matters here: an
# interval bracketing an observed gen-2 collection is mostly that collection,
# leaving the lost records far less room than the interval is wide.
PAUSE_SCALE = {0: 1, 1: 8, 2: 300}


def build_interleaved_run(count: int, iid: int = 0, gap_ns: int = 900_000, ts0: int = TS0) -> list[GCStatsInfo]:
    """Every collection one interpreter performs, in the order they ran.

    CPython serializes collections within an interpreter, so the three
    generations' records interleave without ever overlapping in time. Each
    generation carries its own ``collections`` counter and its own cumulative
    ``duration``, runs a tenth as often as the one below it, and pauses for
    longer by ``PAUSE_SCALE``.
    """
    events: list[GCStatsInfo] = []
    counters = {0: 0, 1: 0, 2: 0}
    cumulative = {0: 0, 1: 0, 2: 0}
    ts = ts0

    for nth in range(count):
        gen = 2 if nth % 100 == 99 else 1 if nth % 10 == 9 else 0
        counters[gen] += 1
        pause = varied_pause(counters[gen]) * PAUSE_SCALE[gen]
        cumulative[gen] += pause
        events.append(
            create_mock_stats_item(
                gen=gen,
                iid=iid,
                collections=counters[gen],
                ts_start=ts,
                ts_stop=ts + pause,
                duration=cumulative[gen] / 1e9,
            )
        )
        ts += pause + gap_ns

    return events


def interpreter_polls(
    events: Sequence[GCStatsInfo], per_tick: int, capacity: Mapping[int, int] = RING_CAPACITY
) -> Iterator[Sequence[GCStatsInfo]]:
    """What one bulk read returns: every generation's ring at once.

    Each ring holds the newest *capacity* records of its own generation to
    have finished by that tick, so the generations lose records over stretches
    that cross, which ``ring_polls`` on a single-ring run never produces.
    """
    for end in range(per_tick, len(events) + per_tick, per_tick):
        done = events[:end]
        batch: list[GCStatsInfo] = []
        for gen, slots in capacity.items():
            batch.extend([event for event in done if event.gen == gen][-slots:])
        yield batch


class LossRecorder(EventsExporter):
    """Every record `_ingest` handed an exporter, in emission order."""

    def __init__(self) -> None:
        super().__init__()
        self.losses: list[TLossMsg] = []
        self.observed: list[TGCStatsInfo] = []

    @override
    def add_event(self, process: Process, item: TGCStatsInfo) -> None:
        self.observed.append(item)

    @override
    def add_loss_event(self, process: Process, item: TLossMsg) -> None:
        self.losses.append(item)

    @override
    def add_instant_event(self, process: Process, item: TInstantMsg) -> None:
        pass

    @override
    def close(self) -> None:
        pass


class Ingested:
    """A monitor, the polls it was given, and what it made of them.

    Thin on purpose: the loss records and the rings are the monitor's own,
    so a test reads what the exporters would be handed rather than what a copy
    of `_ingest` would have produced.
    """

    def __init__(self, pid: int = PID) -> None:
        self.pid = pid
        self.recorder = LossRecorder()
        self.stats = StreamingStats()
        self.monitor = EventsMonitor(
            ExternalProcess(pid=pid),
            self.recorder,
            self.stats,
            reader=FakeEventsReader(),
            wait_policy_factory=no_wait_policy,
        )
        self.polled_at: list[int] = []

    def poll(self, batch: Sequence[GCStatsInfo], ts: int | None = None) -> list[TLossMsg]:
        """Fold one whole ring buffer at *ts*; return the records it emitted.

        *ts* defaults to just past the newest record in the batch, which is
        where a read that returned those records has to have happened.
        """
        if ts is None:
            newest = max((event.ts_stop for event in batch), default=0)
            ts = max(newest + POLL_LAG_NS, (self.polled_at[-1] if self.polled_at else 0) + 1)
        self.polled_at.append(ts)

        before = len(self.recorder.losses)
        self.monitor._ingest(polled(self.monitor, self.pid), list(batch), ts)
        return self.recorder.losses[before:]

    @property
    def rings(self) -> dict[tuple[int, int], RingAccumulator]:
        state = self.monitor._pids.get(self.pid)
        return state.rings if state is not None else {}

    def __getitem__(self, key: tuple[int, int]) -> RingAccumulator:
        return self.rings[key]

    def spans(self, iid: int = 0) -> list[TLossMsg]:
        """Every loss record this interpreter's polls emitted, in order."""
        return [loss for loss in self.recorder.losses if loss.iid == iid]

    def gaps_for(self, key: tuple[int, int]) -> list[TGenLoss]:
        """What one ring lost, one entry per interval it lost anything in."""
        iid, gen = key
        return [entry for loss in self.spans(iid) for entry in loss.gens if entry.gen == gen and entry.lost_count]

    def observed_for(self, key: tuple[int, int]) -> list[int]:
        """The counters of the records that reached the exporter on this key."""
        iid, gen = key
        return [event.collections for event in self.recorder.observed if (event.iid, event.gen) == (iid, gen)]


def observe_all(batches: Iterable[Sequence[GCStatsInfo]]) -> Ingested:
    ingested = Ingested()
    for batch in batches:
        ingested.poll(batch)
    return ingested


@pytest.fixture
def captured() -> Ingested:
    """The verbatim two-poll capture, ingested the way the monitor would."""
    return observe_all([build_batch(POLL_0), build_batch(POLL_1)])


class TestReconstructionAgainstGroundTruth:
    """Show the monitor a lossy view; compare what it reports to what happened."""

    @pytest.mark.parametrize(
        ("capacity", "per_tick"),
        [
            (11, 87),  # gen 0, as measured
            (3, 8),  # gen 1, as measured
            (1, 5),  # free-threaded: one slot per generation
            (11, 11),  # exactly keeping up
            (11, 3),  # polling faster than the target collects
        ],
    )
    def test_counts_and_pause_sums_are_exact(self, capacity: int, per_tick: int) -> None:
        events = build_run(400)

        acc = observe_all(ring_polls(events, capacity, per_tick))[(0, 0)]

        assert acc.exact_count == acc.last_collections - acc.first_collections + 1
        assert acc.exact_pause_ns == true_pause_ns(events, acc.first_collections, acc.last_collections)

    @pytest.mark.parametrize(("capacity", "per_tick"), [(11, 87), (3, 8), (1, 5), (11, 11)])
    def test_the_invariant_holds(self, capacity: int, per_tick: int) -> None:
        """Exact pause time is what gcmon saw plus what every gap says it
        missed. This is the one assertion that catches a fencepost error, a
        clock mismatch between ``duration`` and the timestamps, and a wrong
        gap in a single check."""
        ingested = observe_all(ring_polls(build_run(400), capacity, per_tick))
        acc = ingested[(0, 0)]

        lost = sum(gap.lost_pause_ns for gap in ingested.gaps_for((0, 0)))
        assert acc.exact_pause_ns == acc.sampled_pause_ns + lost

    def test_coverage_approaches_the_ring_ratio(self) -> None:
        """11 slots against 87 collections per tick keeps 11 of every 87, once
        the run is long enough to drown the first tick. That one is narrower:
        its span starts at the oldest slot still in the ring, so the 76
        records lost before gcmon ever looked fall outside the span."""
        acc = observe_all(ring_polls(build_run(8_700), 11, 87))[(0, 0)]

        assert acc.sampled_count / acc.exact_count == pytest.approx(11 / 87, rel=0.02)

    def test_lost_count_matches_the_gaps(self) -> None:
        ingested = observe_all(ring_polls(build_run(400), 11, 87))
        acc = ingested[(0, 0)]

        assert acc.exact_count - acc.sampled_count == sum(gap.lost_count for gap in ingested.gaps_for((0, 0)))


# (gap between collections, collections per tick). The first pace is the
# capture's, gen 0 collecting every ~1 ms against a 100 ms tick. The last two
# are a GC-bound target, two thirds of its wall time inside gen 0, which is
# what it takes for a blind interval to be nearly full of pause.
PACES = [(900_000, 40), (900_000, 87), (900_000, 400), (80_000, 87), (80_000, 120)]


class TestOneSpanPerPollInterval:
    """A poll draws one bar, however many generations went blind under it.

    The generations went blind together, over the interval between two reads.
    Anything narrower would be a claim about where inside it the lost records
    ran, and nothing gcmon holds says that.
    """

    @pytest.mark.parametrize(("gap_ns", "per_tick"), PACES)
    def test_a_poll_emits_at_most_one_record(self, gap_ns: int, per_tick: int) -> None:
        ingested = Ingested()

        emitted = [
            ingested.poll(batch) for batch in interpreter_polls(build_interleaved_run(2_000, gap_ns=gap_ns), per_tick)
        ]

        assert any(emitted)
        assert all(len(records) <= 1 for records in emitted)

    @pytest.mark.parametrize(("gap_ns", "per_tick"), PACES)
    def test_the_spans_are_disjoint_and_in_order(self, gap_ns: int, per_tick: int) -> None:
        """They share a row, and a row is a stack. Consecutive intervals meet
        at a poll instant and never overlap, so nothing has to be sorted into
        nesting order and nothing can cross."""
        spans = observe_all(interpreter_polls(build_interleaved_run(2_000, gap_ns=gap_ns), per_tick)).spans()

        assert spans
        for earlier, later in pairwise(spans):
            assert earlier.ts_stop <= later.ts_start

    @pytest.mark.parametrize(("gap_ns", "per_tick"), PACES)
    def test_every_span_has_an_interval_in_it(self, gap_ns: int, per_tick: int) -> None:
        """Two reads happen at two instants, so a span always has room for the
        records it says went missing. The old per-generation bounds could
        arrive reversed and had to be held back."""
        spans = observe_all(interpreter_polls(build_interleaved_run(2_000, gap_ns=gap_ns), per_tick)).spans()

        assert spans
        assert all(span.ts_start < span.ts_stop for span in spans)

    @pytest.mark.parametrize(("gap_ns", "per_tick"), PACES)
    def test_no_span_overstates_its_pause(self, gap_ns: int, per_tick: int) -> None:
        """A bar cannot hold more GC than the interval it covers. Collections
        in an interpreter are serialized and every lost record ran between the
        two reads, so the whole interval's loss fits inside it however many
        generations contributed."""
        spans = observe_all(interpreter_polls(build_interleaved_run(2_000, gap_ns=gap_ns), per_tick)).spans()

        assert spans
        for span in spans:
            assert sum(entry.lost_pause_ns for entry in span.gens) <= span.ts_stop - span.ts_start

    @pytest.mark.parametrize(("gap_ns", "per_tick"), PACES)
    def test_no_span_is_drawn_reporting_nothing(self, gap_ns: int, per_tick: int) -> None:
        """Every bar on the track stands for records the counters say went
        missing. A span carrying none claims a stretch was blind while saying
        gcmon has no evidence anything happened in it."""
        spans = observe_all(interpreter_polls(build_interleaved_run(2_000, gap_ns=gap_ns), per_tick)).spans()

        assert spans
        assert all(sum(entry.lost_count for entry in span.gens) > 0 for span in spans)

    def test_a_span_carries_every_generation_that_went_blind_at_once(self) -> None:
        """Two rings wrapping under one poll, and the whole point of the merge:
        one bar, two entries, rather than two bars whose differing widths read
        as two events at two times."""
        spans = observe_all(interpreter_polls(build_interleaved_run(2_000), 87)).spans()

        blind = [{entry.gen for entry in span.gens if entry.lost_count} for span in spans]
        assert any(len(gens) > 1 for gens in blind)

    def test_a_quiet_generation_still_gets_an_entry(self) -> None:
        """What makes the interval's coverage checkable: a generation that
        collected without losing anything is named, saying how much of the
        interval gcmon did see."""
        spans = observe_all(interpreter_polls(build_interleaved_run(2_000), 87)).spans()

        quiet = [entry for span in spans for entry in span.gens if not entry.lost_count]
        assert quiet
        assert all(entry.observed_count > 0 for entry in quiet)


class TestTheIntervalIsTheOneBetweenTwoPolls:
    """Where a span's edges come from, stated directly.

    Not from the records: a bound taken off the last record seen sits earlier
    than the read that saw it, and a bound taken off the first record after
    the gap sits inside the interval that lost it.
    """

    def test_the_span_runs_from_the_previous_poll_to_this_one(self) -> None:
        events = build_run(6)

        ingested = Ingested()
        ingested.poll([events[0]], ts=5_000_000_000)

        emitted = ingested.poll([events[4]], ts=6_000_000_000)

        assert [(loss.ts_start, loss.ts_stop) for loss in emitted] == [(5_000_000_000, 6_000_000_000)]

    def test_consecutive_intervals_tile(self) -> None:
        """Each poll is one span's right edge and the next span's left edge, so
        the intervals abut exactly rather than overlapping by a read."""
        events = build_run(12)

        ingested = Ingested()
        ingested.poll([events[0]], ts=1_000)
        ingested.poll([events[4]], ts=2_000)
        ingested.poll([events[8]], ts=3_000)

        assert [(loss.ts_start, loss.ts_stop) for loss in ingested.spans()] == [(1_000, 2_000), (2_000, 3_000)]

    def test_the_first_poll_bounds_nothing(self) -> None:
        """One read is not an interval. A key seeds on its first run and
        seeding opens no gap, so nothing reaches the exporter either way."""
        ingested = Ingested()

        assert ingested.poll(build_run(30)[10:20]) == []

    def test_the_edges_do_not_move_with_the_records(self) -> None:
        """The same loss, with the bounding records shifted in time. The span
        is where the reads were, so it does not follow them."""
        near = Ingested()
        near.poll([build_run(6)[0]], ts=1_000)
        near.poll([build_run(6)[4]], ts=2_000)

        far = Ingested()
        far.poll([build_run(6, ts0=TS0 + 50_000_000)[0]], ts=1_000)
        far.poll([build_run(6, ts0=TS0 + 50_000_000)[4]], ts=2_000)

        assert [(m.ts_start, m.ts_stop) for m in near.spans()] == [(m.ts_start, m.ts_stop) for m in far.spans()]


class TestAQuietGeneration:
    """A generation that sits out several polls and then loses records is
    blind for one interval, not for every tick it was quiet.

    The polls it sat out each read its ring and found the counter unchanged,
    which proves it lost nothing up to then. The span says so by starting at
    the last of those reads.
    """

    def polls(self) -> Ingested:
        gen0 = build_run(9, gen=0)
        # Collects once at the start, then not again until well after the
        # second poll, by which point three of its records are already gone.
        gen2 = build_run(7, gen=2, spacing_ns=3 * SPACING_NS)

        ingested = Ingested()
        ingested.poll([*gen0[0:3], gen2[0]], ts=1_000)
        ingested.poll([*gen0[3:6], gen2[0]], ts=2_000)
        ingested.poll([*gen0[6:9], *gen2[4:7]], ts=3_000)
        return ingested

    def test_the_span_covers_the_last_interval_only(self) -> None:
        spans = self.polls().spans()

        assert [(loss.ts_start, loss.ts_stop) for loss in spans] == [(2_000, 3_000)]

    def test_it_names_the_records_that_generation_lost(self) -> None:
        gaps = self.polls().gaps_for((0, 2))

        assert [gap.lost_count for gap in gaps] == [3]

    def test_an_unchanged_counter_opens_nothing(self) -> None:
        assert len(self.polls().gaps_for((0, 2))) == 1


class TestASpanCoveringAnObservedCollection:
    """A span reaches over collections gcmon did observe, including its own
    interpreter's, and that is the shape being kept rather than a defect.

    The interval is the claim: between these two reads, records were lost.
    Trimming it around the collections that were seen would narrow the bar to
    somewhere the missing records might not be, on evidence that says nothing
    about where they ran. The args carry how much of the interval gcmon saw,
    which is the honest version of the same information.
    """

    def polls(self) -> tuple[Ingested, list[GCStatsInfo], GCStatsInfo]:
        gen0 = build_run(5, gen=0, spacing_ns=44_000_000)
        gen1 = build_run(1, gen=1, ts0=TS0 + 66_000_000)[0]

        ingested = Ingested()
        ingested.poll([gen0[0]])
        ingested.poll([gen1, gen0[3], gen0[4]])
        return ingested, gen0, gen1

    def test_the_span_covers_the_observed_collection(self) -> None:
        ingested, _gen0, gen1 = self.polls()

        span = ingested.spans()[0]

        assert span.ts_start < gen1.ts_start and gen1.ts_stop < span.ts_stop

    def test_the_observed_collection_is_counted_on_the_span(self) -> None:
        """Which is what keeps the wide bar readable: it says two collections
        went missing out of the three that ran in there."""
        ingested, _gen0, _gen1 = self.polls()

        span = ingested.spans()[0]

        assert {entry.gen: (entry.observed_count, entry.lost_count) for entry in span.gens} == {
            0: (2, 2),
            1: (1, 0),
        }

    def test_the_span_holds_the_pause_it_reports(self) -> None:
        ingested, _gen0, _gen1 = self.polls()

        span = ingested.spans()[0]

        assert sum(entry.lost_pause_ns for entry in span.gens) <= span.ts_stop - span.ts_start


def charges(ingested: Ingested, key: tuple[int, int]) -> Counter[int]:
    """Every collection the trace claims on one ring, and how often.

    A reader adding a ring up has two sources: the ``GC Pause`` slices, one
    per record that reached the exporter, and the ``GC Loss`` spans, each
    naming a range of counters per generation. Charging a counter to both, or
    to two spans, double-counts it; charging it to neither loses it with
    nothing on screen to say so.
    """
    charged: Counter[int] = Counter(ingested.observed_for(key))
    for gap in ingested.gaps_for(key):
        charged.update(range(gap.lost_from, gap.lost_from + gap.lost_count))
    return charged


class TestTheRingSpanIsPartitioned:
    """Every collection between the first and last gcmon observed on a ring is
    either drawn as a ``GC Pause`` slice or inside exactly one loss span's
    range for that generation. No collection twice, none unaccounted for.

    This is what ``lost_from`` buys, and it is the strongest statement
    available about the loss arithmetic: the counts alone can only be checked
    against themselves, whereas a partition is checked against the collections
    the target actually performed. A fencepost anywhere (the near fence of a
    range, its far fence, the count it was cut to) shows up here as an
    overlap or a hole, and nothing else in the suite would notice.
    """

    @pytest.mark.parametrize(("gap_ns", "per_tick"), PACES)
    def test_every_collection_is_accounted_for_exactly_once(self, gap_ns: int, per_tick: int) -> None:
        run = build_interleaved_run(2_000, gap_ns=gap_ns)

        ingested = observe_all(interpreter_polls(run, per_tick))

        for (iid, gen), acc in ingested.rings.items():
            # Ground truth: the collections the target really performed on this
            # ring, over the stretch gcmon can speak for. What ran before the
            # first observed record is outside the span, since nothing tells
            # "ran before we attached" from "lost".
            truth = {
                e.collections
                for e in run
                if e.gen == gen and acc.first_collections <= e.collections <= acc.last_collections
            }
            charged = charges(ingested, (iid, gen))

            assert truth
            assert set(charged) == truth, f"gen {gen} charges {set(charged) ^ truth} it should not"
            assert [c for c in truth if charged[c] != 1] == [], f"gen {gen} charges a collection twice"

    @pytest.mark.parametrize(("gap_ns", "per_tick"), PACES)
    def test_the_partition_has_loss_in_it_to_get_wrong(self, gap_ns: int, per_tick: int) -> None:
        """Otherwise the check above would pass on a run that lost nothing,
        where the observed records partition the span on their own and no
        range is exercised at all."""
        run = build_interleaved_run(2_000, gap_ns=gap_ns)

        ingested = observe_all(interpreter_polls(run, per_tick))

        assert any(ingested.gaps_for(key) for key in ingested.rings)
        for key, acc in ingested.rings.items():
            assert sum(gap.lost_count for gap in ingested.gaps_for(key)) == acc.exact_count - acc.sampled_count

    @pytest.mark.parametrize(("gap_ns", "per_tick"), PACES)
    def test_each_range_abuts_the_records_that_bound_it(self, gap_ns: int, per_tick: int) -> None:
        """The fences, stated directly rather than as a consequence. A gap
        opens one counter past the last record gcmon saw before it and closes
        one short of the first it saw after, and both of those are drawn."""
        run = build_interleaved_run(2_000, gap_ns=gap_ns)

        ingested = observe_all(interpreter_polls(run, per_tick))

        for key in ingested.rings:
            seen = set(ingested.observed_for(key))
            for gap in ingested.gaps_for(key):
                assert gap.lost_from - 1 in seen
                assert gap.lost_from + gap.lost_count in seen

    def test_the_capture_names_the_records_it_lost(self) -> None:
        """The same partition on the verbatim two-poll capture, where the
        counters are real. gen 0 ran 466 through 563 and gcmon saw 22 of
        them."""
        captured = observe_all([build_batch(POLL_0), build_batch(POLL_1)])

        acc = captured[(0, 0)]
        gap = captured.gaps_for((0, 0))[0]

        assert (gap.lost_from, gap.lost_count) == (477, 76)
        assert charges(captured, (0, 0)) == Counter(range(acc.first_collections, acc.last_collections + 1))


class TestCaptureFixture:
    """Gap counts against the verbatim two-poll capture in test_monitor_cursor.

    The capture recorded no ``duration``, so every record carries the factory
    default and only counts mean anything here. The pause arithmetic is
    covered by the synthetic runs above.
    """

    def test_gen_0_lost_seventy_six_records(self, captured: Ingested) -> None:
        acc = captured[(0, 0)]

        assert (acc.first_collections, acc.last_collections) == (466, 563)
        assert [gap.lost_count for gap in captured.gaps_for((0, 0))] == [76]
        assert acc.sampled_count == 22

    def test_gen_1_lost_five_records(self, captured: Ingested) -> None:
        acc = captured[(0, 1)]

        assert (acc.first_collections, acc.last_collections) == (41, 51)
        assert [gap.lost_count for gap in captured.gaps_for((0, 1))] == [5]

    def test_an_unchanged_generation_loses_nothing(self, captured: Ingested) -> None:
        acc = captured[(0, 2)]

        assert acc.sampled_count == 1
        assert captured.gaps_for((0, 2)) == []

    def test_the_two_generations_share_one_span(self, captured: Ingested) -> None:
        """One poll went blind in gen 0 and gen 1 at once, and the row says so
        on one bar. Two bars was the old shape, and the widths were the part
        that misled: they differ by where each generation's next record
        happened to sit, not by when anything was lost."""
        spans = captured.spans()

        assert len(spans) == 1
        assert [(entry.gen, entry.lost_count) for entry in spans[0].gens] == [(0, 76), (1, 5)]

    def test_the_span_covers_the_interval_between_the_two_polls(self, captured: Ingested) -> None:
        span = captured.spans()[0]

        assert (span.ts_start, span.ts_stop) == (captured.polled_at[0], captured.polled_at[1])

    def test_a_generation_that_did_nothing_is_left_out(self, captured: Ingested) -> None:
        """gen 2 returned the same record in both polls, so it neither
        collected nor lost anything in the interval. An entry saying zero
        twice is noise on a slice a reader is trying to read quickly; the
        generations that contributed to the coverage figure are the ones that
        appear."""
        assert [entry.gen for entry in captured.spans()[0].gens] == [0, 1]


class TestTheRecordHandedToTheExporters:
    """The whole of what a poll emits, assembled where the arithmetic happens.

    Each generation's accumulator returns the entry it will be read as, so
    nothing downstream merges two halves back together and no field can be
    dropped between the two.
    """

    def polls(self) -> Ingested:
        gen0 = build_run(6, gen=0)
        gen1 = build_run(2, gen=1, ts0=TS0 + 500_000)
        gen2 = build_run(2, gen=2, ts0=TS0 + 700_000)

        ingested = Ingested()
        ingested.poll([gen0[0], gen1[0], gen2[0]], ts=10)
        # gen 0 loses records 2 through 4. The other two collect once each and
        # lose nothing. The batch arrives with the generations reversed.
        ingested.poll([gen2[1], gen1[1], gen0[4]], ts=99)
        return ingested

    def test_it_carries_the_interval_and_the_interpreter(self) -> None:
        [msg] = self.polls().recorder.losses

        assert (msg.iid, msg.ts_start, msg.ts_stop) == (0, 10, 99)

    def test_the_range_survives_the_handover_to_the_exporters(self) -> None:
        """The record is the only thing past this point: a range dropped here
        would leave every span in the trace back to counting alone."""
        [msg] = self.polls().recorder.losses

        entry = next(entry for entry in msg.gens if entry.gen == 0)
        assert (entry.lost_from, entry.lost_count) == (2, 3)

    def test_a_generation_that_only_observed_is_carried_too(self) -> None:
        [msg] = self.polls().recorder.losses

        assert [(entry.gen, entry.observed_count, entry.lost_count) for entry in msg.gens] == [
            (0, 1, 3),
            (1, 1, 0),
            (2, 1, 0),
        ]

    def test_the_generations_come_out_in_order(self) -> None:
        """They are read as a list on the slice, and a reader comparing two
        spans should not have to hunt for gen 1. The poll that found the loss
        handed its generations over backwards."""
        [msg] = self.polls().recorder.losses

        assert [entry.gen for entry in msg.gens] == [0, 1, 2]


class TestCounterOrderNotClockOrder:
    """``ingest`` folds a run by counter: the run's first record is the
    only one that can sit across a gap, and its last one settles the cursor.

    ``_ingest`` sorts on ``collections`` to give it that. A healthy ring makes
    the two orders agree, so this is the invariant holding rather than a bug
    reproducing. The cursor still means a counter, and a batch where the clock
    disagrees must not walk it backwards.
    """

    def skewed(self, events: Sequence[GCStatsInfo], nth: int) -> GCStatsInfo:
        """*nth* with the earliest ``ts_start`` in the run, counter intact."""
        return msgspec.structs.replace(events[nth], ts_start=events[0].ts_start - 1)

    def test_the_cursor_lands_on_the_highest_counter(self) -> None:
        events = build_run(3)

        ingested = Ingested()

        ingested.poll([events[0], events[1], self.skewed(events, 2)])

        assert ingested[(0, 0)].last_collections == 3

    def test_the_next_poll_finds_no_phantom_gap(self) -> None:
        """A cursor left short reports the records past it as lost, and
        re-emits them once they are read again."""
        events = build_run(5)

        ingested = Ingested()
        ingested.poll([events[0], events[1], self.skewed(events, 2)])

        ingested.poll([events[3]])

        assert ingested.gaps_for((0, 0)) == []


class TestTwoInterpreters:
    """One read covers every interpreter in the process, so they share an
    interval, but they collect independently and each gets its own record."""

    def polls(self) -> Ingested:
        first = build_run(6, gen=0, iid=0)
        second = build_run(6, gen=0, iid=3)

        ingested = Ingested()
        ingested.poll([first[0], second[0]], ts=1_000)
        ingested.poll([first[4], second[4]], ts=2_000)
        return ingested

    def test_each_interpreter_gets_a_record(self) -> None:
        assert {loss.iid for loss in self.polls().recorder.losses} == {0, 3}

    def test_they_share_the_interval(self) -> None:
        losses = self.polls().recorder.losses

        assert {(loss.ts_start, loss.ts_stop) for loss in losses} == {(1_000, 2_000)}

    def test_an_interpreter_that_lost_nothing_gets_no_record(self) -> None:
        first = build_run(6, gen=0, iid=0)
        second = build_run(6, gen=0, iid=3)

        ingested = Ingested()
        ingested.poll([first[0], second[0]], ts=1_000)
        ingested.poll([first[4], second[1]], ts=2_000)

        assert {loss.iid for loss in ingested.recorder.losses} == {0}

    def test_the_row_of_the_interpreter_that_lost_nothing_is_clean(self) -> None:
        """The table has to say what the trace says: interpreter 3 skips and
        interpreter 0 does not, so only one row shows a gap.

        Interpreter 3 is the one that loses on purpose. A monitor handing
        `record_loss` a constant iid would land its gap on interpreter 0 and
        satisfy the same assertion the other way round.
        """
        first = build_run(6, gen=0, iid=0)
        second = build_run(6, gen=0, iid=3)

        ingested = Ingested()
        ingested.poll([first[0], second[0]], ts=1_000)
        ingested.poll([first[1], second[4]], ts=2_000)

        assert ingested.stats.pause_totals(proc(PID), 3, 0).lost_count == 3
        assert ingested.stats.pause_totals(proc(PID), 0, 0).lost_count == 0
        assert ingested.stats.pause_totals(proc(PID), 0, 0).coverage == 1.0

    def test_each_row_carries_the_loss_its_own_record_drew(self) -> None:
        """One arithmetic, two readers: whatever a `GC Loss` record says an
        interpreter missed is what that interpreter's row counts."""
        ingested = self.polls()

        for loss in ingested.recorder.losses:
            drawn = sum(gen.lost_count for gen in loss.gens)
            assert ingested.stats.pause_totals(proc(PID), loss.iid, 0).lost_count == drawn


class TestTheStatsAreRecordedWhateverIsDrawn:
    """`_ingest` records the loss before it builds a record, and the counts
    are the ring's own counters, so every cell of the `--stats` row comes from
    the same arithmetic whatever the trace ends up showing."""

    def ingested(self) -> Ingested:
        events = build_run(6)

        ingested = Ingested()
        ingested.poll([events[0]], ts=1_000)
        ingested.poll([events[4]], ts=2_000)
        return ingested

    def test_the_collections_reach_the_table(self) -> None:
        stats = self.ingested().stats

        assert stats.pause_totals(proc(PID), 0, 0).lost_count == 3
        assert stats.pause_totals(proc(PID), 0, 0).lost_pause_ns > 0

    def test_the_exact_totals_include_them(self) -> None:
        stats = self.ingested().stats

        assert stats.pause_totals(proc(PID), 0, 0).exact_count == 5
        assert stats.pause_totals(proc(PID), 0, 0).coverage == pytest.approx(2 / 5)


class TestForgettingAPid:
    """A reused pid must inherit neither a counter nor an interval."""

    def test_forget_drops_the_poll_instant(self) -> None:
        events = build_run(6)

        ingested = Ingested()
        ingested.poll([events[0]], ts=1_000)
        ingested.monitor._forget(PID, 0)
        ingested.poll([events[4]], ts=2_000)

        assert ingested.recorder.losses == []

    def test_retain_drops_it_too(self) -> None:
        events = build_run(6)

        ingested = Ingested()
        ingested.poll([events[0]], ts=1_000)
        ingested.monitor._retain(set(), 0)
        ingested.poll([events[4]], ts=2_000)

        assert ingested.recorder.losses == []


class TestADuplicateCounterInOnePoll:
    """Two slots reporting one `collections` value, and which one survives.

    `RingAccumulator.unseen` keys a poll's run on the counter, so a duplicate
    pair collapses to one record. A dict keeps the last, which the sort leaves
    in slot order. Nothing in the suite distinguished the two before this
    class, and the choice is not inert: the run's last record sets
    `last_duration`, which becomes the next poll's pause base.

    It cannot matter on truthful data, where a duplicate is a byte-identical
    copy of its twin and either resolution gives the same answer. ADR-0015
    says why, and what it would take to stop holding. These tests pin the
    resolution anyway, so a change to it is deliberate rather than silent.
    """

    def _pair(self) -> tuple[GCStatsInfo, GCStatsInfo]:
        first = build_run(1, gen=0)[0]
        later = msgspec.structs.replace(
            first, ts_start=first.ts_start + 5_000, ts_stop=first.ts_stop + 5_000, duration=first.duration + 0.000_005
        )
        return first, later

    def test_the_later_slot_wins(self) -> None:
        first, later = self._pair()

        ingested = Ingested()

        ingested.poll([first, later])

        assert ingested[(0, 0)].last_duration == later.duration

    def test_only_one_of_the_pair_is_counted(self) -> None:
        """A duplicate must not inflate the sample it is measured against."""
        first, later = self._pair()

        ingested = Ingested()

        ingested.poll([first, later])

        assert ingested[(0, 0)].sampled_count == 1

    def test_the_pair_opens_no_gap(self) -> None:
        first, later = self._pair()

        ingested = Ingested()

        ingested.poll([first, later])

        assert ingested.gaps_for((0, 0)) == []

    def test_a_byte_identical_twin_leaves_no_trace(self) -> None:
        """What the target actually produces: the copy before the overwrite.
        Either resolution gives the same answer, which is why the choice has
        never shown up."""
        first = build_run(1, gen=0)[0]

        one = Ingested()
        one.poll([first])
        two = Ingested()
        two.poll([first, msgspec.structs.replace(first)])

        assert two[(0, 0)] == one[(0, 0)]
