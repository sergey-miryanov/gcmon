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
    ClearWeakrefsSubPhase,
    DeduceUnreachableSubPhase,
    DeleteGarbageSubPhase,
    FillIncrementSubPhase,
    FinalizeGarbageSubPhase,
    HandleResurrectedSubPhase,
    HandleWeakrefsSubPhase,
    MarkAliveSubPhase,
    PausePhase,
)
from tests.data_helpers import create_instant_msg
from tests.helpers import phase_bounds


class TestPausePhase:
    """Tests for the Pause phase."""

    def test_name(self) -> None:
        phase = PausePhase

        assert phase.name.label == GC_PAUSE_NAME

    def test_get_values(
        self,
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        phase = PausePhase
        item = gc_stats_item_factory(ts_start=1000, ts_stop=5000)

        ts_start, ts_stop = phase_bounds(phase, item)

        assert ts_start == 1000
        assert ts_stop == 5000

    def test_a_zero_length_pause_reads_as_zero(
        self,
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        """Every GC record carries `ts_start`, so the pause phase's check holds for all of
        them and the pause has no missing-field case its siblings have."""
        phase = PausePhase
        item = gc_stats_item_factory(ts_start=0, ts_stop=0)

        ts_start, ts_stop = phase_bounds(phase, item)

        assert ts_start == 0
        assert ts_stop == 0

    def test_an_item_that_is_no_collection_reads_as_zero(self) -> None:
        """An instant carries `ts` and no `ts_start`, so it has no pause."""
        phase = PausePhase

        values = phase_bounds(phase, create_instant_msg(ts=5_000))

        assert values == (0, 0)


class TestMarkAlivePhase:
    """Tests for the MarkAlive phase."""

    def test_name(self) -> None:
        phase = MarkAliveSubPhase

        assert phase.name.label == MARK_ALIVE.label

    def test_get_values(
        self,
        incremental_gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        phase = MarkAliveSubPhase
        item = incremental_gc_stats_item_factory(
            gen=1,
            ts_mark_alive_start=2000,
            ts_mark_alive_stop=4000,
        )

        ts_start, ts_stop = phase_bounds(phase, item)

        assert ts_start == 2000
        assert ts_stop == 4000

    def test_get_values_returns_zero_without_mark_alive(
        self,
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        phase = MarkAliveSubPhase
        item = gc_stats_item_factory()

        ts1, ts2 = phase_bounds(phase, item)

        assert ts1 == 0
        assert ts2 == 0


class TestFillIncrementPhase:
    """Tests for the FillIncrement phase."""

    def test_name(self) -> None:
        phase = FillIncrementSubPhase

        assert phase.name.label == FILL_INCREMENT.label

    def test_get_values(
        self,
        incremental_gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        phase = FillIncrementSubPhase
        item = incremental_gc_stats_item_factory(
            ts_fill_increment_start=3000,
            ts_fill_increment_stop=5000,
        )

        ts_start, ts_stop = phase_bounds(phase, item)

        assert ts_start == 3000
        assert ts_stop == 5000

    def test_get_values_returns_zero_without_fill_increment(
        self,
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        phase = FillIncrementSubPhase
        item = gc_stats_item_factory()

        ts1, ts2 = phase_bounds(phase, item)

        assert ts1 == 0
        assert ts2 == 0


class TestDeduceUnreachablePhase:
    """Tests for the DeduceUnreachable phase."""

    def test_name(self) -> None:
        phase = DeduceUnreachableSubPhase

        assert phase.name.label == DEDUCE_UNREACHABLE.label

    def test_get_values(
        self,
        incremental_gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        phase = DeduceUnreachableSubPhase
        item = incremental_gc_stats_item_factory(
            ts_deduce_unreachable_start=7000,
            ts_deduce_unreachable_stop=9000,
        )

        ts_start, ts_stop = phase_bounds(phase, item)

        assert ts_start == 7000
        assert ts_stop == 9000

    def test_get_values_returns_zero_without_deduce_unreachable(
        self,
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        phase = DeduceUnreachableSubPhase
        item = gc_stats_item_factory()

        ts1, ts2 = phase_bounds(phase, item)

        assert ts1 == 0
        assert ts2 == 0


class TestHandleWeakrefsPhase:
    """Tests for the HandleWeakrefs phase."""

    def test_name(self) -> None:
        phase = HandleWeakrefsSubPhase

        assert phase.name.label == HANDLE_WEAKREFS.label

    def test_get_values(
        self,
        incremental_gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        phase = HandleWeakrefsSubPhase
        item = incremental_gc_stats_item_factory(
            ts_handle_weakref_callbacks_start=7000,
            ts_handle_weakref_callbacks_stop=9000,
        )

        ts_start, ts_stop = phase_bounds(phase, item)

        assert ts_start == 7000
        assert ts_stop == 9000

    def test_get_values_returns_zero_without_weakrefs(
        self,
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        phase = HandleWeakrefsSubPhase
        item = gc_stats_item_factory()

        ts1, ts2 = phase_bounds(phase, item)

        assert ts1 == 0
        assert ts2 == 0


class TestFinalizeGarbagePhase:
    """Tests for the FinalizeGarbage phase."""

    def test_name(self) -> None:
        phase = FinalizeGarbageSubPhase

        assert phase.name.label == FINALIZE_GARBAGE.label

    def test_get_values(
        self,
        incremental_gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        phase = FinalizeGarbageSubPhase
        item = incremental_gc_stats_item_factory(
            ts_handle_weakref_callbacks_stop=8000,
            ts_finalize_garbage_stop=9000,
        )

        ts_start, ts_stop = phase_bounds(phase, item)

        assert ts_start == 8000
        assert ts_stop == 9000

    def test_get_values_returns_zero_without_prerequisite(
        self,
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        phase = FinalizeGarbageSubPhase
        item = gc_stats_item_factory(ts_finalize_garbage_stop=9000, finalized_garbage_count=1)

        ts1, ts2 = phase_bounds(phase, item)

        assert ts1 == 0
        assert ts2 == 0


class TestHandleResurrectedPhase:
    """Tests for the HandleResurrected phase."""

    def test_name(self) -> None:
        phase = HandleResurrectedSubPhase

        assert phase.name.label == HANDLE_RESURRECTED.label

    def test_get_values(
        self,
        incremental_gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        phase = HandleResurrectedSubPhase
        item = incremental_gc_stats_item_factory(
            ts_finalize_garbage_stop=8000,
            ts_handle_resurrected_stop=9000,
        )

        ts_start, ts_stop = phase_bounds(phase, item)

        assert ts_start == 8000
        assert ts_stop == 9000

    def test_get_values_returns_zero_without_prerequisite(
        self,
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        phase = HandleResurrectedSubPhase
        item = gc_stats_item_factory(ts_handle_resurrected_stop=9000)

        ts1, ts2 = phase_bounds(phase, item)

        assert ts1 == 0
        assert ts2 == 0


class TestClearWeakrefsPhase:
    """Tests for the ClearWeakrefs phase."""

    def test_name(self) -> None:
        phase = ClearWeakrefsSubPhase

        assert phase.name.label == CLEAR_WEAKREFS.label

    def test_get_values(
        self,
        incremental_gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        phase = ClearWeakrefsSubPhase
        item = incremental_gc_stats_item_factory(
            ts_handle_resurrected_stop=8000,
            ts_clear_weakrefs_stop=9000,
        )

        ts_start, ts_stop = phase_bounds(phase, item)

        assert ts_start == 8000
        assert ts_stop == 9000

    def test_get_values_returns_zero_without_prerequisite(
        self,
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        phase = ClearWeakrefsSubPhase
        item = gc_stats_item_factory(ts_clear_weakrefs_stop=9000, clear_weakrefs_count=1)

        ts1, ts2 = phase_bounds(phase, item)

        assert ts1 == 0
        assert ts2 == 0


class TestDeleteGarbagePhase:
    """Tests for the DeleteGarbage phase."""

    def test_name(self) -> None:
        phase = DeleteGarbageSubPhase

        assert phase.name.label == DELETE_GARBAGE.label

    def test_get_values(
        self,
        incremental_gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        phase = DeleteGarbageSubPhase
        item = incremental_gc_stats_item_factory(
            ts_delete_garbage_start=7000,
            ts_delete_garbage_stop=9000,
        )

        ts_start, ts_stop = phase_bounds(phase, item)

        assert ts_start == 7000
        assert ts_stop == 9000

    def test_get_values_returns_zero_without_delete_garbage(
        self,
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        phase = DeleteGarbageSubPhase
        item = gc_stats_item_factory()

        ts1, ts2 = phase_bounds(phase, item)

        assert ts1 == 0
        assert ts2 == 0
