"""Tests for Stats class."""

from __future__ import annotations

import math
from unittest.mock import MagicMock

import pytest

from gcmon.model.data import GCStatsInfo
from gcmon.stats.metrics import PAUSE_KEY
from gcmon.stats.stats import HAS_DDSKETCH, Stats
from gcmon.stats.streaming_stats import StreamingStats
from tests.helpers import create_mock_stats_item, proc

# The process whose figures these tests read.
TARGET_PID: int = 1

# A second process, so a total cannot pass by covering one.
OTHER_PID: int = 2

# A pid the statistics never took a ring for.
UNKNOWN_PID: int = 9999

PAUSE_NS: int = 1_000
"""One pause's length. The expected totals below are multiples of it."""


def _pause(ns: int = PAUSE_NS, iid: int = 0, heap_size: int | None = None) -> GCStatsInfo:
    """A gen-0 pause *ns* long, starting at zero.

    These tests count pauses and sum their lengths, so nothing else about
    the record carries a claim."""
    if heap_size is None:
        return create_mock_stats_item(iid=iid, gen=0, ts_start=0, ts_stop=ns)
    return create_mock_stats_item(iid=iid, gen=0, ts_start=0, ts_stop=ns, heap_size=heap_size)


class TestStatsUpdate:
    """Tests for Stats.update method."""

    def test_update_counts_the_value(self, stats: Stats) -> None:
        stats.update(100.0)

        assert stats.count() == 1

    def test_a_second_update_counts_both(self, stats: Stats) -> None:
        stats.update(100.0)

        stats.update(200.0)

        assert stats.count() == 2

    def test_update_sums_the_value(self, stats: Stats) -> None:
        stats.update(100.0)

        assert stats.sum() == 100.0

    def test_a_second_update_adds_to_the_sum(self, stats: Stats) -> None:
        stats.update(100.0)

        stats.update(250.0)

        assert stats.sum() == 350.0

    def test_update_appends_to_buffer(self, stats: Stats) -> None:
        stats.update(42.0)
        assert len(stats.buffer) == 1
        assert 42.0 in stats.buffer

    @pytest.mark.skipif(not HAS_DDSKETCH, reason="ddsketch not installed")
    def test_update_with_sketch(self, stats: Stats) -> None:
        assert stats.has_sketch
        mock_sketch = MagicMock()
        stats._sketch = mock_sketch

        stats.update(150.0)
        mock_sketch.add.assert_called_once_with(150.0)

    def test_update_without_sketch(self, stats_without_ddsketch: Stats) -> None:
        assert not stats_without_ddsketch.has_sketch
        stats_without_ddsketch.update(150.0)
        assert stats_without_ddsketch.count() == 1
        assert stats_without_ddsketch.sum() == 150.0


class TestStatsMaterialize:
    """Tests for Stats.materialize method."""

    def test_materialize_computes_percentiles(self, stats_with_data: Stats) -> None:
        stats_with_data.materialize()
        p = stats_with_data.percentiles
        assert p is not None
        assert 50 in p
        assert 90 in p
        assert 95 in p
        assert 99 in p

    def test_materialize_percentile_values_correct(self, stats_with_data: Stats) -> None:
        stats_with_data.materialize()
        p = stats_with_data.percentiles
        assert p is not None
        assert p[50] == 300.0

    def test_materialize_clears_buffer(self, stats_with_data: Stats) -> None:
        assert len(stats_with_data.buffer) == 5
        stats_with_data.materialize()
        assert len(stats_with_data.buffer) == 0

    @pytest.mark.skipif(not HAS_DDSKETCH, reason="ddsketch not installed")
    def test_materialize_disables_sketch(self, stats: Stats) -> None:
        for i in range(10):
            stats.update(float(i))
        assert stats.has_sketch
        stats.materialize()
        assert not stats.has_sketch

    def test_update_after_materialize_raises(self, stats_with_data: Stats) -> None:
        """Settling is final. Taking values again would leave `count` and `sum`
        covering the whole run while the percentiles beside them covered only
        what followed, and no column says which."""
        stats_with_data.materialize()

        with pytest.raises(RuntimeError):
            stats_with_data.update(600.0)

    def test_materialize_called_twice_is_noop(self, stats_with_data: Stats) -> None:
        stats_with_data.materialize()
        assert stats_with_data.percentiles is not None
        first_percentiles = stats_with_data.percentiles.copy()
        stats_with_data.materialize()
        assert stats_with_data.percentiles == first_percentiles

    def test_materialize_empty_stats(self, stats: Stats) -> None:
        assert stats.count() == 0
        stats.materialize()
        assert stats.percentiles is None


class TestStatsAverage:
    """Tests for Stats.average method."""

    def test_average_empty(self, stats: Stats) -> None:
        assert stats.average() == 0.0

    def test_average_single_value(self, stats: Stats) -> None:
        stats.update(42.0)
        assert stats.average() == 42.0

    def test_average_multiple_values(self, stats: Stats) -> None:
        stats.update(100.0)
        stats.update(200.0)
        stats.update(300.0)
        assert stats.average() == 200.0


