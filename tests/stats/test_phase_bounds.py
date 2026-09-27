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
    PHASE_ROWS,
    ClearWeakrefsData,
    DeduceUnreachableData,
    DeleteGarbageData,
    FinalizeGarbageData,
    HandleResurrectedData,
    HandleWeakrefsData,
    IncrementalData,
    MarkAliveData,
)
from gcmon.stats.streaming_stats import phase_bounds
from tests.data_helpers import create_instant_msg


class TestPauseMetric:
    """Tests for the Pause metric."""

    def test_name(self) -> None:
        metric = PAUSE_ROW

        assert metric.phase.label == GC_PAUSE_NAME

    def test_get_values(
        self,
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        metric = PAUSE_ROW
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
        metric = PAUSE_ROW
        item = gc_stats_item_factory(ts_start=0, ts_stop=0)

        ts_start, ts_stop = phase_bounds(metric, item)

        assert ts_start == 0
        assert ts_stop == 0

    def test_an_item_that_is_no_collection_reads_as_zero(self) -> None:
        """An instant carries `ts` and no `ts_start`, so it has no pause."""
        metric = PAUSE_ROW

        values = phase_bounds(metric, create_instant_msg(ts=5_000))

        assert values == (0, 0)


class TestMarkAliveMetric:
    """Tests for the MarkAlive metric."""

    def test_name(self) -> None:
        metric = MarkAliveData

        assert metric.phase.label == MARK_ALIVE.label

    def test_get_values(
        self,
        incremental_gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        metric = MarkAliveData
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
        metric = MarkAliveData
        item = gc_stats_item_factory()

        ts1, ts2 = phase_bounds(metric, item)

        assert ts1 == 0
        assert ts2 == 0


class TestFillIncrementMetric:
    """Tests for the FillIncrement metric."""

    def test_name(self) -> None:
        metric = IncrementalData

        assert metric.phase.label == FILL_INCREMENT.label

    def test_get_values(
        self,
        incremental_gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        metric = IncrementalData
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
        metric = IncrementalData
        item = gc_stats_item_factory()

        ts1, ts2 = phase_bounds(metric, item)

        assert ts1 == 0
        assert ts2 == 0


class TestDeduceUnreachableMetric:
    """Tests for the DeduceUnreachable metric."""

    def test_name(self) -> None:
        metric = DeduceUnreachableData

        assert metric.phase.label == DEDUCE_UNREACHABLE.label

    def test_get_values(
        self,
        incremental_gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        metric = DeduceUnreachableData
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
        metric = DeduceUnreachableData
        item = gc_stats_item_factory()

        ts1, ts2 = phase_bounds(metric, item)

        assert ts1 == 0
        assert ts2 == 0


class TestHandleWeakrefsMetric:
    """Tests for the HandleWeakrefs metric."""

    def test_name(self) -> None:
        metric = HandleWeakrefsData

        assert metric.phase.label == HANDLE_WEAKREFS.label

    def test_get_values(
        self,
        incremental_gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        metric = HandleWeakrefsData
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
        metric = HandleWeakrefsData
        item = gc_stats_item_factory()

        ts1, ts2 = phase_bounds(metric, item)

        assert ts1 == 0
        assert ts2 == 0


class TestFinalizeGarbageMetric:
    """Tests for the FinalizeGarbage metric."""

    def test_name(self) -> None:
        metric = FinalizeGarbageData

        assert metric.phase.label == FINALIZE_GARBAGE.label

    def test_get_values(
        self,
        incremental_gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        metric = FinalizeGarbageData
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
        metric = FinalizeGarbageData
        item = gc_stats_item_factory(ts_finalize_garbage_stop=9000, finalized_garbage_count=1)

        ts1, ts2 = phase_bounds(metric, item)

        assert ts1 == 0
        assert ts2 == 0


class TestHandleResurrectedMetric:
    """Tests for the HandleResurrected metric."""

    def test_name(self) -> None:
        metric = HandleResurrectedData

        assert metric.phase.label == HANDLE_RESURRECTED.label

    def test_get_values(
        self,
        incremental_gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        metric = HandleResurrectedData
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
        metric = HandleResurrectedData
        item = gc_stats_item_factory(ts_handle_resurrected_stop=9000)

        ts1, ts2 = phase_bounds(metric, item)

        assert ts1 == 0
        assert ts2 == 0


class TestClearWeakrefsMetric:
    """Tests for the ClearWeakrefs metric."""

    def test_name(self) -> None:
        metric = ClearWeakrefsData

        assert metric.phase.label == CLEAR_WEAKREFS.label

    def test_get_values(
        self,
        incremental_gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        metric = ClearWeakrefsData
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
        metric = ClearWeakrefsData
        item = gc_stats_item_factory(ts_clear_weakrefs_stop=9000, clear_weakrefs_count=1)

        ts1, ts2 = phase_bounds(metric, item)

        assert ts1 == 0
        assert ts2 == 0


class TestDeleteGarbageMetric:
    """Tests for the DeleteGarbage metric."""

    def test_name(self) -> None:
        metric = DeleteGarbageData

        assert metric.phase.label == DELETE_GARBAGE.label

    def test_get_values(
        self,
        incremental_gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        metric = DeleteGarbageData
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
        metric = DeleteGarbageData
        item = gc_stats_item_factory()

        ts1, ts2 = phase_bounds(metric, item)

        assert ts1 == 0
        assert ts2 == 0


def test_every_row_carries_the_key_its_tables_use() -> None:
    """The keys are what a `--stats` table and a ring hold a phase under, in
    the order the table prints them."""
    assert [row.key for row in PHASE_ROWS] == [
        "pause",
        "mark_alive",
        "fill_increment",
        "deduce_unreachable",
        "handle_weakrefs",
        "finalize_garbage",
        "handle_resurrected",
        "clear_weakrefs",
        "delete_garbage",
    ]
