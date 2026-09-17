"""Tests for streaming_stats module."""

from collections.abc import Callable

import numpy as np
import pytest

from gcmon.model.data import GCStatsInfo
from gcmon.model.protocol import TGCStatsInfo
from gcmon.stats.metrics import PAUSE_KEY
from gcmon.stats.stats import get_quantile_value
from gcmon.stats.streaming_stats import StreamingStats
from tests.conftest import DEFAULT_PID
from tests.helpers import proc

TOLERANCE = 1e-12

# How many values the list holds decides which pair of them a percentile
# interpolates between, so each length is asked for the percentiles that
# land on and between its own values.
_DATA_AND_PERCENTILES = [
    ([10.0, 20.0], [0, 50, 90, 95, 99, 100]),
    ([1.0, 2.0, 3.0], [0, 25, 50, 75, 90, 95, 99, 100]),
    ([float(v) for v in range(1, 11)], [0, 10, 25, 50, 75, 90, 95, 99, 100]),
]


def _percentile_cases() -> list[tuple[list[float], int]]:
    return [(data, p) for data, percentiles in _DATA_AND_PERCENTILES for p in percentiles]


class TestGetQuantileValue:
    """Tests for get_quantile_value function."""

    def test_empty(self) -> None:
        assert get_quantile_value([], 50) == 0.0
        assert get_quantile_value([], 90) == 0.0
        assert get_quantile_value([], 95) == 0.0
        assert get_quantile_value([], 99) == 0.0

    def test_single_element(self) -> None:
        assert get_quantile_value([42.0], 50) == 42.0
        assert get_quantile_value([42.0], 0) == 42.0
        assert get_quantile_value([42.0], 100) == 42.0
        assert get_quantile_value([42.0], 90) == 42.0
        assert get_quantile_value([42.0], 95) == 42.0
        assert get_quantile_value([42.0], 99) == 42.0

    @pytest.mark.parametrize("data, percentile", _percentile_cases())
    def test_matches_numpy_linear(self, data: list[float], percentile: int) -> None:
        expected = float(np.percentile(data, percentile, method="linear"))

        assert abs(get_quantile_value(data, percentile) - expected) < TOLERANCE

    @pytest.mark.parametrize("seed", range(20))
    @pytest.mark.parametrize("percentile", [5, 10, 25, 50, 75, 90, 95, 99])
    def test_random_data_matches_numpy(self, seed: int, percentile: int) -> None:
        values = sorted(np.random.default_rng(seed).uniform(0, 1000, size=500).tolist())
        expected = float(np.percentile(values, percentile, method="linear"))

        assert abs(get_quantile_value(values, percentile) - expected) < TOLERANCE