class TestStatsPercentile:
    """Tests for Stats.percentile method."""

    def test_percentile_from_materialized(self, stats_with_data: Stats) -> None:
        stats_with_data.materialize()
        assert stats_with_data.percentile(50) == 300.0

    def test_percentile_unknown_returns_zero_after_materialize(self, stats_with_data: Stats) -> None:
        stats_with_data.materialize()
        assert stats_with_data.percentile(25) == 0.0

    def test_percentile_from_buffer(self, stats: Stats) -> None:
        for v in [10.0, 20.0, 30.0, 40.0, 50.0]:
            stats.update(v)
        p50 = stats.percentile(50)
        assert abs(p50 - 30.0) < 1e-10

    @pytest.mark.skipif(not HAS_DDSKETCH, reason="ddsketch not installed")
    def test_percentile_from_sketch_when_buffer_full(self, stats: Stats) -> None:
        """The buffer holds the long tail alone, and its median is the tail's."""
        for _ in range(3 * Stats.MAX_BUFFER_LEN):
            stats.update(1.0)
        for _ in range(Stats.MAX_BUFFER_LEN):
            stats.update(1000.0)

        p50 = stats.percentile(50)

        assert p50 == pytest.approx(1.0, rel=0.01)

    def test_percentile_empty_returns_zero(self, stats: Stats) -> None:
        assert stats.percentile(50) == 0.0

    def test_percentile_single_value_buffer(self, stats: Stats) -> None:
        stats.update(42.0)
        assert stats.percentile(50) == 42.0
        assert stats.percentile(0) == 42.0
        assert stats.percentile(100) == 42.0

    def test_percentile_unknown_returns_zero_for_arbitrary_value(self, stats_with_data: Stats) -> None:
        stats_with_data.materialize()
        assert stats_with_data.percentile(33) == 0.0


class TestStatsCountAndSum:
    """Tests for Stats.count and Stats.sum methods."""

    def test_count_initial(self, stats: Stats) -> None:
        assert stats.count() == 0

    def test_count_after_updates(self, stats: Stats) -> None:
        for i in range(5):
            stats.update(float(i))
        assert stats.count() == 5

    def test_sum_initial(self, stats: Stats) -> None:
        assert stats.sum() == 0.0

    def test_sum_after_updates(self, stats: Stats) -> None:
        stats.update(10.0)
        stats.update(20.0)
        stats.update(30.0)
        assert stats.sum() == 60.0


class TestStatsBufferLimit:
    """Tests for Stats buffer size limit."""

    def test_buffer_respects_maxlen(self, stats: Stats) -> None:
        for i in range(Stats.MAX_BUFFER_LEN + 1000):
            stats.update(float(i))
        assert len(stats.buffer) == Stats.MAX_BUFFER_LEN

    def test_buffer_count_not_affected_by_limit(self, stats: Stats) -> None:
        total_updates = Stats.MAX_BUFFER_LEN + 500
        for _ in range(total_updates):
            stats.update(1.0)
        assert stats.count() == total_updates

    def test_buffer_sum_not_affected_by_limit(self, stats: Stats) -> None:
        total_updates = Stats.MAX_BUFFER_LEN + 500
        for _ in range(total_updates):
            stats.update(2.0)
        assert stats.sum() == float(total_updates) * 2.0


class TestStatsNonNumbers:
    def test_update_nan(self, stats: Stats) -> None:
        stats.update(float("nan"))
        assert stats.count() == 1
        assert math.isnan(stats.sum())

    def test_update_inf_without_ddsketch(self, stats_without_ddsketch: Stats) -> None:
        stats_without_ddsketch.update(float("inf"))
        assert stats_without_ddsketch.count() == 1
        assert stats_without_ddsketch.sum() == float("inf")
        assert stats_without_ddsketch.average() == float("inf")


class TestStatsPercentileValidation:
    """Tests for Stats.percentile input validation (BUG-32)."""

    def test_negative_raises_value_error(self, stats_with_data: Stats) -> None:
        with pytest.raises(ValueError, match=r"percentile must be in \[0, 100\]"):
            stats_with_data.percentile(-1)

    def test_zero_is_valid(self, stats_with_data: Stats) -> None:
        result = stats_with_data.percentile(0)
        assert result == 100.0

    def test_hundred_is_valid(self, stats_with_data: Stats) -> None:
        result = stats_with_data.percentile(100)
        assert result == 500.0

    def test_above_hundred_raises_value_error(self, stats_with_data: Stats) -> None:
        with pytest.raises(ValueError, match=r"percentile must be in \[0, 100\]"):
            stats_with_data.percentile(101)

    def test_far_above_hundred_raises_value_error(self, stats_with_data: Stats) -> None:
        with pytest.raises(ValueError, match=r"percentile must be in \[0, 100\]"):
            stats_with_data.percentile(9999)

    def test_negative_raises_on_materialized_stats(self, stats_with_data: Stats) -> None:
        stats_with_data.materialize()
        with pytest.raises(ValueError, match=r"percentile must be in \[0, 100\]"):
            stats_with_data.percentile(-50)

    def test_above_hundred_raises_on_materialized_stats(self, stats_with_data: Stats) -> None:
        stats_with_data.materialize()
        with pytest.raises(ValueError, match=r"percentile must be in \[0, 100\]"):
            stats_with_data.percentile(150)

    def test_negative_raises_on_empty_stats(self, stats: Stats) -> None:
        with pytest.raises(ValueError, match=r"percentile must be in \[0, 100\]"):
            stats.percentile(-1)


