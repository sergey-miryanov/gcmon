"""Tests for what one ring can say about the records it did not return.

``RingAccumulator`` folds the records of a single ring, one poll at a time,
and every test here drives it directly. What a whole poll makes of several
rings at once, and the loss record it emits, is
``tests/monitoring/test_monitor_loss.py``.
"""

from collections.abc import Sequence

import msgspec.structs
import pytest

from gcmon.model.data import GCStatsInfo
from gcmon.model.loss import RingAccumulator
from gcmon.model.names import GEN, LOST_COUNT, LOST_FROM, LOST_PAUSE_NS, OBSERVED_COUNT
from gcmon.support.time_units import secs_to_ns
from tests.helpers import TS0, build_run, create_mock_stats_item, true_pause_ns


def fold_singly(events: Sequence[GCStatsInfo]) -> RingAccumulator:
    """The same records, one single-record run at a time."""
    accumulator = RingAccumulator()
    for event in events:
        accumulator.ingest([event])
    return accumulator


@pytest.fixture
def accumulator() -> RingAccumulator:
    return RingAccumulator()


class TestEmptyAccumulator:
    def test_reports_nothing(self, accumulator: RingAccumulator) -> None:
        assert accumulator.exact_count == 0
        assert accumulator.exact_pause_ns == 0

    def test_last_starts_below_every_counter(self, accumulator: RingAccumulator) -> None:
        """``last`` doubles as the poll cursor, and CPython counts from 1."""
        assert accumulator.last_collections == 0


class TestFencepost:
    def test_one_record_spans_itself(self, accumulator: RingAccumulator) -> None:
        entry = accumulator.ingest(
            [create_mock_stats_item(collections=42, ts_start=1_000, ts_stop=1_700, duration=0.0007)]
        )

        assert accumulator.exact_count == 1
        assert accumulator.exact_pause_ns == 700
        assert accumulator.sampled_pause_ns == 700
        assert (entry.observed_count, entry.lost_count) == (1, 0)

    def test_two_adjacent_records_leave_no_gap(self, accumulator: RingAccumulator) -> None:
        entry = accumulator.ingest(build_run(2))

        assert accumulator.exact_count == 2
        assert (entry.observed_count, entry.lost_count) == (2, 0)

    def test_exact_pause_covers_the_first_record(self, accumulator: RingAccumulator) -> None:
        """The delta of a cumulative field starts *after* the first record, so
        dropping the fencepost term would under-report by one pause."""
        events = build_run(5)

        accumulator.ingest(events)

        assert accumulator.exact_pause_ns == true_pause_ns(events, 1, 5)
        assert accumulator.exact_pause_ns != secs_to_ns(events[-1].duration - events[0].duration)

    def test_a_span_starting_late_ignores_earlier_collections(self, accumulator: RingAccumulator) -> None:
        """gcmon cannot tell "ran before we attached" from "lost", so
        collections before the first observed record are outside the span."""
        events = build_run(20)

        accumulator.ingest(events[10:])

        assert accumulator.exact_count == 10
        assert accumulator.exact_pause_ns == true_pause_ns(events, 11, 20)


