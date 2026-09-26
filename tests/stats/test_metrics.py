"""Tests for the phases `--stats` measures."""

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
from gcmon.model.phases import (
    PAUSE_ROW,
    ClearWeakrefsData,
    DeduceUnreachableData,
    DeleteGarbageData,
    FinalizeGarbageData,
    HandleResurrectedData,
    HandleWeakrefsData,
    IncrementalData,
    MarkAliveData,
)
from gcmon.stats.metrics import (
    METRICS,
    PAUSE_KEY,
    phase_bounds,
)
from tests.data_helpers import create_instant_msg


class TestPauseMetric:
    """Tests for the Pause metric."""

    def test_name(self) -> None:
        metric = METRICS[PAUSE_KEY]

        assert metric.phase.label == GC_PAUSE_NAME

    def test_get_values(
        self,
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        metric = METRICS[PAUSE_KEY]
        item = gc_stats_item_factory(ts_start=1000, ts_stop=5000)

        ts_start, ts_stop = phase_bounds(metric, item)

        assert ts_start == 1000
        assert ts_stop == 5000

    def test_a_zero_length_pause_reads_as_zero(
        self,
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        """Every GC record carries `ts_start`, so the pause row's check holds for all of
        them and the pause has no missing-field case its siblings have."""
        metric = METRICS[PAUSE_KEY]
        item = gc_stats_item_factory(ts_start=0, ts_stop=0)

        ts_start, ts_stop = phase_bounds(metric, item)

        assert ts_start == 0
        assert ts_stop == 0

    def test_an_item_that_is_no_collection_reads_as_zero(self) -> None:
        """An instant carries `ts` and no `ts_start`, so it has no pause."""
        metric = METRICS[PAUSE_KEY]

        values = phase_bounds(metric, create_instant_msg(ts=5_000))

        assert values == (0, 0)


class TestMarkAliveMetric:
    """Tests for the MarkAlive metric."""

    def test_name(self) -> None:
        metric = METRICS["mark_alive"]

        assert metric.phase.label == MARK_ALIVE.label

    def test_get_values(
        self,
        incremental_gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        metric = METRICS["mark_alive"]
        item = incremental_gc_stats_item_factory(
            gen=1,
            ts_mark_alive_start=2000,
            ts_mark_alive_stop=4000,
        )

        ts_start, ts_stop = phase_bounds(metric, item)

        assert ts_start == 2000
        assert ts_stop == 4000

    def test_get_values_returns_zero_without_mark_alive(
        self,
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        metric = METRICS["mark_alive"]
        item = gc_stats_item_factory()

        ts1, ts2 = phase_bounds(metric, item)

        assert ts1 == 0
        assert ts2 == 0


class TestFillIncrementMetric:
    """Tests for the FillIncrement metric."""

    def test_name(self) -> None:
        metric = METRICS["fill_increment"]

        assert metric.phase.label == FILL_INCREMENT.label

    def test_get_values(
        self,
        incremental_gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        metric = METRICS["fill_increment"]
        item = incremental_gc_stats_item_factory(
            ts_fill_increment_start=3000,
            ts_fill_increment_stop=5000,
        )

        ts_start, ts_stop = phase_bounds(metric, item)

        assert ts_start == 3000
        assert ts_stop == 5000

    def test_get_values_returns_zero_without_fill_increment(
        self,
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        metric = METRICS["fill_increment"]
        item = gc_stats_item_factory()

        ts1, ts2 = phase_bounds(metric, item)

        assert ts1 == 0
        assert ts2 == 0


class TestDeduceUnreachableMetric:
    """Tests for the DeduceUnreachable metric."""

    def test_name(self) -> None:
        metric = METRICS["deduce_unreachable"]

        assert metric.phase.label == DEDUCE_UNREACHABLE.label

    def test_get_values(
        self,
        incremental_gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        metric = METRICS["deduce_unreachable"]
        item = incremental_gc_stats_item_factory(
            ts_deduce_unreachable_start=7000,
            ts_deduce_unreachable_stop=9000,
        )

        ts_start, ts_stop = phase_bounds(metric, item)

        assert ts_start == 7000
        assert ts_stop == 9000

    def test_get_values_returns_zero_without_deduce_unreachable(
        self,
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        metric = METRICS["deduce_unreachable"]
        item = gc_stats_item_factory()

        ts1, ts2 = phase_bounds(metric, item)

        assert ts1 == 0
        assert ts2 == 0


class TestHandleWeakrefsMetric:
    """Tests for the HandleWeakrefs metric."""

    def test_name(self) -> None:
        metric = METRICS["handle_weakrefs"]

        assert metric.phase.label == HANDLE_WEAKREFS.label

    def test_get_values(
        self,
        incremental_gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        metric = METRICS["handle_weakrefs"]
        item = incremental_gc_stats_item_factory(
            ts_handle_weakref_callbacks_start=7000,
            ts_handle_weakref_callbacks_stop=9000,
        )

        ts_start, ts_stop = phase_bounds(metric, item)

        assert ts_start == 7000
        assert ts_stop == 9000

    def test_get_values_returns_zero_without_weakrefs(
        self,
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        metric = METRICS["handle_weakrefs"]
        item = gc_stats_item_factory()

        ts1, ts2 = phase_bounds(metric, item)

        assert ts1 == 0
        assert ts2 == 0


class TestFinalizeGarbageMetric:
    """Tests for the FinalizeGarbage metric."""

    def test_name(self) -> None:
        metric = METRICS["finalize_garbage"]

        assert metric.phase.label == FINALIZE_GARBAGE.label

    def test_get_values(
        self,
        incremental_gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        metric = METRICS["finalize_garbage"]
        item = incremental_gc_stats_item_factory(
            ts_handle_weakref_callbacks_stop=8000,
            ts_finalize_garbage_stop=9000,
        )

        ts_start, ts_stop = phase_bounds(metric, item)

        assert ts_start == 8000
        assert ts_stop == 9000

    def test_get_values_returns_zero_without_prerequisite(
        self,
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        metric = METRICS["finalize_garbage"]
        item = gc_stats_item_factory(ts_finalize_garbage_stop=9000, finalized_garbage_count=1)

        ts1, ts2 = phase_bounds(metric, item)

        assert ts1 == 0
        assert ts2 == 0


class TestHandleResurrectedMetric:
    """Tests for the HandleResurrected metric."""

    def test_name(self) -> None:
        metric = METRICS["handle_resurrected"]

        assert metric.phase.label == HANDLE_RESURRECTED.label

    def test_get_values(
        self,
        incremental_gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        metric = METRICS["handle_resurrected"]
        item = incremental_gc_stats_item_factory(
            ts_finalize_garbage_stop=8000,
            ts_handle_resurrected_stop=9000,
        )

        ts_start, ts_stop = phase_bounds(metric, item)

        assert ts_start == 8000
        assert ts_stop == 9000

    def test_get_values_returns_zero_without_prerequisite(
        self,
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        metric = METRICS["handle_resurrected"]
        item = gc_stats_item_factory(ts_handle_resurrected_stop=9000)

        ts1, ts2 = phase_bounds(metric, item)

        assert ts1 == 0
        assert ts2 == 0


class TestClearWeakrefsMetric:
    """Tests for the ClearWeakrefs metric."""

    def test_name(self) -> None:
        metric = METRICS["clear_weakrefs"]

        assert metric.phase.label == CLEAR_WEAKREFS.label

    def test_get_values(
        self,
        incremental_gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        metric = METRICS["clear_weakrefs"]
        item = incremental_gc_stats_item_factory(
            ts_handle_resurrected_stop=8000,
            ts_clear_weakrefs_stop=9000,
        )

        ts_start, ts_stop = phase_bounds(metric, item)

        assert ts_start == 8000
        assert ts_stop == 9000

    def test_get_values_returns_zero_without_prerequisite(
        self,
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        metric = METRICS["clear_weakrefs"]
        item = gc_stats_item_factory(ts_clear_weakrefs_stop=9000, clear_weakrefs_count=1)

        ts1, ts2 = phase_bounds(metric, item)

        assert ts1 == 0
        assert ts2 == 0


class TestDeleteGarbageMetric:
    """Tests for the DeleteGarbage metric."""

    def test_name(self) -> None:
        metric = METRICS["delete_garbage"]

        assert metric.phase.label == DELETE_GARBAGE.label

    def test_get_values(
        self,
        incremental_gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        metric = METRICS["delete_garbage"]
        item = incremental_gc_stats_item_factory(
            ts_delete_garbage_start=7000,
            ts_delete_garbage_stop=9000,
        )

        ts_start, ts_stop = phase_bounds(metric, item)

        assert ts_start == 7000
        assert ts_stop == 9000

    def test_get_values_returns_zero_without_delete_garbage(
        self,
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        metric = METRICS["delete_garbage"]
        item = gc_stats_item_factory()

        ts1, ts2 = phase_bounds(metric, item)

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
        assert METRICS[PAUSE_KEY] is PAUSE_ROW
        assert METRICS["mark_alive"] is MarkAliveData
        assert METRICS["fill_increment"] is IncrementalData
        assert METRICS["deduce_unreachable"] is DeduceUnreachableData
        assert METRICS["handle_weakrefs"] is HandleWeakrefsData
        assert METRICS["finalize_garbage"] is FinalizeGarbageData
        assert METRICS["handle_resurrected"] is HandleResurrectedData
        assert METRICS["clear_weakrefs"] is ClearWeakrefsData
        assert METRICS["delete_garbage"] is DeleteGarbageData

    def test_metrics_count(self) -> None:
        assert len(METRICS) == 9