class TestExactTotals:
    """Loss arrives per poll, so the exact totals follow from ADR-0015's
    invariant: what gcmon saw plus what the target's counters say it missed."""

    def _stats(self, sampled: int = 3, lost: int = 7) -> StreamingStats:
        stats = StreamingStats()
        for _ in range(sampled):
            stats.update(proc(TARGET_PID), _pause())
        stats.record_loss(proc(TARGET_PID), 0, 0, lost, lost * 1_000)
        return stats

    def test_exact_is_sampled_plus_lost(self) -> None:
        stats = self._stats()

        assert stats.pause_totals(proc(TARGET_PID), 0, 0).exact_count == 10
        assert stats.pause_totals(proc(TARGET_PID), 0, 0).exact_pause_ns == 10_000

    def test_coverage_and_scale_agree_with_the_totals(self) -> None:
        stats = self._stats()

        assert stats.pause_totals(proc(TARGET_PID), 0, 0).coverage == pytest.approx(0.3)
        assert stats.pause_totals(proc(TARGET_PID), 0, 0).scale_factor == pytest.approx(10 / 3)

    def test_the_scale_follows_pause_time_and_not_the_count(self) -> None:
        """One lost collection as long as the three read between them: a
        quarter of the count and half of the pause."""
        stats = StreamingStats()
        for _ in range(3):
            stats.update(proc(TARGET_PID), _pause())

        stats.record_loss(proc(TARGET_PID), 0, 0, 1, 3 * PAUSE_NS)

        assert stats.pause_totals(proc(TARGET_PID), 0, 0).scale_factor == pytest.approx(2.0)

    def test_an_untouched_generation_is_neutral(self) -> None:
        """1.0 rather than a division by zero, so no call site has to guard."""
        stats = StreamingStats()

        assert stats.pause_totals(proc(TARGET_PID), 0, 2).coverage == 1.0
        assert stats.pause_totals(proc(TARGET_PID), 0, 2).scale_factor == 1.0
        assert stats.pause_totals(proc(TARGET_PID), 0, 2).exact_count == 0

    def test_a_lossless_run_reports_full_coverage(self) -> None:
        stats = StreamingStats()
        stats.update(proc(TARGET_PID), _pause())

        assert stats.pause_totals(proc(TARGET_PID), 0, 0).coverage == 1.0
        assert stats.pause_totals(proc(TARGET_PID), 0, 0).exact_count == 1

    def test_totals_span_every_pid(self) -> None:
        stats = self._stats()
        stats.update(proc(OTHER_PID), _pause())
        stats.record_loss(proc(OTHER_PID), 0, 0, 1, 1_000)

        assert stats.pause_totals_by_gen()[0].exact_count == 12
        assert stats.pause_totals(proc(OTHER_PID), 0, 0).exact_count == 2

    def test_loss_survives_a_pid_the_monitor_forgets(self) -> None:
        """Recorded per poll rather than flushed at the end, so a child that
        exits mid-run still counts."""
        stats = self._stats()

        stats.materialize(proc(TARGET_PID))

        assert stats.pause_totals_by_gen()[0].exact_count == 10
        assert stats.pause_totals(proc(TARGET_PID), 0, 0).lost_count == 7


class TestTotalsLeaveTheAccumulatorBehind:
    """`StreamingStats` keeps its accumulators and answers from copies of
    them, so a caller writing to what it got back changes nothing."""

    def test_one_pid_gets_an_answer_rather_than_the_slot(self) -> None:
        stats = StreamingStats()
        stats.record_loss(proc(TARGET_PID), 0, 0, 7, 7_000)

        totals = stats.pause_totals(proc(TARGET_PID), 0, 0)
        with pytest.raises(AttributeError):
            totals.lost_count = 99  # type: ignore[misc]

        assert (totals.lost_count, totals.lost_pause_ns) == (7, 7_000)
        assert stats.pause_totals(proc(TARGET_PID), 0, 0).lost_count == 7

    def test_every_pid_gets_one_too(self) -> None:
        stats = StreamingStats()
        stats.record_loss(proc(TARGET_PID), 0, 0, 7, 7_000)
        stats.record_loss(proc(OTHER_PID), 0, 0, 1, 1_000)

        with pytest.raises(AttributeError):
            stats.pause_totals_by_gen()[0].lost_count = 99  # type: ignore[misc]

        assert stats.pause_totals_by_gen()[0].lost_count == 8

    def test_an_untouched_key_answers_zero(self) -> None:
        stats = StreamingStats()

        assert stats.pause_totals(proc(TARGET_PID), 0, 0).lost_count == 0
        assert stats.pause_totals_by_gen()[2].lost_pause_ns == 0

    def test_polls_still_accumulate(self) -> None:
        stats = StreamingStats()
        stats.record_loss(proc(TARGET_PID), 0, 0, 3, 3_000)
        stats.record_loss(proc(TARGET_PID), 0, 0, 4, 4_000)

        assert stats.pause_totals(proc(TARGET_PID), 0, 0).lost_count == 7
        assert stats.pause_totals(proc(TARGET_PID), 0, 0).lost_pause_ns == 7_000

    def test_cumulative_reads_hand_back_scratch(self) -> None:
        """`CumulativeCounters` is the accumulator a fold adds into, so this side
        cannot be frozen the way the pause side is. The fold still sums into
        a fresh one, which is what the write below lands on."""
        stats = StreamingStats()
        stats.observe_cumulative(proc(TARGET_PID), 0, 0, 40, 4.0)
        stats.observe_cumulative(proc(TARGET_PID), 1, 0, 2, 0.5)

        stats.cumulative_totals_by_gen()[0].add(99, 9.0)

        assert stats.cumulative_totals_by_gen()[0].collections == 42
        assert stats.cumulative_totals_by_gen()[0].pause_ns == 4_500_000_000


