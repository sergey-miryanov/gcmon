"""The phases of a collection: which records carry each, where it runs,
and what it annotates."""

from typing import Any, ClassVar, Protocol, TypeGuard

from .names import (
    ALIVE_SIZE,
    CANDIDATES,
    CLEAR_WEAKREFS,
    CLEAR_WEAKREFS_COUNT,
    COLLECTED,
    COLLECTIONS,
    DEDUCE_UNREACHABLE,
    DELETE_GARBAGE,
    DELETED_GARBAGE_COUNT,
    DURATION,
    FILL_INCREMENT,
    FINALIZE_GARBAGE,
    FINALIZED_GARBAGE_COUNT,
    GENERATION,
    HANDLE_RESURRECTED,
    HANDLE_WEAKREFS,
    HEAP_SIZE,
    IID,
    INCREMENT_SIZE,
    MARK_ALIVE,
    PAUSE,
    UNCOLLECTABLE,
    Phase,
)
from .protocol import (
    TClearWeakrefsInfo,
    TDeduceUnreachableInfo,
    TDeleteGarbageInfo,
    TFinalizeGarbageInfo,
    TGCStatsInfo,
    THandleResurrectedInfo,
    THandleWeakrefsInfo,
    TIncrementalInfo,
    TMarkAliveInfo,
    has_clear_weakrefs,
    has_deduce_unreachable,
    has_delete_garbage,
    has_finalize_garbage,
    has_handle_resurrected,
    has_handle_weakrefs,
    has_incremental,
    has_mark_alive,
    has_pause_ts,
)
from .trace_event import EventArgs

__all__ = [
    "Data",
    "PauseData",
    "PhaseRow",
    "sub_phase_rows",
]


class PhaseRow(Protocol):
    """One phase: which records carry it, where it runs, and what it
    annotates. *item* in `bounds` and `args` is one `check` accepted."""

    phase: ClassVar[Phase]

    @staticmethod
    def check(item: object) -> bool: ...

    @staticmethod
    def bounds(item: Any) -> tuple[int, int]: ...

    # None when the phase does not run at generation *gen*.
    @staticmethod
    def args(gen: int, item: Any) -> EventArgs | None: ...


class PauseData:
    phase: ClassVar[Phase] = PAUSE

    @staticmethod
    def check(item: object) -> TypeGuard[TGCStatsInfo]:
        return has_pause_ts(item)

    @staticmethod
    def bounds(item: TGCStatsInfo) -> tuple[int, int]:
        return item.ts_start, item.ts_stop

    @staticmethod
    def args(gen: int, item: TGCStatsInfo) -> EventArgs:
        return {
            GENERATION: item.gen,
            IID: item.iid,
            HEAP_SIZE: item.heap_size,
            COLLECTIONS: item.collections,
            COLLECTED: item.collected,
            UNCOLLECTABLE: item.uncollectable,
            CANDIDATES: item.candidates,
            DURATION: item.duration,
        }


class MarkAliveData:
    phase: ClassVar[Phase] = MARK_ALIVE

    @staticmethod
    def check(item: object) -> TypeGuard[TMarkAliveInfo]:
        return has_mark_alive(item)

    @staticmethod
    def bounds(item: TMarkAliveInfo) -> tuple[int, int]:
        return item.ts_mark_alive_start, item.ts_mark_alive_stop

    @staticmethod
    def args(gen: int, item: TMarkAliveInfo) -> EventArgs | None:
        return {ALIVE_SIZE: item.alive_size} if gen > 0 else None


class IncrementalData:
    phase: ClassVar[Phase] = FILL_INCREMENT

    @staticmethod
    def check(item: object) -> TypeGuard[TIncrementalInfo]:
        return has_incremental(item)

    @staticmethod
    def bounds(item: TIncrementalInfo) -> tuple[int, int]:
        return item.ts_fill_increment_start, item.ts_fill_increment_stop

    @staticmethod
    def args(gen: int, item: TIncrementalInfo) -> EventArgs | None:
        return {INCREMENT_SIZE: item.increment_size} if gen < 2 else None


class DeduceUnreachableData:
    phase: ClassVar[Phase] = DEDUCE_UNREACHABLE

    @staticmethod
    def check(item: object) -> TypeGuard[TDeduceUnreachableInfo]:
        return has_deduce_unreachable(item)

    @staticmethod
    def bounds(item: TDeduceUnreachableInfo) -> tuple[int, int]:
        return item.ts_deduce_unreachable_start, item.ts_deduce_unreachable_stop

    @staticmethod
    def args(gen: int, item: TDeduceUnreachableInfo) -> EventArgs | None:
        return {CANDIDATES: item.candidates}


