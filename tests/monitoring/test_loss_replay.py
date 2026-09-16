"""What the loss arithmetic recovers, against a target whose every collection
is known.

Every other test of the loss code hands the monitor batches somebody wrote by
hand, which can only check that the arithmetic does what its author expected.
This one starts from `SSL_CONTEXT_SIZE`, a capture of every collection a real
target ran, puts a ring buffer and a poll clock in front of it, and asks
whether what gcmon reconstructs from the wreckage matches the target. The
capture is the answer key; the ring is what hides it.

The ring model follows `add_stats` in CPython's `Python/gc.c`: it advances the
index before writing, so record `k` lands in slot `k % size`, and it publishes
`ts_stop` last, so a record becomes readable when its collection ends.
`read_gc_stats` then copies the whole `struct gc_stats` in one read and walks
it generation-major, each generation's slots in index order. `TestTheRingModel`
holds the model to the verbatim hardware capture in `test_monitor_cursor`, so a
model that drifts from the extension fails here rather than quietly making
every claim below easier.
"""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from typing import override
from unittest.mock import patch

import pytest

from gcmon.exporters.exporter import EventsExporter
from gcmon.model.data import GCStatsInfo
from gcmon.model.poll_status import PollStatus
from gcmon.model.process import Process
from gcmon.model.protocol import TGCStatsInfo, TGenLoss, TInstantMsg, TLossMsg
from gcmon.monitoring.monitor import EventsMonitor
from gcmon.monitoring.target_process import ExternalProcess
from gcmon.monitoring.wait_policy import no_wait_policy
from gcmon.stats.streaming_stats import StreamingStats
from tests.captures import SSL_CONTEXT_SIZE
from tests.helpers import FakeEventsReader, create_mock_stats_item, polled, proc
from tests.monitoring.test_monitor_cursor import POLL_0

PID = 33328
IID = 0

# CPython 3.15's default build. A parameter rather than a constant, so a test
# can ask what the free-threaded geometry of one slot per ring would cost.
RING_SIZES = {0: 11, 1: 3, 2: 3}
FREE_THREADED_SIZES = {0: 1, 1: 1, 2: 1}

MS = 1_000_000

# What one read costs the replayed monitor. Only the read-time
# statistic reads it; a span's edges are two wakes, and a wake is where the
# read began.
READ_COST_NS = 600_000

# Poll periods the replays below run at, in ms. 250 outruns the target, 1000
# blinds gen 0 alone, and 3000 blinds gen 0 and gen 1 together, which is the
# only shape that puts two generations' counts on one span.
LOSSLESS_MS = 250
ONE_GENERATION_MS = 1000
TWO_GENERATIONS_MS = 3000


class Recorder(EventsExporter):
    """Every record the monitor exported, split by kind."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[TGCStatsInfo] = []
        self.losses: list[TLossMsg] = []

    @override
    def add_event(self, process: Process, item: TGCStatsInfo) -> None:
        self.records.append(item)

    @override
    def add_loss_event(self, process: Process, item: TLossMsg) -> None:
        self.losses.append(item)

    @override
    def add_instant_event(self, process: Process, item: TInstantMsg) -> None:
        pass

    @override
    def close(self) -> None:
        pass


def capture_records(
    capture: Sequence[tuple[int, int, int, int, float]] = SSL_CONTEXT_SIZE,
) -> dict[int, list[GCStatsInfo]]:
    """The capture as records, one list per generation, in counter order."""
    per_gen: dict[int, list[GCStatsInfo]] = {}
    for gen, collections, ts_start, ts_stop, duration in capture:
        per_gen.setdefault(gen, []).append(
            create_mock_stats_item(
                gen=gen,
                iid=IID,
                ts_start=ts_start,
                ts_stop=ts_stop,
                heap_size=0,
                collections=collections,
                collected=0,
                uncollectable=0,
                candidates=0,
                duration=duration,
            )
        )
    for records in per_gen.values():
        records.sort(key=lambda record: record.collections)
    return per_gen


def empty_slot(gen: int) -> GCStatsInfo:
    """A slot the target never wrote.

    Zeroed but for the ring it belongs to, which is how the extension returns
    one: `read_gc_stats` sets `gen` from the loop it is in rather than from the
    slot. `_is_complete` rejects it.
    """
    return create_mock_stats_item(
        gen=gen,
        iid=IID,
        ts_start=0,
        ts_stop=0,
        heap_size=0,
        collections=0,
        collected=0,
        uncollectable=0,
        candidates=0,
        duration=0.0,
    )


def ring_at(records: Sequence[GCStatsInfo], gen: int, size: int, ts: int) -> list[GCStatsInfo]:
    """One ring's slots as they stand at *ts*, in slot order."""
    written = [record for record in records if record.ts_stop <= ts]
    slots = [empty_slot(gen) for _ in range(size)]
    for record in written[-size:]:
        slots[record.collections % size] = record
    return slots