class TestLowCoverage:
    """Which ring gcmon read too little of, and how little.

    The answer is three numbers, so nothing here reads a log: wording it and
    saying it once belong to the monitor, in `test_monitor_coverage.py`.
    """

    def _sampled(self, stats: StreamingStats, count: int, iid: int = 0) -> None:
        for _ in range(count):
            stats.update(proc(TARGET_PID), _pause(iid=iid))

    def test_it_names_the_ring_and_its_coverage(self) -> None:
        stats = StreamingStats()
        self._sampled(stats, 3)

        stats.record_loss(proc(TARGET_PID), 0, 0, 7, 7_000)

        low = stats.low_coverage(proc(TARGET_PID))
        assert low is not None
        iid, gen, coverage = low
        assert (iid, gen) == (0, 0)
        assert coverage == pytest.approx(0.3)

    def test_a_starved_interpreter_answers_beside_a_covered_one(self) -> None:
        """The blended figure clears the floor, so a per-process key kept
        this quiet."""
        stats = StreamingStats()
        self._sampled(stats, 99, iid=0)
        self._sampled(stats, 2, iid=1)
        stats.record_loss(proc(TARGET_PID), 1, 0, 8, 8_000)

        assert stats.pause_totals_by_gen()[0].coverage > StreamingStats.COVERAGE_ADVISORY

        low = stats.low_coverage(proc(TARGET_PID))
        assert low is not None
        iid, gen, coverage = low
        assert (iid, gen) == (1, 0)
        assert coverage == pytest.approx(0.2)

    def test_it_answers_with_the_worst_ring_rather_than_the_first(self) -> None:
        """The caller says it once, so a marginal figure must not stand for a
        capture holding an interpreter at a tenth of its collections."""
        stats = StreamingStats()
        self._sampled(stats, 87, iid=0)
        stats.record_loss(proc(TARGET_PID), 0, 0, 13, 13_000)
        self._sampled(stats, 1, iid=1)
        stats.record_loss(proc(TARGET_PID), 1, 0, 19, 19_000)

        low = stats.low_coverage(proc(TARGET_PID))
        assert low is not None
        iid, gen, coverage = low
        assert (iid, gen) == (1, 0), "interpreter 0 dipped first and is the milder of the two"
        assert coverage == pytest.approx(0.05)

    def test_the_worst_ring_wins_whichever_order_the_loss_arrived_in(self) -> None:
        stats = StreamingStats()
        self._sampled(stats, 1, iid=1)
        stats.record_loss(proc(TARGET_PID), 1, 0, 19, 19_000)
        self._sampled(stats, 87, iid=0)
        stats.record_loss(proc(TARGET_PID), 0, 0, 13, 13_000)

        low = stats.low_coverage(proc(TARGET_PID))
        assert low is not None
        assert low[:2] == (1, 0)

    def test_a_covered_interpreter_does_not_answer_for_a_starved_one(self) -> None:
        stats = StreamingStats()
        self._sampled(stats, 99, iid=0)
        self._sampled(stats, 2, iid=1)
        stats.record_loss(proc(TARGET_PID), 0, 0, 1, 1_000)

        assert stats.low_coverage(proc(TARGET_PID)) is None

    def test_a_covered_run_answers_nothing(self) -> None:
        stats = StreamingStats()
        self._sampled(stats, 99)

        stats.record_loss(proc(TARGET_PID), 0, 0, 1, 1_000)

        assert stats.pause_totals(proc(TARGET_PID), 0, 0).coverage > StreamingStats.COVERAGE_ADVISORY
        assert stats.low_coverage(proc(TARGET_PID)) is None

    def test_a_run_that_lost_nothing_answers_nothing(self) -> None:
        """The shortcut the check leads with: a ring that lost nothing is
        fully covered, whatever its sample size."""
        stats = StreamingStats()
        self._sampled(stats, 3)

        assert stats.low_coverage(proc(TARGET_PID)) is None

    def test_a_loss_entry_with_no_count_is_skipped(self) -> None:
        """`pyperf.hook` records a ring that lost pause time but no collections.
        Nothing was missed there, so it is not a ring gcmon read too little of."""
        stats = StreamingStats()
        self._sampled(stats, 3)

        stats.record_loss(proc(TARGET_PID), 0, 0, 0, 7_000)

        assert stats.low_coverage(proc(TARGET_PID)) is None

    def test_one_pids_loss_does_not_answer_for_another(self) -> None:
        stats = StreamingStats()
        self._sampled(stats, 3)

        stats.record_loss(proc(OTHER_PID), 0, 0, 7, 7_000)

        assert stats.low_coverage(proc(TARGET_PID)) is None
        assert stats.low_coverage(proc(OTHER_PID)) == (0, 0, 0.0), "pid 2 sampled nothing of what it lost"

    def test_asking_twice_answers_twice(self) -> None:
        """No latch of its own: every poll asks, and a second reader must not
        be told a blind run is healthy because the first one asked first."""
        stats = StreamingStats()
        self._sampled(stats, 3)
        stats.record_loss(proc(TARGET_PID), 0, 0, 7, 7_000)

        first = stats.low_coverage(proc(TARGET_PID))
        assert first is not None
        assert stats.low_coverage(proc(TARGET_PID)) == first


