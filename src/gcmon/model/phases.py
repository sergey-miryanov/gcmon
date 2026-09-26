"""The phases of a collection: which records carry each, where it runs,
and what it annotates."""

from typing import Any, ClassVar, Final, Protocol, TypeGuard

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
    TS_CLEAR_WEAKREFS_STOP,
    TS_DEDUCE_UNREACHABLE_START,
    TS_DELETE_GARBAGE_START,
    TS_FINALIZE_GARBAGE_STOP,
    TS_HANDLE_RESURRECTED_STOP,
    TS_HANDLE_WEAKREF_CALLBACKS_START,
    TS_START,
    UNCOLLECTABLE,
    Phase,
)
from .protocol import (
    TGCStatsInfo,
    is_gc_stats,
)
from .trace_event import EventArgs

__all__ = [
    "PAUSE_ROW",
    "SUB_PHASE_ROWS",
    "ClearWeakrefsData",
    "DeduceUnreachableData",
    "DeleteGarbageData",
    "FinalizeGarbageData",
    "HandleResurrectedData",
    "HandleWeakrefsData",
    "IncrementalData",
    "MarkAliveData",
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
    # The whole record: other modules name it too, so it lives in `protocol`.
    type Info = TGCStatsInfo

    phase: ClassVar[Phase] = PAUSE

    @staticmethod
    def check(item: object) -> TypeGuard[Info]:
        # A loss record carries `ts_start` too, and it is no GC record.
        return is_gc_stats(item) and getattr(item, TS_START, None) is not None

    @staticmethod
    def bounds(item: Info) -> tuple[int, int]:
        return item.ts_start, item.ts_stop

    @staticmethod
    def args(gen: int, item: Info) -> EventArgs:
        return {
            GENERATION: item.gen,
            IID: item.iid,
            COLLECTIONS: item.collections,
            COLLECTED: item.collected,
            UNCOLLECTABLE: item.uncollectable,
            CANDIDATES: item.candidates,
            HEAP_SIZE: item.heap_size,
            DURATION: item.duration,
        }


class MarkAliveData:
    class Info(Protocol):
        alive_size: int
        ts_mark_alive_start: int
        ts_mark_alive_stop: int

    phase: ClassVar[Phase] = MARK_ALIVE

    @staticmethod
    def check(item: object) -> TypeGuard[Info]:
        return getattr(item, ALIVE_SIZE, None) is not None

    @staticmethod
    def bounds(item: Info) -> tuple[int, int]:
        return item.ts_mark_alive_start, item.ts_mark_alive_stop

    @staticmethod
    def args(gen: int, item: Info) -> EventArgs | None:
        return {ALIVE_SIZE: item.alive_size} if gen > 0 else None


class IncrementalData:
    class Info(Protocol):
        increment_size: int
        ts_fill_increment_start: int
        ts_fill_increment_stop: int

    phase: ClassVar[Phase] = FILL_INCREMENT

    @staticmethod
    def check(item: object) -> TypeGuard[Info]:
        return getattr(item, INCREMENT_SIZE, None) is not None

    @staticmethod
    def bounds(item: Info) -> tuple[int, int]:
        return item.ts_fill_increment_start, item.ts_fill_increment_stop

    @staticmethod
    def args(gen: int, item: Info) -> EventArgs | None:
        return {INCREMENT_SIZE: item.increment_size} if gen < 2 else None


class DeduceUnreachableData:
    class Info(Protocol):
        candidates: int
        ts_deduce_unreachable_start: int
        ts_deduce_unreachable_stop: int

    phase: ClassVar[Phase] = DEDUCE_UNREACHABLE

    @staticmethod
    def check(item: object) -> TypeGuard[Info]:
        return getattr(item, TS_DEDUCE_UNREACHABLE_START, None) is not None

    @staticmethod
    def bounds(item: Info) -> tuple[int, int]:
        return item.ts_deduce_unreachable_start, item.ts_deduce_unreachable_stop

    @staticmethod
    def args(gen: int, item: Info) -> EventArgs | None:
        return {CANDIDATES: item.candidates}


class HandleWeakrefsData:
    class Info(Protocol):
        ts_handle_weakref_callbacks_start: int
        ts_handle_weakref_callbacks_stop: int

    phase: ClassVar[Phase] = HANDLE_WEAKREFS

    @staticmethod
    def check(item: object) -> TypeGuard[Info]:
        return getattr(item, TS_HANDLE_WEAKREF_CALLBACKS_START, None) is not None

    @staticmethod
    def bounds(item: Info) -> tuple[int, int]:
        return item.ts_handle_weakref_callbacks_start, item.ts_handle_weakref_callbacks_stop

    @staticmethod
    def args(gen: int, item: Info) -> EventArgs | None:
        return {}


# The next three start where the phase before them stopped, so each needs
# that phase's field as well as its own.
class FinalizeGarbageData:
    class Info(Protocol):
        finalized_garbage_count: int
        ts_handle_weakref_callbacks_stop: int
        ts_finalize_garbage_stop: int

    phase: ClassVar[Phase] = FINALIZE_GARBAGE

    @staticmethod
    def check(item: object) -> TypeGuard[Info]:
        return (
            getattr(item, TS_HANDLE_WEAKREF_CALLBACKS_START, None) is not None
            and getattr(item, TS_FINALIZE_GARBAGE_STOP, None) is not None
        )

    @staticmethod
    def bounds(item: Info) -> tuple[int, int]:
        return item.ts_handle_weakref_callbacks_stop, item.ts_finalize_garbage_stop

    @staticmethod
    def args(gen: int, item: Info) -> EventArgs | None:
        return {FINALIZED_GARBAGE_COUNT: item.finalized_garbage_count}


class HandleResurrectedData:
    class Info(Protocol):
        ts_finalize_garbage_stop: int
        ts_handle_resurrected_stop: int

    phase: ClassVar[Phase] = HANDLE_RESURRECTED

    @staticmethod
    def check(item: object) -> TypeGuard[Info]:
        return (
            getattr(item, TS_FINALIZE_GARBAGE_STOP, None) is not None
            and getattr(item, TS_HANDLE_RESURRECTED_STOP, None) is not None
        )

    @staticmethod
    def bounds(item: Info) -> tuple[int, int]:
        return item.ts_finalize_garbage_stop, item.ts_handle_resurrected_stop

    @staticmethod
    def args(gen: int, item: Info) -> EventArgs | None:
        return {}


class ClearWeakrefsData:
    class Info(Protocol):
        clear_weakrefs_count: int
        ts_handle_resurrected_stop: int
        ts_clear_weakrefs_stop: int

    phase: ClassVar[Phase] = CLEAR_WEAKREFS

    @staticmethod
    def check(item: object) -> TypeGuard[Info]:
        return (
            getattr(item, TS_HANDLE_RESURRECTED_STOP, None) is not None
            and getattr(item, TS_CLEAR_WEAKREFS_STOP, None) is not None
        )

    @staticmethod
    def bounds(item: Info) -> tuple[int, int]:
        return item.ts_handle_resurrected_stop, item.ts_clear_weakrefs_stop

    @staticmethod
    def args(gen: int, item: Info) -> EventArgs | None:
        return {CLEAR_WEAKREFS_COUNT: item.clear_weakrefs_count}


class DeleteGarbageData:
    class Info(Protocol):
        deleted_garbage_count: int
        ts_delete_garbage_start: int
        ts_delete_garbage_stop: int

    phase: ClassVar[Phase] = DELETE_GARBAGE

    @staticmethod
    def check(item: object) -> TypeGuard[Info]:
        return getattr(item, TS_DELETE_GARBAGE_START, None) is not None

    @staticmethod
    def bounds(item: Info) -> tuple[int, int]:
        return item.ts_delete_garbage_start, item.ts_delete_garbage_stop

    @staticmethod
    def args(gen: int, item: Info) -> EventArgs | None:
        return {DELETED_GARBAGE_COUNT: item.deleted_garbage_count}


PAUSE_ROW: Final = PauseData

# In the order the collector runs them, which is the order they are drawn in.
SUB_PHASE_ROWS: Final[tuple[type[PhaseRow], ...]] = (
    MarkAliveData,
    IncrementalData,
    DeduceUnreachableData,
    HandleWeakrefsData,
    FinalizeGarbageData,
    HandleResurrectedData,
    ClearWeakrefsData,
    DeleteGarbageData,
)

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
        rows = tuple(row for row in SUB_PHASE_ROWS if row.check(item))
        if isinstance(item, tuple):
            _SUB_PHASE_ROWS[t] = rows
    return rows