def poll_batches(
    per_gen: dict[int, list[GCStatsInfo]], sizes: dict[int, int], interval_ns: int, skew_ns: int = 0
) -> Iterator[tuple[int, list[GCStatsInfo]]]:
    """One ``(wake, batch)`` per tick of a clock running every *interval_ns*.

    The wake instant comes back with the batch because a loss record is
    bounded by two of them: the monitor reads its own clock, and a replay that
    let it read the real one would put every span light-years from the
    capture's timestamps.

    The first wake lands on the first collection in the capture, so the run
    starts against a ring holding something. The last lands past the final
    collection, so no record goes unread for want of a poll to read it.

    *skew_ns* tears the read. One `ReadRemoteMemory` copies the whole
    `struct gc_stats` without stopping the target, so the rings in it can
    reflect different instants; `struct gc_stats` holds gen 0 first, so a copy
    running up through memory takes the older generations later. Each ring is
    read `skew_ns` after the one below it. Zero is the untorn read, and the
    direction is a model of one plausible copy and not a guarantee: nothing
    below rests on which ring comes out newer, only on their disagreeing.
    """
    last = max(record.ts_stop for records in per_gen.values() for record in records)

    ts = min(record.ts_stop for records in per_gen.values() for record in records)
    while ts <= last + interval_ns:
        batch: list[GCStatsInfo] = []
        for gen in sorted(per_gen):
            batch.extend(ring_at(per_gen[gen], gen, sizes[gen], ts + skew_ns * gen))
        yield ts, batch
        ts += interval_ns


@dataclass
class Replay:
    """One run of the monitor over the capture, with the answer key beside it."""

    truth: dict[int, list[GCStatsInfo]]
    recorder: Recorder
    stats: StreamingStats
    polls: int = 0
    wakes: list[int] = field(default_factory=list)
    _read: dict[int, set[int]] = field(default_factory=dict)

    def read(self, gen: int) -> set[int]:
        """The counters gcmon exported a record for."""
        if not self._read:
            for record in self.recorder.records:
                self._read.setdefault(record.gen, set()).add(record.collections)
        return self._read.get(gen, set())

    def entries(self, gen: int) -> list[TGenLoss]:
        """Every gap one ring reported, one per interval it lost records in."""
        return [entry for loss in self.recorder.losses for entry in loss.gens if entry.gen == gen and entry.lost_count]

    def lost(self, gen: int) -> set[int]:
        """The counters the gaps claim, expanded from their ranges."""
        return {
            collections
            for entry in self.entries(gen)
            for collections in range(entry.lost_from, entry.lost_from + entry.lost_count)
        }

    def span(self, gen: int) -> range:
        """The collections between the first and last gcmon read, inclusive.

        What ran before gcmon attached is outside anything it can claim, so
        this is the interval every check below is scoped to.
        """
        read = self.read(gen)
        return range(min(read), max(read) + 1)

    def truth_pause_ns(self, gen: int, collections: Sequence[int] | set[int]) -> int:
        """What those collections really cost, from the capture's own
        timestamps rather than from the cumulative `duration` gcmon works
        off."""
        by_counter = {record.collections: record for record in self.truth[gen]}
        return sum(by_counter[c].ts_stop - by_counter[c].ts_start for c in collections)


