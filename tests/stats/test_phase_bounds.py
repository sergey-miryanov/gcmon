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
from gcmon.stats.streaming_stats import phase_bounds
from tests.data_helpers import create_instant_msg


class TestPausePhase:
    """Tests for the Pause phase."""

    def test_name(self) -> None:
        row = PAUSE_ROW

        assert row.phase.label == GC_PAUSE_NAME

    def test_get_values(
        self,
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        row = PAUSE_ROW
        item = gc_stats_item_factory(ts_start=1000, ts_stop=5000)

        ts_start, ts_stop = phase_bounds(row, item)

        assert ts_start == 1000
        assert ts_stop == 5000

    def test_a_zero_length_pause_reads_as_zero(
        self,
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        """Every GC record carries `ts_start`, so the pause row's check holds for all of
        them and the pause has no missing-field case its siblings have."""
        row = PAUSE_ROW
        item = gc_stats_item_factory(ts_start=0, ts_stop=0)

        ts_start, ts_stop = phase_bounds(row, item)

        assert ts_start == 0
        assert ts_stop == 0

    def test_an_item_that_is_no_collection_reads_as_zero(self) -> None:
        """An instant carries `ts` and no `ts_start`, so it has no pause."""
        row = PAUSE_ROW

        values = phase_bounds(row, create_instant_msg(ts=5_000))

        assert values == (0, 0)


class TestMarkAlivePhase:
    """Tests for the MarkAlive phase."""

    def test_name(self) -> None:
        row = MarkAliveData

        assert row.phase.label == MARK_ALIVE.label

    def test_get_values(
        self,
        incremental_gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        row = MarkAliveData
        item = incremental_gc_stats_item_factory(
            gen=1,
            ts_mark_alive_start=2000,
            ts_mark_alive_stop=4000,
        )

        ts_start, ts_stop = phase_bounds(row, item)

        assert ts_start == 2000
        assert ts_stop == 4000

    def test_get_values_returns_zero_without_mark_alive(
        self,
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        row = MarkAliveData
        item = gc_stats_item_factory()

        ts1, ts2 = phase_bounds(row, item)

        assert ts1 == 0
        assert ts2 == 0


class TestFillIncrementPhase:
    """Tests for the FillIncrement phase."""

    def test_name(self) -> None:
        row = IncrementalData

        assert row.phase.label == FILL_INCREMENT.label

    def test_get_values(
        self,
        incremental_gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        row = IncrementalData
        item = incremental_gc_stats_item_factory(
            ts_fill_increment_start=3000,
            ts_fill_increment_stop=5000,
        )

        ts_start, ts_stop = phase_bounds(row, item)

        assert ts_start == 3000
        assert ts_stop == 5000

    def test_get_values_returns_zero_without_fill_increment(
        self,
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        row = IncrementalData
        item = gc_stats_item_factory()

        ts1, ts2 = phase_bounds(row, item)

        assert ts1 == 0
        assert ts2 == 0


class TestDeduceUnreachablePhase:
    """Tests for the DeduceUnreachable phase."""

    def test_name(self) -> None:
        row = DeduceUnreachableData

        assert row.phase.label == DEDUCE_UNREACHABLE.label

    def test_get_values(
        self,
        incremental_gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        row = DeduceUnreachableData
        item = incremental_gc_stats_item_factory(
            ts_deduce_unreachable_start=7000,
            ts_deduce_unreachable_stop=9000,
        )

        ts_start, ts_stop = phase_bounds(row, item)

        assert ts_start == 7000
        assert ts_stop == 9000

    def test_get_values_returns_zero_without_deduce_unreachable(
        self,
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        row = DeduceUnreachableData
        item = gc_stats_item_factory()

        ts1, ts2 = phase_bounds(row, item)

        assert ts1 == 0
        assert ts2 == 0


class TestHandleWeakrefsPhase:
    """Tests for the HandleWeakrefs phase."""

    def test_name(self) -> None:
        row = HandleWeakrefsData

        assert row.phase.label == HANDLE_WEAKREFS.label

    def test_get_values(
        self,
        incremental_gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        row = HandleWeakrefsData
        item = incremental_gc_stats_item_factory(
            ts_handle_weakref_callbacks_start=7000,
            ts_handle_weakref_callbacks_stop=9000,
        )

        ts_start, ts_stop = phase_bounds(row, item)

        assert ts_start == 7000
        assert ts_stop == 9000

    def test_get_values_returns_zero_without_weakrefs(
        self,
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        row = HandleWeakrefsData
        item = gc_stats_item_factory()

        ts1, ts2 = phase_bounds(row, item)

        assert ts1 == 0
        assert ts2 == 0


class TestFinalizeGarbagePhase:
    """Tests for the FinalizeGarbage phase."""

    def test_name(self) -> None:
        row = FinalizeGarbageData

        assert row.phase.label == FINALIZE_GARBAGE.label

    def test_get_values(
        self,
        incremental_gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        row = FinalizeGarbageData
        item = incremental_gc_stats_item_factory(
            ts_handle_weakref_callbacks_stop=8000,
            ts_finalize_garbage_stop=9000,
        )

        ts_start, ts_stop = phase_bounds(row, item)

        assert ts_start == 8000
        assert ts_stop == 9000

    def test_get_values_returns_zero_without_prerequisite(
        self,
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        row = FinalizeGarbageData
        item = gc_stats_item_factory(ts_finalize_garbage_stop=9000, finalized_garbage_count=1)

        ts1, ts2 = phase_bounds(row, item)

        assert ts1 == 0
        assert ts2 == 0


class TestHandleResurrectedPhase:
    """Tests for the HandleResurrected phase."""

    def test_name(self) -> None:
        row = HandleResurrectedData

        assert row.phase.label == HANDLE_RESURRECTED.label

    def test_get_values(
        self,
        incremental_gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        row = HandleResurrectedData
        item = incremental_gc_stats_item_factory(
            ts_finalize_garbage_stop=8000,
            ts_handle_resurrected_stop=9000,
        )

        ts_start, ts_stop = phase_bounds(row, item)

        assert ts_start == 8000
        assert ts_stop == 9000

    def test_get_values_returns_zero_without_prerequisite(
        self,
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        row = HandleResurrectedData
        item = gc_stats_item_factory(ts_handle_resurrected_stop=9000)

        ts1, ts2 = phase_bounds(row, item)

        assert ts1 == 0
        assert ts2 == 0


class TestClearWeakrefsPhase:
    """Tests for the ClearWeakrefs phase."""

    def test_name(self) -> None:
        row = ClearWeakrefsData

        assert row.phase.label == CLEAR_WEAKREFS.label

    def test_get_values(
        self,
        incremental_gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        row = ClearWeakrefsData
        item = incremental_gc_stats_item_factory(
            ts_handle_resurrected_stop=8000,
            ts_clear_weakrefs_stop=9000,
        )

        ts_start, ts_stop = phase_bounds(row, item)

        assert ts_start == 8000
        assert ts_stop == 9000

    def test_get_values_returns_zero_without_prerequisite(
        self,
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        row = ClearWeakrefsData
        item = gc_stats_item_factory(ts_clear_weakrefs_stop=9000, clear_weakrefs_count=1)

        ts1, ts2 = phase_bounds(row, item)

        assert ts1 == 0
        assert ts2 == 0


class TestDeleteGarbagePhase:
    """Tests for the DeleteGarbage phase."""

    def test_name(self) -> None:
        row = DeleteGarbageData

        assert row.phase.label == DELETE_GARBAGE.label

    def test_get_values(
        self,
        incremental_gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        row = DeleteGarbageData
        item = incremental_gc_stats_item_factory(
            ts_delete_garbage_start=7000,
            ts_delete_garbage_stop=9000,
        )

        ts_start, ts_stop = phase_bounds(row, item)

        assert ts_start == 7000
        assert ts_stop == 9000

    def test_get_values_returns_zero_without_delete_garbage(
        self,
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        row = DeleteGarbageData
        item = gc_stats_item_factory()

        ts1, ts2 = phase_bounds(row, item)

        assert ts1 == 0
        assert ts2 == 0