class HandleWeakrefsData:
    phase: ClassVar[Phase] = HANDLE_WEAKREFS

    @staticmethod
    def check(item: object) -> TypeGuard[THandleWeakrefsInfo]:
        return has_handle_weakrefs(item)

    @staticmethod
    def bounds(item: THandleWeakrefsInfo) -> tuple[int, int]:
        return item.ts_handle_weakref_callbacks_start, item.ts_handle_weakref_callbacks_stop

    @staticmethod
    def args(gen: int, item: THandleWeakrefsInfo) -> EventArgs | None:
        return {}


# The next three start where the phase before them stopped, so each needs
# that phase's guard as well as its own.
class FinalizeGarbageData:
    phase: ClassVar[Phase] = FINALIZE_GARBAGE

    @staticmethod
    def check(item: object) -> TypeGuard[TFinalizeGarbageInfo]:
        return has_handle_weakrefs(item) and has_finalize_garbage(item)

    @staticmethod
    def bounds(item: TFinalizeGarbageInfo) -> tuple[int, int]:
        return item.ts_handle_weakref_callbacks_stop, item.ts_finalize_garbage_stop

    @staticmethod
    def args(gen: int, item: TFinalizeGarbageInfo) -> EventArgs | None:
        return {FINALIZED_GARBAGE_COUNT: item.finalized_garbage_count}


class HandleResurrectedData:
    phase: ClassVar[Phase] = HANDLE_RESURRECTED

    @staticmethod
    def check(item: object) -> TypeGuard[THandleResurrectedInfo]:
        return has_finalize_garbage(item) and has_handle_resurrected(item)

    @staticmethod
    def bounds(item: THandleResurrectedInfo) -> tuple[int, int]:
        return item.ts_finalize_garbage_stop, item.ts_handle_resurrected_stop

    @staticmethod
    def args(gen: int, item: THandleResurrectedInfo) -> EventArgs | None:
        return {}


class ClearWeakrefsData:
    phase: ClassVar[Phase] = CLEAR_WEAKREFS

    @staticmethod
    def check(item: object) -> TypeGuard[TClearWeakrefsInfo]:
        return has_handle_resurrected(item) and has_clear_weakrefs(item)

    @staticmethod
    def bounds(item: TClearWeakrefsInfo) -> tuple[int, int]:
        return item.ts_handle_resurrected_stop, item.ts_clear_weakrefs_stop

    @staticmethod
    def args(gen: int, item: TClearWeakrefsInfo) -> EventArgs | None:
        return {CLEAR_WEAKREFS_COUNT: item.clear_weakrefs_count}


class DeleteGarbageData:
    phase: ClassVar[Phase] = DELETE_GARBAGE

    @staticmethod
    def check(item: object) -> TypeGuard[TDeleteGarbageInfo]:
        return has_delete_garbage(item)

    @staticmethod
    def bounds(item: TDeleteGarbageInfo) -> tuple[int, int]:
        return item.ts_delete_garbage_start, item.ts_delete_garbage_stop

    @staticmethod
    def args(gen: int, item: TDeleteGarbageInfo) -> EventArgs | None:
        return {DELETED_GARBAGE_COUNT: item.deleted_garbage_count}


# The pause first, then its sub-phases in the order the collector runs them.
Data: list[type[PhaseRow]] = [
    PauseData,
    MarkAliveData,
    IncrementalData,
    DeduceUnreachableData,
    HandleWeakrefsData,
    FinalizeGarbageData,
    HandleResurrectedData,
    ClearWeakrefsData,
    DeleteGarbageData,
]

# Per struct-sequence type: the sub-phase rows its records carry.
_SUB_PHASE_ROWS: dict[type, tuple[type[PhaseRow], ...]] = {}


def sub_phase_rows(item: TGCStatsInfo) -> tuple[type[PhaseRow], ...]:
    """The sub-phase rows whose `check` accepts *item*.

    A struct sequence's fields are fixed by its type, so the checks run on
    the first record of each type. A msgspec record holds `None` for a field
    it lacks, and its type says nothing about which, so it is checked every
    time.
    """
    t = type(item)
    rows = _SUB_PHASE_ROWS.get(t)
    if rows is None:
        rows = tuple(row for row in Data[1:] if row.check(item))
        if isinstance(item, tuple):
            _SUB_PHASE_ROWS[t] = rows
    return rows