def replay(interval_ms: float, sizes: dict[int, int] = RING_SIZES, skew_ms: float = 0) -> Replay:
    """Poll the capture through a ring of *sizes* every *interval_ms*."""
    truth = capture_records()
    batches = list(poll_batches(truth, sizes, int(interval_ms * MS), int(skew_ms * MS)))

    recorder = Recorder()
    stats = StreamingStats()
    reads = iter(batches)
    wakes: list[int] = []

    def one_read(pid: int) -> list[GCStatsInfo]:
        wake, batch = next(reads)
        wakes.append(wake)
        return list(batch)

    monitor = EventsMonitor(
        ExternalProcess(pid=PID),
        recorder,
        stats,
        reader=FakeEventsReader(one_read),
        wait_policy_factory=no_wait_policy,
    )

    def clock() -> Iterator[int]:
        # `poll` reads the clock before the batch is fetched and again after,
        # so the first call of each pair has to guess the wake it is about to
        # land on. They are known in advance; walking them here keeps the
        # monitor reading the capture's clock rather than the machine's.
        for wake, _batch in batches:
            yield wake
            yield wake + READ_COST_NS

    ticks = clock()

    with patch("gcmon.monitoring.monitor.time.monotonic_ns", side_effect=lambda: next(ticks)):
        for _ in batches:
            assert monitor._poll(PID).status is PollStatus.OK

    return Replay(truth=truth, recorder=recorder, stats=stats, polls=len(batches), wakes=wakes)


LOSSY_MS = [ONE_GENERATION_MS, TWO_GENERATIONS_MS]


@pytest.fixture(scope="module")
def lossless() -> Replay:
    return replay(LOSSLESS_MS)


@pytest.fixture(scope="module")
def lossy() -> dict[float, Replay]:
    return {interval_ms: replay(interval_ms) for interval_ms in LOSSY_MS}


class TestTheRingModel:
    """The model against the extension, before anything is claimed with it."""

    def test_it_lays_out_the_verbatim_hardware_capture(self) -> None:
        """`POLL_0` is a real `get_gc_stats` return, rotated the way the target
        left it. Rebuilding it from its own records slot by slot is what says
        the model here is CPython's ring and not a plausible one: the counters
        alone do not fix an order, and `% size` off by one still produces a
        ring holding the same records in the wrong places.
        """
        per_gen: dict[int, list[GCStatsInfo]] = {}
        for gen, collections, ts_start, ts_stop in POLL_0:
            if ts_start < ts_stop:
                per_gen.setdefault(gen, []).append(
                    create_mock_stats_item(
                        gen=gen,
                        iid=IID,
                        ts_start=ts_start,
                        ts_stop=ts_stop,
                        heap_size=0,
                        collections=collections,
                        collected=0,
                        uncollectable=0,
                        candidates=0,
                        duration=0.0,
                    )
                )
        newest = max(ts_stop for _gen, _c, _ts, ts_stop in POLL_0)

        rebuilt = [
            (slot.gen, slot.collections, slot.ts_start, slot.ts_stop)
            for gen in (0, 1, 2)
            for slot in ring_at(sorted(per_gen.get(gen, []), key=lambda r: r.collections), gen, RING_SIZES[gen], newest)
        ]

        assert rebuilt == POLL_0

    def test_a_ring_holds_only_its_newest_records(self) -> None:
        gen_0 = capture_records()[0]

        held = ring_at(gen_0, 0, RING_SIZES[0], gen_0[49].ts_stop)

        assert {slot.collections for slot in held} == set(range(40, 51))

    def test_an_unwritten_slot_still_names_its_generation(self) -> None:
        """Gen 2 collected twice in the whole capture, so its ring never fills.
        A real poll returns those unwritten slots under their own generation,
        and a replay that filed them elsewhere would hand the monitor a batch
        no target ever produces."""
        held = ring_at(capture_records()[2], 2, RING_SIZES[2], 0)

        assert [slot.gen for slot in held] == [2, 2, 2]
        assert [slot.collections for slot in held] == [0, 0, 0]