class TestCumulativeCounters:
    def test_summed_across_interpreters(self) -> None:
        stats = StreamingStats()
        stats.observe_cumulative(proc(TARGET_PID), 0, 0, 500, 0.5)
        stats.observe_cumulative(proc(TARGET_PID), 1, 0, 300, 0.3)

        assert stats.cumulative_totals_by_gen()[0].collections == 800
        assert stats.cumulative_totals_by_gen()[0].pause_ns == 800_000_000

    def test_the_newest_value_replaces_the_last(self) -> None:
        """Cumulative in the target, so polls report a running total, not a
        delta -- adding them would count every collection many times."""
        stats = StreamingStats()
        stats.observe_cumulative(proc(TARGET_PID), 0, 0, 500, 0.5)
        stats.observe_cumulative(proc(TARGET_PID), 0, 0, 900, 0.9)

        assert stats.cumulative_totals_by_gen()[0].collections == 900

    def test_it_can_exceed_the_observed_span(self) -> None:
        """The point of reporting it: what ran before gcmon attached is not
        loss, and must not touch `Cov`."""
        stats = StreamingStats()
        stats.update(proc(TARGET_PID), _pause())
        stats.observe_cumulative(proc(TARGET_PID), 0, 0, 5_000, 5.0)

        assert stats.cumulative_totals_by_gen()[0].collections == 5_000
        assert stats.pause_totals(proc(TARGET_PID), 0, 0).exact_count == 1
        assert stats.pause_totals(proc(TARGET_PID), 0, 0).coverage == 1.0


class TestTwoInterpretersOfOnePid:
    """The arithmetic the old key could not carry.

    Every statistics test drove one interpreter, and on that input a
    per-process figure and a per-ring one are the same number.
    """

    def _stats(self) -> StreamingStats:
        """Nine records read of ten on iid 0, one of ten on iid 1. Blended,
        the pid reads 50%."""
        stats = StreamingStats()
        for _ in range(9):
            stats.update(proc(TARGET_PID), _pause())
        stats.record_loss(proc(TARGET_PID), 0, 0, 1, 1_000)

        stats.update(proc(TARGET_PID), _pause(5_000, iid=1))
        stats.record_loss(proc(TARGET_PID), 1, 0, 9, 45_000)
        return stats

    def test_each_interpreter_reports_its_own_coverage(self) -> None:
        stats = self._stats()

        assert stats.pause_totals(proc(TARGET_PID), 0, 0).coverage == pytest.approx(0.9)
        assert stats.pause_totals(proc(TARGET_PID), 1, 0).coverage == pytest.approx(0.1)

    def test_the_sampled_durations_stay_apart(self) -> None:
        stats = self._stats()

        assert stats.pause_totals(proc(TARGET_PID), 0, 0).sampled_pause_ns == 9_000
        assert stats.pause_totals(proc(TARGET_PID), 1, 0).sampled_pause_ns == 5_000

    def test_the_run_still_folds_to_one_answer(self) -> None:
        """The key separates rings; a roll-up over all of them is one number,
        and `Total` prints it."""
        stats = self._stats()
        totals = stats.pause_totals_by_gen()[0]

        assert (totals.sampled_count, totals.lost_count) == (10, 10)
        assert totals.coverage == pytest.approx(0.5)

    def test_the_two_rings_are_two_entries(self) -> None:
        stats = self._stats()

        assert stats.rings() == [(proc(TARGET_PID), 0), (proc(TARGET_PID), 1)]


class TestAProcessThatExits:
    """gcmon settles a ring when its process goes, and never before."""

    def _ran_and_exited(self) -> StreamingStats:
        """Ring (1, 0) with three records, its process gone."""
        stats = StreamingStats()
        for _ in range(3):
            stats.update(proc(TARGET_PID), _pause())
        stats.materialize(proc(TARGET_PID))
        return stats

    def test_the_ring_keeps_its_row(self) -> None:
        stats = self._ran_and_exited()

        assert stats.rings() == [(proc(TARGET_PID), 0)]

    def test_the_percentiles_cover_the_whole_life(self) -> None:
        stats = self._ran_and_exited()
        ring = stats.get_ring_stats(proc(TARGET_PID), 0)

        assert ring is not None
        assert ring[PAUSE_KEY][0].percentiles == {50: 1_000, 90: 1_000, 95: 1_000, 99: 1_000}

    def test_count_and_sum_survive(self) -> None:
        totals = self._ran_and_exited().pause_totals(proc(TARGET_PID), 0, 0)

        assert (totals.sampled_count, totals.sampled_pause_ns) == (3, 3_000)

    def test_a_running_ring_stays_open(self) -> None:
        """Only the pid that went is settled."""
        stats = StreamingStats()
        stats.update(proc(TARGET_PID), _pause())
        stats.update(proc(OTHER_PID), _pause())

        stats.materialize(proc(TARGET_PID))

        running = stats.get_ring_stats(proc(OTHER_PID), 0)
        assert running is not None
        assert running[PAUSE_KEY][0].percentiles is None

    def test_retain_settles_the_pids_it_leaves_out(self) -> None:
        stats = StreamingStats()
        stats.update(proc(TARGET_PID), _pause())
        stats.update(proc(OTHER_PID), _pause())

        stats.retain({proc(OTHER_PID)})

        assert stats._open_processes == {proc(OTHER_PID)}
        assert set(stats._running_rings) == {(proc(OTHER_PID), 0)}

    def test_every_interpreter_of_the_pid_settles(self) -> None:
        stats = StreamingStats()
        stats.update(proc(TARGET_PID), _pause())
        stats.update(proc(TARGET_PID), _pause(iid=1))

        stats.materialize(proc(TARGET_PID))

        assert stats._running_rings == {}
        assert stats.rings() == [(proc(TARGET_PID), 0), (proc(TARGET_PID), 1)]

    def test_the_exit_hands_the_slot_back(self) -> None:
        """A target that spawns and exits keeps every row it earned. The bound
        counts the interpreters running, so the dead ones cost no slot."""
        stats = StreamingStats()
        for pid in range(StreamingStats.MAX_ACTIVE_RINGS * 2):
            stats.update(proc(pid), _pause())
            stats.materialize(proc(pid))

        assert len(stats.rings()) == StreamingStats.MAX_ACTIVE_RINGS * 2
        assert stats.untracked_rings() == 0


