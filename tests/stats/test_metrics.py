"""Tests for metric classes."""

from __future__ import annotations

from collections.abc import Callable

from gcmon.model.data import GCStatsInfo
from gcmon.model.names import (
    CLEAR_WEAKREFS,
    DEDUCE_UNREACHABLE,
    DELETE_GARBAGE,
    FILL_INCREMENT,
    FINALIZE_GARBAGE,
    GC_PAUSE_NAME,
    HANDLE_RESURRECTED,
    HANDLE_WEAKREFS,
    MARK_ALIVE,
)
from gcmon.stats.metrics import (
    METRICS,
    PAUSE_KEY,
    ClearWeakrefsMetric,
    DeduceUnreachableMetric,
    DeleteGarbageMetric,
    FillIncrementMetric,
    FinalizeGarbageMetric,
    HandleResurrectedMetric,
    HandleWeakrefsMetric,
    MarkAliveMetric,
    PauseMetric,
)


class TestPauseMetric:
    """Tests for PauseMetric class."""

    def test_name(self) -> None:
        metric = PauseMetric()
        assert metric.name == GC_PAUSE_NAME

    def test_get_values(
        self,
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        metric = PauseMetric()
        item = gc_stats_item_factory(ts_start=1000, ts_stop=5000)
        ts_start, ts_stop = metric.get_values(item)
        assert ts_start == 1000
        assert ts_stop == 5000

    def test_a_zero_length_pause_reads_as_zero(
        self,
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        """Every GC record carries `ts_start`, so `has_pause_ts` holds for all of
        them and `PauseMetric` has no missing-field case its siblings have."""
        metric = PauseMetric()
        item = gc_stats_item_factory(ts_start=0, ts_stop=0)
        ts_start, ts_stop = metric.get_values(item)
        assert ts_start == 0
        assert ts_stop == 0


class TestMarkAliveMetric:
    """Tests for MarkAliveMetric class."""

    def test_name(self) -> None:
        metric = MarkAliveMetric()
        assert metric.name == MARK_ALIVE.label

    def test_get_values(
        self,
        incremental_gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        metric = MarkAliveMetric()
        item = incremental_gc_stats_item_factory(
            ts_mark_alive_start=2000,
            ts_mark_alive_stop=4000,
        )
        ts_start, ts_stop = metric.get_values(item)
        assert ts_start == 2000
        assert ts_stop == 4000

    def test_get_values_asserts_non_incremental(
        self,
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        metric = MarkAliveMetric()
        item = gc_stats_item_factory()
        ts1, ts2 = metric.get_values(item)
        assert ts1 == 0
        assert ts2 == 0


class TestFillIncrementMetric:
    """Tests for FillIncrementMetric class."""

    def test_name(self) -> None:
        metric = FillIncrementMetric()
        assert metric.name == FILL_INCREMENT.label

    def test_get_values(
        self,
        incremental_gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        metric = FillIncrementMetric()
        item = incremental_gc_stats_item_factory(
            ts_fill_increment_start=3000,
            ts_fill_increment_stop=5000,
        )
        ts_start, ts_stop = metric.get_values(item)
        assert ts_start == 3000
        assert ts_stop == 5000

    def test_get_values_asserts_non_incremental(
        self,
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        metric = FillIncrementMetric()
        item = gc_stats_item_factory()
        ts1, ts2 = metric.get_values(item)
        assert ts1 == 0
        assert ts2 == 0


class TestDeduceUnreachableMetric:
    """Tests for DeduceUnreachableMetric class."""

    def test_name(self) -> None:
        metric = DeduceUnreachableMetric()
        assert metric.name == DEDUCE_UNREACHABLE.label

    def test_get_values(
        self,
        incremental_gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        metric = DeduceUnreachableMetric()
        item = incremental_gc_stats_item_factory(
            ts_deduce_unreachable_start=7000,
            ts_deduce_unreachable_stop=9000,
        )
        ts_start, ts_stop = metric.get_values(item)
        assert ts_start == 7000
        assert ts_stop == 9000

    def test_get_values_asserts_non_incremental(
        self,
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        metric = DeduceUnreachableMetric()
        item = gc_stats_item_factory()
        ts1, ts2 = metric.get_values(item)
        assert ts1 == 0
        assert ts2 == 0


class TestHandleWeakrefsMetric:
    """Tests for HandleWeakrefsMetric class."""

    def test_name(self) -> None:
        metric = HandleWeakrefsMetric()
        assert metric.name == HANDLE_WEAKREFS.label

    def test_get_values(
        self,
        incremental_gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        metric = HandleWeakrefsMetric()
        item = incremental_gc_stats_item_factory(
            ts_handle_weakref_callbacks_start=7000,
            ts_handle_weakref_callbacks_stop=9000,
        )
        ts_start, ts_stop = metric.get_values(item)
        assert ts_start == 7000
        assert ts_stop == 9000

    def test_get_values_returns_zero_without_weakrefs(
        self,
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        metric = HandleWeakrefsMetric()
        item = gc_stats_item_factory()
        ts1, ts2 = metric.get_values(item)
        assert ts1 == 0
        assert ts2 == 0


class TestFinalizeGarbageMetric:
    """Tests for FinalizeGarbageMetric class."""

    def test_name(self) -> None:
        metric = FinalizeGarbageMetric()
        assert metric.name == FINALIZE_GARBAGE.label

    def test_get_values(
        self,
        incremental_gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        metric = FinalizeGarbageMetric()
        item = incremental_gc_stats_item_factory(
            ts_handle_weakref_callbacks_stop=8000,
            ts_finalize_garbage_stop=9000,
        )
        ts_start, ts_stop = metric.get_values(item)
        assert ts_start == 8000
        assert ts_stop == 9000

    def test_get_values_returns_zero_without_prerequisite(
        self,
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        metric = FinalizeGarbageMetric()
        item = gc_stats_item_factory(ts_finalize_garbage_stop=9000, finalized_garbage_count=1)
        ts1, ts2 = metric.get_values(item)
        assert ts1 == 0
        assert ts2 == 0


class TestHandleResurrectedMetric:
    """Tests for HandleResurrectedMetric class."""

    def test_name(self) -> None:
        metric = HandleResurrectedMetric()
        assert metric.name == HANDLE_RESURRECTED.label

    def test_get_values(
        self,
        incremental_gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        metric = HandleResurrectedMetric()
        item = incremental_gc_stats_item_factory(
            ts_finalize_garbage_stop=8000,
            ts_handle_resurrected_stop=9000,
        )
        ts_start, ts_stop = metric.get_values(item)
        assert ts_start == 8000
        assert ts_stop == 9000

    def test_get_values_returns_zero_without_prerequisite(
        self,
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        metric = HandleResurrectedMetric()
        item = gc_stats_item_factory(ts_handle_resurrected_stop=9000)
        ts1, ts2 = metric.get_values(item)
        assert ts1 == 0
        assert ts2 == 0


class TestClearWeakrefsMetric:
    """Tests for ClearWeakrefsMetric class."""

    def test_name(self) -> None:
        metric = ClearWeakrefsMetric()
        assert metric.name == CLEAR_WEAKREFS.label

    def test_get_values(
        self,
        incremental_gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        metric = ClearWeakrefsMetric()
        item = incremental_gc_stats_item_factory(
            ts_handle_resurrected_stop=8000,
            ts_clear_weakrefs_stop=9000,
        )
        ts_start, ts_stop = metric.get_values(item)
        assert ts_start == 8000
        assert ts_stop == 9000

    def test_get_values_returns_zero_without_prerequisite(
        self,
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        metric = ClearWeakrefsMetric()
        item = gc_stats_item_factory(ts_clear_weakrefs_stop=9000, clear_weakrefs_count=1)
        ts1, ts2 = metric.get_values(item)
        assert ts1 == 0
        assert ts2 == 0


class TestDeleteGarbageMetric:
    """Tests for DeleteGarbageMetric class."""

    def test_name(self) -> None:
        metric = DeleteGarbageMetric()
        assert metric.name == DELETE_GARBAGE.label

    def test_get_values(
        self,
        incremental_gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        metric = DeleteGarbageMetric()
        item = incremental_gc_stats_item_factory(
            ts_delete_garbage_start=7000,
            ts_delete_garbage_stop=9000,
        )
        ts_start, ts_stop = metric.get_values(item)
        assert ts_start == 7000
        assert ts_stop == 9000

    def test_get_values_returns_zero_without_delete_garbage(
        self,
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        metric = DeleteGarbageMetric()
        item = gc_stats_item_factory()
        ts1, ts2 = metric.get_values(item)
        assert ts1 == 0
        assert ts2 == 0


class TestMetricDictionaries:
    """Tests for METRICS dictionary."""

    def test_metrics_keys(self) -> None:
        expected_keys = {
            PAUSE_KEY,
            "mark_alive",
            "fill_increment",
            "deduce_unreachable",
            "handle_weakrefs",
            "finalize_garbage",
            "handle_resurrected",
            "clear_weakrefs",
            "delete_garbage",
        }
        assert set(METRICS) == expected_keys

    def test_metrics_instances(self) -> None:
        assert isinstance(METRICS[PAUSE_KEY], PauseMetric)
        assert isinstance(METRICS["mark_alive"], MarkAliveMetric)
        assert isinstance(METRICS["fill_increment"], FillIncrementMetric)
        assert isinstance(METRICS["deduce_unreachable"], DeduceUnreachableMetric)
        assert isinstance(METRICS["handle_weakrefs"], HandleWeakrefsMetric)
        assert isinstance(METRICS["finalize_garbage"], FinalizeGarbageMetric)
        assert isinstance(METRICS["handle_resurrected"], HandleResurrectedMetric)
        assert isinstance(METRICS["clear_weakrefs"], ClearWeakrefsMetric)
        assert isinstance(METRICS["delete_garbage"], DeleteGarbageMetric)

    def test_metrics_count(self) -> None:
        assert len(METRICS) == 9