class TestTheReplayLosesWhatItClaimsTo:
    """Controls. Every check below is vacuous against a replay that read
    everything, and a replay silently reading everything is the likeliest way
    for this file to stop testing the loss code at all."""

    def test_polling_faster_than_the_target_collects_loses_nothing(self, lossless: Replay) -> None:
        assert lossless.recorder.losses == []
        assert [len(lossless.read(gen)) for gen in (0, 1, 2)] == [230, 20, 2]

    def test_one_generation_goes_blind(self, lossy: dict[float, Replay]) -> None:
        run = lossy[ONE_GENERATION_MS]

        assert run.stats.pause_totals(proc(PID), 0, 0).coverage < 0.6
        assert [len(run.entries(gen)) > 0 for gen in (0, 1, 2)] == [True, False, False]

    def test_two_generations_go_blind_in_one_interval(self, lossy: dict[float, Replay]) -> None:
        """What puts two generations' counts on one span, which is the
        precondition for anything below to have a merge to check."""
        run = lossy[TWO_GENERATIONS_MS]

        together = [
            [entry.gen for entry in loss.gens if entry.lost_count]
            for loss in run.recorder.losses
            if sum(1 for entry in loss.gens if entry.lost_count) > 1
        ]

        assert together, "no interval lost records in more than one generation"

    def test_a_ring_of_one_slot_loses_almost_everything(self) -> None:
        """The free-threaded geometry, where a poll can keep at most the last
        collection of each generation."""
        run = replay(LOSSLESS_MS, FREE_THREADED_SIZES)

        assert run.stats.pause_totals(proc(PID), 0, 0).coverage < 0.25


@pytest.mark.parametrize("interval_ms", LOSSY_MS)
class TestEveryCollectionIsChargedOnce:
    """The claim `GenLoss.lost_from` exists to support: over the span gcmon
    observed, every collection the target ran is either a record gcmon drew or
    a counter inside exactly one window, and never both."""

    def test_nothing_is_charged_twice(self, interval_ms: float, lossy: dict[float, Replay]) -> None:
        run = lossy[interval_ms]

        for gen in (0, 1, 2):
            assert run.read(gen) & run.lost(gen) == set(), f"gen {gen} drew a record it also called lost"

    def test_nothing_is_charged_nowhere(self, interval_ms: float, lossy: dict[float, Replay]) -> None:
        run = lossy[interval_ms]

        for gen in (0, 1, 2):
            assert set(run.span(gen)) - run.read(gen) - run.lost(gen) == set()

    def test_nothing_is_charged_outside_the_span(self, interval_ms: float, lossy: dict[float, Replay]) -> None:
        """A window reaching behind the first record gcmon read would be
        claiming collections that ran before it attached."""
        run = lossy[interval_ms]

        for gen in (0, 1, 2):
            assert run.lost(gen) <= set(run.span(gen))

    def test_the_windows_do_not_overlap_each_other(self, interval_ms: float, lossy: dict[float, Replay]) -> None:
        run = lossy[interval_ms]

        for gen in (0, 1, 2):
            claimed = sum(entry.lost_count for entry in run.entries(gen))
            assert claimed == len(run.lost(gen)), f"gen {gen} gaps claim overlapping counters"


@pytest.mark.parametrize("interval_ms", LOSSY_MS)
class TestTheTotalsAreExact:
    """`--stats` promises counts and sums that cover every collection, read or
    not. The capture says what those are."""

    def test_the_count_covers_the_whole_span(self, interval_ms: float, lossy: dict[float, Replay]) -> None:
        run = lossy[interval_ms]

        for gen in (0, 1, 2):
            assert run.stats.pause_totals(proc(PID), 0, gen).exact_count == len(run.span(gen))

    def test_the_pause_sum_matches_the_target(self, interval_ms: float, lossy: dict[float, Replay]) -> None:
        """Within a microsecond over eleven seconds. The reconstruction adds up
        deltas of a cumulative double of seconds; the capture carries integer
        nanoseconds. They cannot agree exactly and nothing should claim they
        do."""
        run = lossy[interval_ms]

        for gen in (0, 1, 2):
            truth = run.truth_pause_ns(gen, run.span(gen))
            assert run.stats.pause_totals(proc(PID), 0, gen).exact_pause_ns == pytest.approx(truth, abs=1_000)

    def test_the_lost_pause_is_the_pause_of_the_lost_records(
        self, interval_ms: float, lossy: dict[float, Replay]
    ) -> None:
        """Not a share of the window, and not the window's width: the time the
        collections nobody saw actually spent collecting."""
        run = lossy[interval_ms]

        for gen in (0, 1, 2):
            truth = run.truth_pause_ns(gen, run.lost(gen))
            assert run.stats.pause_totals(proc(PID), 0, gen).lost_pause_ns == pytest.approx(truth, abs=1_000)

    def test_coverage_is_the_share_actually_read(self, interval_ms: float, lossy: dict[float, Replay]) -> None:
        run = lossy[interval_ms]

        for gen in (0, 1, 2):
            assert run.stats.pause_totals(proc(PID), 0, gen).coverage == pytest.approx(
                len(run.read(gen)) / len(run.span(gen))
            )