class TestAnOpenPidHoldsARing:
    """`retain` finds the rings of the departed by scanning `_running_rings`,
    so a pid that opened without one would go unsettled and count as departed
    on every later tick. Every path that opens a pid opens a ring with it."""

    def test_each_path_that_opens_a_pid_opens_a_ring(self) -> None:
        stats = StreamingStats()
        stats.update(proc(TARGET_PID), _pause())
        stats.record_loss(proc(OTHER_PID), 0, 0, 4, 400)
        stats.observe_cumulative(proc(3), 0, 0, 10, 0.5)

        assert (
            stats._open_processes
            == {process for process, _ in stats._running_rings}
            == {proc(TARGET_PID), proc(OTHER_PID), proc(3)}
        )


class TestAFanOutThatDeparts:
    """A tick where many pids leave at once."""

    PIDS = tuple(range(1, 101))
    IIDS = (0, 1, 2)
    # 300 rings against MAX_ACTIVE_RINGS: the tail is declined, and the
    # comparison covers departing pids whose rings hold no buffers.
    SURVIVORS = frozenset({1, 100})
    SURVIVING = frozenset({proc(pid) for pid in SURVIVORS})

    def _fan_out(self) -> StreamingStats:
        """A hundred pids, three interpreters each, every ring still running."""
        stats = StreamingStats()
        for pid in self.PIDS:
            for iid in self.IIDS:
                stats.update(proc(pid), _pause(PAUSE_NS * pid, iid=iid))
                stats.record_loss(proc(pid), iid, 0, pid, 100 * pid)
                stats.observe_cumulative(proc(pid), iid, 0, 10 * pid, 0.5)
        return stats

    def _state(self, stats: StreamingStats) -> object:
        """Everything settling a pid touches, as one comparable value."""
        return (
            set(stats._open_processes),
            sorted(stats._running_rings),
            sorted(stats._settled_rings),
            stats._admitted_rings,
            {
                key: (
                    ring.declined,
                    ring.metrics is not None,
                    None if ring.metrics is None else ring.metrics[PAUSE_KEY][0].percentiles,
                    {gen: (loss.count, loss.pause_ns) for gen, loss in ring.loss.items()},
                    {gen: (totals.collections, totals.duration_s) for gen, totals in ring.cumulative.items()},
                )
                for key, ring in stats._keyed_rings()
            },
        )

    def test_one_pass_leaves_what_the_per_pid_path_leaves(self) -> None:
        one_pass, per_pid = self._fan_out(), self._fan_out()

        one_pass.retain(self.SURVIVING)
        for pid in self.PIDS:
            if pid not in self.SURVIVORS:
                per_pid.materialize(proc(pid))

        assert self._state(one_pass) == self._state(per_pid)

    def test_a_successor_on_a_reused_pid_files_apart_from_its_predecessor(self) -> None:
        """The equivalence above compares two paths through one body. This pins
        what a settled ring keeps once the pid comes back."""
        stats = self._fan_out()

        stats.retain(self.SURVIVING)
        stats.update(proc(3, 2), _pause(7_000))

        assert stats.pause_totals(proc(3, 1), 0, 0).sampled_pause_ns == 3_000
        assert stats.pause_totals(proc(3, 2), 0, 0).sampled_pause_ns == 7_000

    def test_a_pid_whose_rings_interleave_settles_in_one_go(self) -> None:
        """A ring is keyed by its first record, so another pid's ring can sit
        between two of a pid's own. Grouping the departed by adjacency would
        settle such a pid once per run of its keys, leaving the rest of its
        interpreters running."""
        stats = StreamingStats()
        for iid in self.IIDS:
            for pid in self.PIDS:
                stats.update(proc(pid), _pause(iid=iid))

        stats.retain(self.SURVIVING)

        assert sorted(stats._settled_rings) == [
            (proc(pid), iid) for pid in self.PIDS if pid not in self.SURVIVORS for iid in self.IIDS
        ]

    def test_the_survivors_keep_their_rings(self) -> None:
        """A whole-tree drop exercises neither the grouping nor the pids it has
        to leave alone."""
        stats = self._fan_out()

        stats.retain(self.SURVIVING)

        assert stats._open_processes == set(self.SURVIVING)
        assert set(stats._running_rings) == {(process, iid) for process in self.SURVIVING for iid in self.IIDS}

    def test_settling_the_same_pids_again_is_a_no_op(self) -> None:
        stats = self._fan_out()
        stats.retain(self.SURVIVING)
        settled = self._state(stats)

        stats.retain(self.SURVIVING)

        assert self._state(stats) == settled

    def test_a_pid_already_settled_costs_nothing(self) -> None:
        """One pid `retain` already settled, and one gcmon never saw."""
        stats = self._fan_out()
        stats.retain(self.SURVIVING)
        settled = self._state(stats)

        stats.materialize(proc(self.PIDS[5]))
        stats.materialize(proc(UNKNOWN_PID))

        assert self._state(stats) == settled

    def test_a_tick_where_nothing_departed_settles_nothing(self) -> None:
        stats = self._fan_out()
        running = self._state(stats)

        stats.retain({proc(pid) for pid in self.PIDS})

        assert self._state(stats) == running

    def test_a_declined_ring_hands_back_no_slot(self) -> None:
        """The bound counts rings holding buffers."""
        stats = StreamingStats()
        for pid in range(StreamingStats.MAX_ACTIVE_RINGS + 4):
            stats.update(proc(pid), _pause())
        assert stats.untracked_rings() == 4

        stats.retain(set())

        assert stats._admitted_rings == 0

    def test_the_slots_a_departed_fan_out_frees_are_whole(self) -> None:
        stats = StreamingStats()
        for pid in range(StreamingStats.MAX_ACTIVE_RINGS + 4):
            stats.update(proc(pid), _pause())

        stats.retain(set())
        for pid in range(9_000, 9_000 + StreamingStats.MAX_ACTIVE_RINGS):
            stats.update(proc(pid), _pause())

        assert stats.untracked_rings() == 4
        assert stats.get_ring_stats(proc(9_000 + StreamingStats.MAX_ACTIVE_RINGS - 1), 0) is not None