class TestStreamingStatsUpdate:
    """Tests for StreamingStats.update method."""

    def test_update_counts_the_record(
        self,
        streaming_stats: StreamingStats,
        mock_stats_item: TGCStatsInfo,
    ) -> None:
        streaming_stats.update(proc(DEFAULT_PID), mock_stats_item)

        assert streaming_stats.count() == 1

    def test_a_second_update_counts_both(
        self,
        streaming_stats: StreamingStats,
        mock_stats_item: TGCStatsInfo,
    ) -> None:
        streaming_stats.update(proc(DEFAULT_PID), mock_stats_item)

        streaming_stats.update(proc(DEFAULT_PID), mock_stats_item)

        assert streaming_stats.count() == 2

    def test_update_records_pause_metric(
        self,
        streaming_stats: StreamingStats,
        mock_stats_item: TGCStatsInfo,
    ) -> None:
        streaming_stats.update(proc(DEFAULT_PID), mock_stats_item)
        assert streaming_stats.metrics[PAUSE_KEY][0].count() == 1

    def test_update_records_incremental_metrics(
        self,
        streaming_stats: StreamingStats,
        incremental_gc_stats_item: GCStatsInfo,
    ) -> None:
        streaming_stats.update(proc(DEFAULT_PID), incremental_gc_stats_item)
        assert streaming_stats.metrics["mark_alive"][0].count() == 1
        assert streaming_stats.metrics["fill_increment"][0].count() == 1
        assert streaming_stats.metrics["deduce_unreachable"][0].count() == 1
        assert streaming_stats.metrics["handle_weakrefs"][0].count() == 1
        assert streaming_stats.metrics["finalize_garbage"][0].count() == 1
        assert streaming_stats.metrics["handle_resurrected"][0].count() == 1
        assert streaming_stats.metrics["clear_weakrefs"][0].count() == 1
        assert streaming_stats.metrics["delete_garbage"][0].count() == 1

    def test_update_skips_zero_duration(
        self,
        streaming_stats: StreamingStats,
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        item = gc_stats_item_factory(ts_start=1000, ts_stop=1000)
        streaming_stats.update(proc(DEFAULT_PID), item)
        assert streaming_stats.metrics[PAUSE_KEY][0].count() == 0

    def test_update_keeps_sub_microsecond_duration(
        self,
        streaming_stats: StreamingStats,
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        """Durations are stored in nanoseconds. They used to be truncated to
        microseconds on ingest, so a sub-microsecond phase counted as 0."""
        item = gc_stats_item_factory(ts_start=0, ts_stop=750)
        streaming_stats.update(proc(DEFAULT_PID), item)

        assert streaming_stats.metrics[PAUSE_KEY][0].count() == 1
        assert streaming_stats.metrics[PAUSE_KEY][0].sum() == 750

    def test_update_tracks_heap_size(
        self,
        streaming_stats: StreamingStats,
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        item1 = gc_stats_item_factory(heap_size=1_000_000)
        item2 = gc_stats_item_factory(heap_size=5_000_000)
        streaming_stats.update(proc(DEFAULT_PID), item1)
        streaming_stats.update(proc(DEFAULT_PID), item2)
        assert streaming_stats._heap_size[proc(DEFAULT_PID)] == 5_000_000

    def test_update_heap_size_is_max_per_pid(
        self,
        streaming_stats: StreamingStats,
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        item_small = gc_stats_item_factory(heap_size=100)
        item_large = gc_stats_item_factory(heap_size=500)
        streaming_stats.update(proc(DEFAULT_PID), item_large)
        streaming_stats.update(proc(DEFAULT_PID), item_small)
        assert streaming_stats._heap_size[proc(DEFAULT_PID)] == 500


class TestStreamingStatsRingTracking:
    """Tests for StreamingStats ring tracking."""

    def test_rings_returns_all_tracked_rings(self, streaming_stats_with_pids: StreamingStats) -> None:
        rings = streaming_stats_with_pids.rings()
        assert rings == [(proc(11111), 0), (proc(22222), 0), (proc(33333), 0)]

    def test_get_ring_stats_returns_active(self, streaming_stats_with_pids: StreamingStats) -> None:
        ring_stats = streaming_stats_with_pids.get_ring_stats(proc(11111), 0)
        assert ring_stats is not None
        assert PAUSE_KEY in ring_stats

    def test_get_ring_stats_returns_settled(
        self,
        streaming_stats: StreamingStats,
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        streaming_stats.update(proc(DEFAULT_PID), gc_stats_item_factory())
        streaming_stats.materialize(proc(DEFAULT_PID))

        ring_stats = streaming_stats.get_ring_stats(proc(DEFAULT_PID), 0)
        assert ring_stats is not None

    def test_get_ring_stats_missing_returns_none(self, streaming_stats: StreamingStats) -> None:
        assert streaming_stats.get_ring_stats(proc(99999), 0) is None

    def test_an_interpreter_of_a_known_pid_is_its_own_ring(
        self,
        streaming_stats: StreamingStats,
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        streaming_stats.update(proc(DEFAULT_PID), gc_stats_item_factory(iid=0))

        assert streaming_stats.get_ring_stats(proc(DEFAULT_PID), 1) is None

    def test_per_ring_pause_recorded_once(
        self,
        streaming_stats: StreamingStats,
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        """The per-ring 'pause' metric is recorded once per event, matching the
        global total. It used to be recorded twice, doubling Count/Sum/Avg in
        the per-ring rows of the --stats table."""
        streaming_stats.update(proc(DEFAULT_PID), gc_stats_item_factory(ts_start=1_000, ts_stop=6_000))

        ring_stats = streaming_stats.get_ring_stats(proc(DEFAULT_PID), 0)
        assert ring_stats is not None
        assert ring_stats[PAUSE_KEY][0].count() == 1
        assert ring_stats[PAUSE_KEY][0].sum() == 5_000
        assert ring_stats[PAUSE_KEY][0].count() == streaming_stats.metrics[PAUSE_KEY][0].count()
        assert ring_stats[PAUSE_KEY][0].sum() == streaming_stats.metrics[PAUSE_KEY][0].sum()

    def test_per_ring_metrics_match_totals_for_a_single_ring(
        self,
        streaming_stats: StreamingStats,
        incremental_gc_stats_item: GCStatsInfo,
    ) -> None:
        """With one interpreter of one PID, every per-ring metric equals the
        global total."""
        streaming_stats.update(proc(DEFAULT_PID), incremental_gc_stats_item)

        ring_stats = streaming_stats.get_ring_stats(proc(DEFAULT_PID), 0)
        assert ring_stats is not None
        for metric_key, gen_stats in streaming_stats.metrics.items():
            for gen, total in gen_stats.items():
                assert ring_stats[metric_key][gen].count() == total.count(), metric_key
                assert ring_stats[metric_key][gen].sum() == total.sum(), metric_key

    def test_every_metric_splits_between_two_interpreters(
        self,
        streaming_stats: StreamingStats,
        incremental_gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        """The sub-phase metrics ride the same key as `pause`, and nothing
        else reads them per ring. Two interpreters, so each ring holds one
        record and the pair adds up to the run."""
        streaming_stats.update(proc(DEFAULT_PID), incremental_gc_stats_item_factory(iid=0))
        streaming_stats.update(proc(DEFAULT_PID), incremental_gc_stats_item_factory(iid=1))

        first = streaming_stats.get_ring_stats(proc(DEFAULT_PID), 0)
        second = streaming_stats.get_ring_stats(proc(DEFAULT_PID), 1)
        assert first is not None and second is not None
        for metric_key, gen_stats in streaming_stats.metrics.items():
            for gen, total in gen_stats.items():
                one, other = first[metric_key][gen], second[metric_key][gen]
                assert one.count() + other.count() == total.count(), metric_key
                assert one.sum() + other.sum() == total.sum(), metric_key
                if total.count():
                    assert one.count() == 1, f"{metric_key} folded both interpreters"


class TestStreamingStatsRingBound:
    """Tests for the bound on rings gcmon holds detailed statistics for."""

    def test_the_bound_counts_interpreters_rather_than_processes(
        self,
        streaming_stats: StreamingStats,
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        """One process running many interpreters fills the bound the way many
        processes do."""
        for iid in range(StreamingStats.MAX_ACTIVE_RINGS + 1):
            streaming_stats.update(proc(DEFAULT_PID), gc_stats_item_factory(iid=iid))

        assert len(streaming_stats.rings()) == StreamingStats.MAX_ACTIVE_RINGS
        assert streaming_stats.untracked_rings() == 1


class TestStreamingStatsReadTime:
    """Tests for StreamingStats.record_read_time and the read_time property."""

    def test_read_time_empty_by_default(self, streaming_stats: StreamingStats) -> None:
        assert streaming_stats.read_time.count() == 0
        assert streaming_stats.read_time.sum() == 0
        assert streaming_stats.read_time.average() == 0.0

    def test_record_read_time_accumulates(self, streaming_stats: StreamingStats) -> None:
        for duration_ns in (100_000, 200_000, 300_000):
            streaming_stats.record_read_time(duration_ns)

        assert streaming_stats.read_time.count() == 3
        assert streaming_stats.read_time.sum() == 600_000
        assert streaming_stats.read_time.average() == 200_000

    def test_record_read_time_stores_nanoseconds_exactly(self, streaming_stats: StreamingStats) -> None:
        streaming_stats.record_read_time(1_500)
        streaming_stats.record_read_time(501)

        assert streaming_stats.read_time.sum() == 2_001

    def test_record_read_time_percentiles(self, streaming_stats: StreamingStats) -> None:
        for value in range(1, 101):
            streaming_stats.record_read_time(value)

        assert streaming_stats.read_time.percentile(50) == 50.5
        assert abs(streaming_stats.read_time.percentile(99) - 99.01) < 1e-9
        assert streaming_stats.read_time.percentile(100) == 100.0

    def test_record_read_time_zero(self, streaming_stats: StreamingStats) -> None:
        streaming_stats.record_read_time(0)

        assert streaming_stats.read_time.count() == 1
        assert streaming_stats.read_time.sum() == 0

    def test_read_time_independent_of_pause_metrics(
        self,
        streaming_stats: StreamingStats,
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        streaming_stats.update(proc(DEFAULT_PID), gc_stats_item_factory(ts_start=0, ts_stop=1_000_000))
        streaming_stats.record_read_time(42_000)

        assert streaming_stats.count() == 1
        assert streaming_stats.read_time.count() == 1
        assert streaming_stats.read_time.sum() == 42_000
        assert streaming_stats.metrics[PAUSE_KEY][0].sum() == 1_000_000