class TestGapDetection:
    """Gaps open at the seam between two polls, so every case here folds one
    run, then another that starts further along than the first ended."""

    def test_a_skipped_record_opens_a_gap(self, accumulator: RingAccumulator) -> None:
        events = build_run(3)
        accumulator.ingest([events[0]])

        entry = accumulator.ingest([events[2]])

        assert entry.lost_count == 1
        assert entry.lost_pause_ns == events[1].ts_stop - events[1].ts_start

    def test_the_gap_names_the_collections_it_is_missing(self, accumulator: RingAccumulator) -> None:
        """The gap is found by subtracting the ring's own counters, so both
        bounds are in hand before the count is. Records 2, 3 and 4 never
        arrived: the entry says so rather than saying "three of them"."""
        events = build_run(6)
        accumulator.ingest([events[0]])

        entry = accumulator.ingest([events[4]])

        assert (entry.lost_from, entry.lost_count) == (2, 3)
        assert [e.collections for e in events[1:4]] == [2, 3, 4]

    def test_the_range_stops_short_of_the_records_that_bound_it(self, accumulator: RingAccumulator) -> None:
        """Both fences, in the smallest case that has them: the record before
        the gap and the record after it were observed and are drawn, so a range
        reaching either would charge a collection twice."""
        events = build_run(3)
        accumulator.ingest([events[0]])

        entry = accumulator.ingest([events[2]])

        assert entry.lost_from == events[0].collections + 1
        assert entry.lost_from + entry.lost_count == events[2].collections

    def test_the_entry_carries_no_timestamps(self, accumulator: RingAccumulator) -> None:
        """Where the lost records ran is not something the ring knows, and the
        two polls that bracket them already say all there is to say about it.
        An entry that carried bounds of its own would be a second answer to
        that question, derived from the records rather than from the reads."""
        events = build_run(3)
        accumulator.ingest([events[0]])

        entry = accumulator.ingest([events[2]])

        assert set(msgspec.structs.asdict(entry)) == {
            GEN,
            OBSERVED_COUNT,
            LOST_FROM,
            LOST_COUNT,
            LOST_PAUSE_NS,
        }

    def test_a_pause_shortfall_floors_at_zero(self, accumulator: RingAccumulator) -> None:
        """``duration`` is a cumulative float of seconds while the bounds are
        ns timestamps, so a gap holding almost no pause can subtract to a hair
        below zero. Negative pause has no meaning downstream: it would drag
        the exact sum under the one gcmon measured, and make the scale factor
        shrink what it exists to grow."""
        first = create_mock_stats_item(gen=0, collections=1, ts_start=TS0, ts_stop=TS0 + 100_000, duration=100e-6)
        # Record 2 is lost. Record 3's own pause is 200 us, but the target's
        # accumulator has only moved 199 us since record 1.
        third = create_mock_stats_item(
            gen=0, collections=3, ts_start=TS0 + 10_000_000, ts_stop=TS0 + 10_200_000, duration=299e-6
        )

        accumulator.ingest([first])

        entry = accumulator.ingest([third])

        assert entry.lost_pause_ns == 0

    def test_the_entry_carries_its_generation(self, accumulator: RingAccumulator) -> None:
        events = build_run(3, gen=1)
        accumulator.ingest([events[0]])

        entry = accumulator.ingest([events[2]])

        assert entry.gen == 1

    def test_a_lossless_run_opens_none(self, accumulator: RingAccumulator) -> None:
        entry = accumulator.ingest(build_run(50))

        assert (entry.lost_count, entry.lost_from, entry.lost_pause_ns) == (0, 0, 0)
        assert accumulator.exact_count == accumulator.sampled_count
        assert accumulator.exact_pause_ns == pytest.approx(accumulator.sampled_pause_ns, abs=1)

    def test_no_gap_before_the_first_record_or_after_the_last(self, accumulator: RingAccumulator) -> None:
        events = build_run(30)

        entry = accumulator.ingest(events[10:20])

        assert entry.lost_count == 0


class TestIngestingARunAtOnce:
    """A poll hands over one ring's run at once. Whatever that saves, it has
    to leave the accumulator where folding the same records one at a time
    would have left it."""

    @pytest.mark.parametrize("count", [1, 2, 11])
    def test_a_run_matches_record_by_record(self, count: int) -> None:
        events = build_run(count)
        batched = RingAccumulator()

        batched.ingest(events)

        assert batched == fold_singly(events)

    def test_a_poll_returning_nothing_new_folds_nothing(self, accumulator: RingAccumulator) -> None:
        """`ingest` takes a non-empty run, and `unseen` is what keeps
        that true. A generation whose ring returned only records gcmon already
        has contributed neither loss nor coverage."""
        events = build_run(5)
        accumulator.ingest(events)

        assert accumulator.unseen(events) == []

    def test_consecutive_polls_pick_up_where_the_last_left_off(self) -> None:
        events = build_run(20)
        batched = RingAccumulator()

        batched.ingest(events[:11])
        batched.ingest(events[11:])

        assert batched == fold_singly(events)

    def test_a_gap_between_two_runs_is_found(self) -> None:
        """The seam between polls is where a ring loses records, and the only
        place a contiguous run can have lost any."""
        events = build_run(20)
        batched = RingAccumulator()

        batched.ingest(events[:5])

        entry = batched.ingest(events[12:])

        assert entry.lost_count == 7
        assert batched == fold_singly(events[:5] + events[12:])

    def test_a_hole_inside_a_run_goes_unnoticed(self) -> None:
        """Pinning an accepted risk, not a wanted behaviour. A run is trusted
        to be contiguous because a ring holds consecutive records; only a read
        torn by two collections landing inside one ~1 KB copy could break that.
        The ends still give the right counts, but nothing carries the hole's
        pause, so ADR-0015's invariant does not hold."""
        events = build_run(10)
        torn = events[:4] + events[6:]
        batched = RingAccumulator()

        entry = batched.ingest(torn)

        assert entry.lost_count == 0
        assert batched.exact_count - batched.sampled_count == 2
        assert batched.exact_pause_ns > batched.sampled_pause_ns