class TestTheBoundOnRunningRings:
    """Rings arriving to a full set get no row, and their records still count."""

    def _full(self) -> StreamingStats:
        """Every slot taken by a ring still running, then one more ring."""
        stats = StreamingStats()
        for pid in range(StreamingStats.MAX_ACTIVE_RINGS):
            stats.update(proc(pid), _pause())
        stats.update(proc(UNKNOWN_PID), _pause(4_000))
        return stats

    def test_the_ring_gets_no_row(self) -> None:
        stats = self._full()

        assert stats.get_ring_stats(proc(UNKNOWN_PID), 0) is None
        assert (proc(UNKNOWN_PID), 0) not in stats.rings()

    def test_it_is_counted_as_untracked(self) -> None:
        assert self._full().untracked_rings() == 1

    def test_a_repeat_record_counts_the_ring_once(self) -> None:
        stats = self._full()
        stats.update(proc(UNKNOWN_PID), _pause(4_000))

        assert stats.untracked_rings() == 1

    def test_the_records_reach_the_run_totals(self) -> None:
        """`Total` is fed once per record whatever the table can hold, so the
        run's cost is whole even where its detail is not."""
        totals = self._full().pause_totals_by_gen()[0]

        assert totals.sampled_count == StreamingStats.MAX_ACTIVE_RINGS + 1
        assert totals.sampled_pause_ns == StreamingStats.MAX_ACTIVE_RINGS * 1_000 + 4_000

    def test_no_row_loses_its_place_to_the_newcomer(self) -> None:
        stats = self._full()

        assert len(stats.rings()) == StreamingStats.MAX_ACTIVE_RINGS

    def test_a_freed_slot_does_not_open_a_row_halfway_through(self) -> None:
        """The ring has been running unrecorded, so a row opened now would
        cover its tail and read as its whole life."""
        stats = self._full()
        stats.materialize(proc(0))

        stats.update(proc(UNKNOWN_PID), _pause(4_000))

        assert stats.get_ring_stats(proc(UNKNOWN_PID), 0) is None

    def test_the_advisory_passes_over_a_ring_with_no_row(self) -> None:
        """Its sampled count reads zero, which is not what gcmon observed."""
        stats = self._full()
        stats.record_loss(proc(UNKNOWN_PID), 0, 0, 99, 99_000)

        assert stats.low_coverage(proc(UNKNOWN_PID)) is None

    def test_its_loss_still_reaches_the_run_totals(self) -> None:
        """The sample buffers are what the bound withholds. Counters cost four
        numbers a generation, so a declined ring keeps them and `Cov` under
        `Total` stays honest."""
        stats = self._full()
        stats.record_loss(proc(UNKNOWN_PID), 0, 0, 99, 99_000)

        assert stats.pause_totals_by_gen()[0].lost_count == 99

    def test_its_cumulative_counters_still_reach_the_note(self) -> None:
        stats = self._full()
        stats.observe_cumulative(proc(UNKNOWN_PID), 0, 0, 400, 0.4)

        assert stats.cumulative_totals_by_gen()[0].collections == 400

    def test_a_successor_on_a_declined_pid_can_get_a_row(self) -> None:
        """The decline was made against the process that held the pid, and it
        goes with the entry when that process exits. A successor arriving to a
        free slot has a whole life to record, so a row covers all of it."""
        stats = self._full()
        stats.materialize(proc(0))
        stats.materialize(proc(UNKNOWN_PID))

        stats.update(proc(UNKNOWN_PID, 2), _pause(4_000))

        assert stats.rings() == sorted(
            [*((proc(pid), 0) for pid in range(StreamingStats.MAX_ACTIVE_RINGS)), (proc(UNKNOWN_PID, 2), 0)]
        )
        assert stats.untracked_rings() == 1

    def test_a_ring_that_only_lost_gets_no_row(self) -> None:
        """`record_loss` opens an entry for its counters, which is not a row.
        Printing one would give an empty block a heading."""
        stats = StreamingStats()
        stats.record_loss(proc(7), 0, 0, 5, 5_000)

        assert stats.rings() == []


class TestADeathTheMonitorCalled:
    """`gcmon.monitoring.monitor` decides who is alive and this side takes the decision.

    Whatever arrives on a pid called dead is a new process, the same one or
    not, which `TestAReusedPid` covers figure by figure. What is left here is
    the case where the call was wrong, the one a reader is most likely to try
    to correct: the interpreter goes on running and its cumulative counter
    carries on past its predecessor's instead of restarting.
    """

    def _called_dead_but_running(self) -> StreamingStats:
        stats = StreamingStats()
        for _ in range(3):
            stats.update(proc(TARGET_PID), _pause())
        stats.record_loss(proc(TARGET_PID), 0, 0, 2, 2_000)
        stats.observe_cumulative(proc(TARGET_PID), 0, 0, 300, 0.3)

        stats.materialize(proc(TARGET_PID))

        for _ in range(2):
            stats.update(proc(TARGET_PID), _pause())
        stats.record_loss(proc(TARGET_PID), 0, 0, 1, 1_000)
        stats.observe_cumulative(proc(TARGET_PID), 0, 0, 500, 0.5)
        return stats

    def test_its_cumulative_counters_start_fresh(self) -> None:
        """The fold reads 800 over an interpreter that ran 500, and that is the
        decision rather than a slip. gcmon could tell the two apart here, since
        a real successor's counter restarts low, but only by deciding liveness
        a second way and disagreeing with the monitor whenever the two differ.
        ADR-0016 has the reasoning.
        """
        assert self._called_dead_but_running().cumulative_totals_by_gen()[0].collections == 800


