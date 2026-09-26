"""The phases of a collection: which records carry each, where it runs,
and what it annotates."""

from collections.abc import Mapping
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
    GEN_COUNTER_METRICS,
    GENERATION,
    GENERATIONS,
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
    counter_display_name,
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

    # Empty when the phase does not run at the record's generation.
    @staticmethod
    def bounds(item: Any) -> tuple[int, int]: ...

    @staticmethod
    def args(gen: int, item: Any) -> EventArgs: ...


# Each counter's track name per generation the collector has. A pause draws
# three or four counters and each name is the same string every time, so
# `counters` reads it here rather than building it.
_COUNTER_NAMES: Final[Mapping[int, Mapping[str, str]]] = {
    gen: {metric: counter_display_name(gen, metric) for metric in GEN_COUNTER_METRICS} for gen in GENERATIONS
}


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

    # (metric, track name, value) for each counter the pause draws: the
    # per-generation series, `GEN_COUNTER_METRICS`, then `heap_size`.
    @staticmethod
    def counters(gen: int, item: Info) -> list[tuple[str, str, int | float]]:
        names = _COUNTER_NAMES.get(gen)
        # No collector emits a generation the table lacks, and a capture that
        # carries one still converts.
        if names is None:
            names = {metric: counter_display_name(gen, metric) for metric in GEN_COUNTER_METRICS}
        counters: list[tuple[str, str, int | float]] = [
            (COLLECTED, names[COLLECTED], item.collected),
            (CANDIDATES, names[CANDIDATES], item.candidates),
            (DURATION, names[DURATION], item.duration),
        ]
        # A run that collected everything omits it rather than writing a zero.
        if item.uncollectable:
            counters.append((UNCOLLECTABLE, names[UNCOLLECTABLE], item.uncollectable))
        # Unqualified: it gauges the interpreter rather than a generation
        # (ADR-0004), and its row sits inside the interpreter's own group, so
        # no two share a parent and the name need not tell them apart
        # (ADR-0027).
        counters.append((HEAP_SIZE, HEAP_SIZE, item.heap_size))
        return counters


class MarkAliveData:
    class Info(Protocol):
        gen: int
        alive_size: int
        ts_mark_alive_start: int
        ts_mark_alive_stop: int

    phase: ClassVar[Phase] = MARK_ALIVE

    @staticmethod
    def check(item: object) -> TypeGuard[Info]:
        return getattr(item, ALIVE_SIZE, None) is not None

    @staticmethod
    def bounds(item: Info) -> tuple[int, int]:
        # Mark Alive does not run at generation 0.
        if item.gen == 0:
            return 0, 0
        return item.ts_mark_alive_start, item.ts_mark_alive_stop

    @staticmethod
    def args(gen: int, item: Info) -> EventArgs:
        return {ALIVE_SIZE: item.alive_size}


class IncrementalData:
    class Info(Protocol):
        gen: int
        increment_size: int
        ts_fill_increment_start: int
        ts_fill_increment_stop: int

    phase: ClassVar[Phase] = FILL_INCREMENT

    @staticmethod
    def check(item: object) -> TypeGuard[Info]:
        return getattr(item, INCREMENT_SIZE, None) is not None

    @staticmethod
    def bounds(item: Info) -> tuple[int, int]:
        # Fill Increment does not run at generation 2.
        if item.gen == 2:
            return 0, 0
        return item.ts_fill_increment_start, item.ts_fill_increment_stop

    @staticmethod
    def args(gen: int, item: Info) -> EventArgs:
        return {INCREMENT_SIZE: item.increment_size}


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
    def args(gen: int, item: Info) -> EventArgs:
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
    def args(gen: int, item: Info) -> EventArgs:
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
    def args(gen: int, item: Info) -> EventArgs:
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
    def args(gen: int, item: Info) -> EventArgs:
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
    def args(gen: int, item: Info) -> EventArgs:
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
    def args(gen: int, item: Info) -> EventArgs:
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