@pytest.mark.parametrize("interval_ms", LOSSY_MS)
class TestTheSpansTileTheTimeline:
    """Geometry, over the same replays. The loss row is a Perfetto stack and
    nothing downstream complains when it is built wrong."""

    def test_every_span_bounds_an_interval(self, interval_ms: float, lossy: dict[float, Replay]) -> None:
        """Two reads happen at two instants, so this cannot fail for the
        reason it used to: edges taken off the records could arrive reversed
        and the span had to be held back."""
        run = lossy[interval_ms]

        assert [loss for loss in run.recorder.losses if loss.ts_start >= loss.ts_stop] == []

    def test_the_spans_never_overlap(self, interval_ms: float, lossy: dict[float, Replay]) -> None:
        """One row per interpreter holds all of them now, so this is over the
        whole run rather than per generation."""
        run = lossy[interval_ms]

        ordered = sorted(run.recorder.losses, key=lambda loss: loss.ts_start)
        assert all(a.ts_stop <= b.ts_start for a, b in pairwise(ordered))

    def test_every_span_runs_between_two_consecutive_polls(
        self, interval_ms: float, lossy: dict[float, Replay]
    ) -> None:
        """Where the edges come from, checked against the clock the replay
        drove rather than against the records."""
        run = lossy[interval_ms]
        consecutive = set(pairwise(run.wakes))

        for loss in run.recorder.losses:
            assert (loss.ts_start, loss.ts_stop) in consecutive

    def test_a_poll_emits_one_span_at_most(self, interval_ms: float, lossy: dict[float, Replay]) -> None:
        run = lossy[interval_ms]

        assert len({(loss.iid, loss.ts_start) for loss in run.recorder.losses}) == len(run.recorder.losses)


class TestATornRead:
    """A read whose rings disagree about when "now" is, and what it costs.

    The read is one copy of the whole `struct gc_stats` taken while the target
    runs, so a poll can take gen 1 later than gen 0. Under the old geometry
    that was expensive: a window's left edge was the newest record any ring
    returned, so a torn read could set it past the record that closed the
    window, and the span had to be held back with `--stats` counting a loss
    the trace never showed.

    Bounding a span by the two reads instead costs it nothing. The edges are
    the monitor's own clock, which no tear can invert, and the counts were
    never at risk: `lost_count` and `lost_from` subtract the ring's cumulative
    counters and carry no timestamp at all.
    """

    # Found by sweeping the capture: a poll period the ring survives almost
    # every time, so the one window that does open is a single collection wide
    # and a read taken 20 ms apart across the rings is enough to invert it.
    # A wider window has room for the skew and draws normally.
    INTERVAL_MS = 330
    SKEW_MS = 20

    @pytest.fixture(scope="class")
    def torn(self) -> Replay:
        return replay(self.INTERVAL_MS, skew_ms=self.SKEW_MS)

    @pytest.fixture(scope="class")
    def untorn(self) -> Replay:
        return replay(self.INTERVAL_MS)

    def test_the_same_run_read_whole_draws_a_span(self, untorn: Replay) -> None:
        """The control that makes this a test of the tear and not of the poll
        period: read without skew, the same loss draws one span."""
        assert untorn.stats.pause_totals(proc(PID), 0, 0).lost_count == 1
        assert len(untorn.recorder.losses) == 1

    def test_the_torn_read_draws_one_too(self, torn: Replay) -> None:
        """What the change bought. The tear moves which records a poll sees;
        it cannot move the instants the poll happened at, so there is no
        geometry left for it to break."""
        assert len(torn.recorder.losses) == 1

    def test_the_span_still_bounds_an_interval(self, torn: Replay) -> None:
        span = torn.recorder.losses[0]

        assert span.ts_start < span.ts_stop
        assert (span.ts_start, span.ts_stop) in set(pairwise(torn.wakes))

    def test_the_count_is_kept_anyway(self, torn: Replay) -> None:
        """`--stats` covers every collection whether or not a bar came of it."""
        assert torn.stats.pause_totals(proc(PID), 0, 0).lost_count == 1
        assert torn.stats.pause_totals(proc(PID), 0, 0).exact_count == len(torn.span(0))

    def test_the_lost_pause_is_kept_anyway(self, torn: Replay) -> None:
        missing = set(torn.span(0)) - torn.read(0)

        assert torn.stats.pause_totals(proc(PID), 0, 0).lost_pause_ns == torn.truth_pause_ns(0, missing)

    def test_the_totals_still_match_the_target(self, torn: Replay) -> None:
        for gen in (0, 1, 2):
            truth = torn.truth_pause_ns(gen, torn.span(gen))
            assert torn.stats.pause_totals(proc(PID), 0, gen).exact_pause_ns == pytest.approx(truth, abs=1_000)

    def test_coverage_is_unmoved_by_the_tear(self, torn: Replay) -> None:
        assert torn.stats.pause_totals(proc(PID), 0, 0).coverage == pytest.approx(len(torn.read(0)) / len(torn.span(0)))