class TestAReusedPid:
    """Two processes held the pid, so the run keeps two of everything.

    Merging them was the hazard: the figures read as one interpreter's history
    and belonged to two.
    """

    def _reused(self) -> StreamingStats:
        """A process on pid 1 exits, and a successor claims the pid.

        Each loses two records of ten, so their coverage figures are equal and
        a row printing one for the other cannot hide behind a coincidence.
        """
        stats = StreamingStats()
        for _ in range(8):
            stats.update(proc(TARGET_PID), _pause())
        stats.record_loss(proc(TARGET_PID), 0, 0, 2, 2_000)
        stats.observe_cumulative(proc(TARGET_PID), 0, 0, 400, 0.4)

        stats.materialize(proc(TARGET_PID))

        for _ in range(8):
            stats.update(proc(TARGET_PID, 2), _pause(9_000))
        stats.record_loss(proc(TARGET_PID, 2), 0, 0, 2, 18_000)
        stats.observe_cumulative(proc(TARGET_PID, 2), 0, 0, 10, 0.01)
        return stats

    def test_each_process_gets_a_block(self) -> None:
        assert self._reused().rings() == [(proc(TARGET_PID), 0), (proc(TARGET_PID, 2), 0)]

    def test_the_predecessor_keeps_its_own_durations(self) -> None:
        totals = self._reused().pause_totals(proc(TARGET_PID, 1), 0, 0)

        assert (totals.sampled_count, totals.sampled_pause_ns) == (8, 8_000)

    def test_the_successor_keeps_its_own(self) -> None:
        totals = self._reused().pause_totals(proc(TARGET_PID, 2), 0, 0)

        assert (totals.sampled_count, totals.sampled_pause_ns) == (8, 72_000)

    def test_the_predecessors_percentiles_stay_settled(self) -> None:
        ring = self._reused().get_ring_stats(proc(TARGET_PID, 1), 0)

        assert ring is not None
        assert ring[PAUSE_KEY][0].percentiles == {50: 1_000, 90: 1_000, 95: 1_000, 99: 1_000}

    def test_the_loss_splits_between_them(self) -> None:
        """`Cov` and `F` read this, so a shared entry would print one
        process's gaps against the other's records."""
        stats = self._reused()

        assert stats.pause_totals(proc(TARGET_PID, 1), 0, 0).lost_pause_ns == 2_000
        assert stats.pause_totals(proc(TARGET_PID, 2), 0, 0).lost_pause_ns == 18_000

    def test_the_cumulative_fold_adds_them(self) -> None:
        """The successor's counters are smaller and used to overwrite the
        predecessor's, so the folded total could fall mid-run."""
        assert self._reused().cumulative_totals_by_gen()[0].collections == 410

    def test_the_footnote_counts_two_processes(self) -> None:
        assert self._reused().cumulative_scope() == (2, 2)

    def test_neither_is_counted_as_untracked(self) -> None:
        assert self._reused().untracked_rings() == 0

    def test_the_run_totals_hold_both(self) -> None:
        totals = self._reused().pause_totals_by_gen()[0]

        assert (totals.sampled_count, totals.sampled_pause_ns) == (16, 80_000)


class TestASettledRingNeverReopens:
    """ADR-0016: a ring settles on the exit that ends it and never again.

    A successor on a reused pid re-reads the ring its predecessor left, and
    the monitor attributes those records to the process that produced them,
    so they arrive here after that process settled. They were counted when
    they first came, and counting them again would put the run totals and the
    ring's own figures out by a duplicate.
    """

    def _settled_then_late(self) -> StreamingStats:
        stats = StreamingStats()
        stats.update(proc(TARGET_PID), _pause(heap_size=4_000))
        stats.materialize(proc(TARGET_PID))

        stats.update(proc(TARGET_PID), _pause(9_000, heap_size=8_000))
        return stats

    def test_the_ring_keeps_one_row(self) -> None:
        """Reopening it filed a second entry under a key `_settled_rings`
        already held, so the block printed twice."""
        assert self._settled_then_late().rings() == [(proc(TARGET_PID), 0)]

    def test_the_ring_keeps_its_own_figures(self) -> None:
        assert self._settled_then_late().pause_totals(proc(TARGET_PID), 0, 0).sampled_pause_ns == 1_000

    def test_the_run_totals_keep_theirs(self) -> None:
        stats = self._settled_then_late()

        assert (stats.count(), stats.pause_totals_by_gen()[0].sampled_pause_ns) == (1, 1_000)

    def test_the_high_water_heap_size_is_left_alone(self) -> None:
        assert self._settled_then_late().heap_size_p99() == 4_000

    def test_a_process_that_is_still_running_takes_the_record(self) -> None:
        """The control: only a settled process turns one away."""
        stats = StreamingStats()
        stats.update(proc(TARGET_PID), _pause())
        stats.update(proc(TARGET_PID), _pause(9_000))

        assert stats.pause_totals(proc(TARGET_PID), 0, 0).sampled_count == 2