class TestAMidWriteSlot:
    """`add_stats` is not atomic, so a poll can catch a slot part-written.

    It memcpy's the record it is about to overwrite into the new slot, then
    stores the new `ts_start`, then increments `collections`, and publishes
    `ts_stop` last. A reader therefore sees one of two things it must not take
    at face value, and `_ingest` guards both.
    """

    def batch_at(self, ts: int, sizes: dict[int, int] = RING_SIZES) -> list[GCStatsInfo]:
        truth = capture_records()
        return [slot for gen in sorted(truth) for slot in ring_at(truth[gen], gen, sizes[gen], ts)]

    def test_a_half_written_slot_is_not_read(self) -> None:
        """After the `ts_start` store and before the `ts_stop` one, the slot
        carries the new record's start over the previous record's stop. The
        start is the later of the two, which is what `_is_complete` keys on."""
        truth = capture_records()
        settled = truth[0][20]
        in_flight = truth[0][21]
        batch = self.batch_at(settled.ts_stop)
        half_written = create_mock_stats_item(
            gen=0,
            iid=IID,
            ts_start=in_flight.ts_start,
            ts_stop=truth[0][10].ts_stop,
            heap_size=0,
            collections=in_flight.collections,
            collected=0,
            uncollectable=0,
            candidates=0,
            duration=truth[0][10].duration,
        )
        batch[in_flight.collections % RING_SIZES[0]] = half_written

        recorder = Recorder()
        monitor = EventsMonitor(
            ExternalProcess(pid=PID),
            recorder,
            StreamingStats(),
            reader=FakeEventsReader(),
            wait_policy_factory=no_wait_policy,
        )
        monitor._ingest(polled(monitor, PID), batch, ts_poll=1)

        assert half_written.ts_start > half_written.ts_stop
        assert in_flight.collections not in {record.collections for record in recorder.records}

    def test_the_copy_made_ahead_of_a_write_is_not_counted_twice(self) -> None:
        """Between the memcpy and the `collections` increment, two slots hold
        the same record. Nothing tells them apart by threshold, so `_ingest`
        keys the run on the counter."""
        truth = capture_records()
        settled = truth[0][20]
        batch = self.batch_at(settled.ts_stop)
        twin_slot = (settled.collections + 1) % RING_SIZES[0]
        batch[twin_slot] = settled

        recorder = Recorder()
        stats = StreamingStats()
        monitor = EventsMonitor(
            ExternalProcess(pid=PID),
            recorder,
            stats,
            reader=FakeEventsReader(),
            wait_policy_factory=no_wait_policy,
        )
        monitor._ingest(polled(monitor, PID), batch, ts_poll=1)

        counters = [record.collections for record in recorder.records if record.gen == 0]
        assert counters == sorted(set(counters))
        assert settled.collections in counters
        assert stats.pause_totals(proc(PID), 0, 0).lost_count == 0
