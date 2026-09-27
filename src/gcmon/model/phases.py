"""The phases of a collection: which records carry each, where it runs,
and what it annotates; and the fields that annotate the pause alone."""

from collections.abc import Mapping
from functools import partial
from operator import attrgetter
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
    TS_FILL_INCREMENT_START,
    TS_FINALIZE_GARBAGE_STOP,
    TS_HANDLE_RESURRECTED_STOP,
    TS_HANDLE_WEAKREF_CALLBACKS_START,
    TS_HANDLE_WEAKREF_CALLBACKS_STOP,
    TS_START,
    UNCOLLECTABLE,
    PerGeneration,
    PhaseName,
    counter_display_name,
)
from .protocol import (
    TGCStatsInfo,
    is_gc_stats,
)
from .trace_event import EventArgs

__all__ = [
    "PAUSE_FIELDS",
    "PHASES",
    "SUB_PHASES",
    "ClearWeakrefsSubPhase",
    "DeduceUnreachableSubPhase",
    "DeleteGarbageSubPhase",
    "FillIncrementSubPhase",
    "FinalizeGarbageSubPhase",
    "HandleResurrectedSubPhase",
    "HandleWeakrefsSubPhase",
    "IncrementSizeField",
    "MarkAliveSubPhase",
    "PauseField",
    "PausePhase",
    "Phase",
    "SubPhase",
    "sub_phases",
    "sub_phases_and_fields",
]


class Phase(Protocol):
    """One phase: which records carry it, where it runs, and what it
    annotates. *item* in `bounds` and `args` is one `check` accepted."""

    name: ClassVar[PhaseName]

    @staticmethod
    def check(item: object) -> bool: ...

    @staticmethod
    def bounds(gen: int, item: Any) -> tuple[int, int]: ...

    @staticmethod
    def args(gen: int, iid: int, item: Any) -> EventArgs: ...


class SubPhase(Phase, Protocol):
    """A phase inside the pause. `Info` names the fields `bounds` and `args`
    read."""

    Info: ClassVar[type]


class PauseField(Protocol):
    """Fields with no span of their own: which records carry them, and what
    they annotate the pause with. `Info` names the fields `args` reads."""

    Info: ClassVar[type]

    @staticmethod
    def check(item: object) -> bool: ...

    @staticmethod
    def args(gen: int, iid: int, item: Any) -> EventArgs: ...


class PausePhase:
    type Info = TGCStatsInfo

    name: ClassVar[PhaseName] = PAUSE

    counter_metrics: ClassVar = (COLLECTED, CANDIDATES, DURATION, UNCOLLECTABLE)
    counter_names: ClassVar[Mapping[str, PerGeneration[str]]] = {
        metric: PerGeneration(partial(counter_display_name, metric=metric)) for metric in counter_metrics
    }

    @staticmethod
    def check(item: object) -> TypeGuard[Info]:
        # A loss record carries `ts_start` too, and it is no GC record.
        return is_gc_stats(item) and getattr(item, TS_START, None) is not None

    @staticmethod
    def bounds(gen: int, item: Info) -> tuple[int, int]:
        return item.ts_start, item.ts_stop

    @staticmethod
    def args(gen: int, iid: int, item: Info) -> EventArgs:
        return {
            GENERATION: gen,
            IID: iid,
            COLLECTIONS: item.collections,
            COLLECTED: item.collected,
            UNCOLLECTABLE: item.uncollectable,
            CANDIDATES: item.candidates,
            HEAP_SIZE: item.heap_size,
            DURATION: item.duration,
        }

    @classmethod
    def counters(cls, gen: int, item: Info) -> list[tuple[str, str, int | float]]:
        names = cls.counter_names
        counters: list[tuple[str, str, int | float]] = [
            (COLLECTED, names[COLLECTED][gen], item.collected),
            (CANDIDATES, names[CANDIDATES][gen], item.candidates),
            (DURATION, names[DURATION][gen], item.duration),
        ]
        if item.uncollectable:
            counters.append((UNCOLLECTABLE, names[UNCOLLECTABLE][gen], item.uncollectable))
        counters.append((HEAP_SIZE, HEAP_SIZE, item.heap_size))
        return counters


class MarkAliveSubPhase:
    class _Info(Protocol):
        alive_size: int
        ts_mark_alive_start: int
        ts_mark_alive_stop: int

    Info: ClassVar[type] = _Info
    name: ClassVar[PhaseName] = MARK_ALIVE

    @staticmethod
    def check(item: object) -> TypeGuard[_Info]:
        return getattr(item, ALIVE_SIZE, None) is not None

    @staticmethod
    def bounds(gen: int, item: _Info) -> tuple[int, int]:
        # Mark Alive does not run at generation 0.
        if gen == 0:
            return 0, 0
        return item.ts_mark_alive_start, item.ts_mark_alive_stop

    @staticmethod
    def args(gen: int, iid: int, item: _Info) -> EventArgs:
        return {GENERATION: gen, IID: iid, ALIVE_SIZE: item.alive_size}


class IncrementSizeField:
    """The size of the increment Fill Increment fills."""

    class _Info(Protocol):
        increment_size: int

    Info: ClassVar[type] = _Info

    @staticmethod
    def check(item: object) -> TypeGuard[_Info]:
        return getattr(item, INCREMENT_SIZE, None) is not None

    @staticmethod
    def args(gen: int, iid: int, item: _Info) -> EventArgs:
        # Generation 2 fills no increment.
        if gen == 2:
            return {}
        return {GENERATION: gen, IID: iid, INCREMENT_SIZE: item.increment_size}


class FillIncrementSubPhase:
    class _Info(Protocol):
        ts_fill_increment_start: int
        ts_fill_increment_stop: int

    Info: ClassVar[type] = _Info
    name: ClassVar[PhaseName] = FILL_INCREMENT

    @staticmethod
    def check(item: object) -> TypeGuard[_Info]:
        return getattr(item, TS_FILL_INCREMENT_START, None) is not None

    @staticmethod
    def bounds(gen: int, item: _Info) -> tuple[int, int]:
        # Fill Increment does not run at generation 2.
        if gen == 2:
            return 0, 0
        return item.ts_fill_increment_start, item.ts_fill_increment_stop

    @staticmethod
    def args(gen: int, iid: int, item: _Info) -> EventArgs:
        return {GENERATION: gen, IID: iid}


class DeduceUnreachableSubPhase:
    class _Info(Protocol):
        candidates: int
        ts_deduce_unreachable_start: int
        ts_deduce_unreachable_stop: int

    Info: ClassVar[type] = _Info
    name: ClassVar[PhaseName] = DEDUCE_UNREACHABLE

    @staticmethod
    def check(item: object) -> TypeGuard[_Info]:
        return getattr(item, TS_DEDUCE_UNREACHABLE_START, None) is not None

    @staticmethod
    def bounds(gen: int, item: _Info) -> tuple[int, int]:
        return item.ts_deduce_unreachable_start, item.ts_deduce_unreachable_stop

    @staticmethod
    def args(gen: int, iid: int, item: _Info) -> EventArgs:
        return {GENERATION: gen, IID: iid, CANDIDATES: item.candidates}


class HandleWeakrefsSubPhase:
    class _Info(Protocol):
        ts_handle_weakref_callbacks_start: int
        ts_handle_weakref_callbacks_stop: int

    Info: ClassVar[type] = _Info
    name: ClassVar[PhaseName] = HANDLE_WEAKREFS

    @staticmethod
    def check(item: object) -> TypeGuard[_Info]:
        return getattr(item, TS_HANDLE_WEAKREF_CALLBACKS_START, None) is not None

    @staticmethod
    def bounds(gen: int, item: _Info) -> tuple[int, int]:
        return item.ts_handle_weakref_callbacks_start, item.ts_handle_weakref_callbacks_stop

    @staticmethod
    def args(gen: int, iid: int, item: _Info) -> EventArgs:
        return {GENERATION: gen, IID: iid}


class FinalizeGarbageSubPhase:
    class _Info(Protocol):
        finalized_garbage_count: int
        ts_handle_weakref_callbacks_stop: int
        ts_finalize_garbage_stop: int

    Info: ClassVar[type] = _Info
    name: ClassVar[PhaseName] = FINALIZE_GARBAGE

    @staticmethod
    def check(item: object) -> TypeGuard[_Info]:
        return (
            getattr(item, TS_HANDLE_WEAKREF_CALLBACKS_STOP, None) is not None
            and getattr(item, TS_FINALIZE_GARBAGE_STOP, None) is not None
        )

    @staticmethod
    def bounds(gen: int, item: _Info) -> tuple[int, int]:
        return item.ts_handle_weakref_callbacks_stop, item.ts_finalize_garbage_stop

    @staticmethod
    def args(gen: int, iid: int, item: _Info) -> EventArgs:
        return {GENERATION: gen, IID: iid, FINALIZED_GARBAGE_COUNT: item.finalized_garbage_count}


class HandleResurrectedSubPhase:
    class _Info(Protocol):
        ts_finalize_garbage_stop: int
        ts_handle_resurrected_stop: int

    Info: ClassVar[type] = _Info
    name: ClassVar[PhaseName] = HANDLE_RESURRECTED

    @staticmethod
    def check(item: object) -> TypeGuard[_Info]:
        return (
            getattr(item, TS_FINALIZE_GARBAGE_STOP, None) is not None
            and getattr(item, TS_HANDLE_RESURRECTED_STOP, None) is not None
        )

    @staticmethod
    def bounds(gen: int, item: _Info) -> tuple[int, int]:
        return item.ts_finalize_garbage_stop, item.ts_handle_resurrected_stop

    @staticmethod
    def args(gen: int, iid: int, item: _Info) -> EventArgs:
        return {GENERATION: gen, IID: iid}


class ClearWeakrefsSubPhase:
    class _Info(Protocol):
        clear_weakrefs_count: int
        ts_handle_resurrected_stop: int
        ts_clear_weakrefs_stop: int

    Info: ClassVar[type] = _Info
    name: ClassVar[PhaseName] = CLEAR_WEAKREFS

    @staticmethod
    def check(item: object) -> TypeGuard[_Info]:
        return (
            getattr(item, TS_HANDLE_RESURRECTED_STOP, None) is not None
            and getattr(item, TS_CLEAR_WEAKREFS_STOP, None) is not None
        )

    @staticmethod
    def bounds(gen: int, item: _Info) -> tuple[int, int]:
        return item.ts_handle_resurrected_stop, item.ts_clear_weakrefs_stop

    @staticmethod
    def args(gen: int, iid: int, item: _Info) -> EventArgs:
        return {GENERATION: gen, IID: iid, CLEAR_WEAKREFS_COUNT: item.clear_weakrefs_count}


class DeleteGarbageSubPhase:
    class _Info(Protocol):
        deleted_garbage_count: int
        ts_delete_garbage_start: int
        ts_delete_garbage_stop: int

    Info: ClassVar[type] = _Info
    name: ClassVar[PhaseName] = DELETE_GARBAGE

    @staticmethod
    def check(item: object) -> TypeGuard[_Info]:
        return getattr(item, TS_DELETE_GARBAGE_START, None) is not None

    @staticmethod
    def bounds(gen: int, item: _Info) -> tuple[int, int]:
        return item.ts_delete_garbage_start, item.ts_delete_garbage_stop

    @staticmethod
    def args(gen: int, iid: int, item: _Info) -> EventArgs:
        return {GENERATION: gen, IID: iid, DELETED_GARBAGE_COUNT: item.deleted_garbage_count}


# In the order the collector runs them, which is the order they are drawn in.
SUB_PHASES: Final[tuple[type[SubPhase], ...]] = (
    MarkAliveSubPhase,
    FillIncrementSubPhase,
    DeduceUnreachableSubPhase,
    HandleWeakrefsSubPhase,
    FinalizeGarbageSubPhase,
    HandleResurrectedSubPhase,
    ClearWeakrefsSubPhase,
    DeleteGarbageSubPhase,
)

# The pause, then its sub-phases.
PHASES: Final[tuple[type[Phase], ...]] = (PausePhase, *SUB_PHASES)

PAUSE_FIELDS: Final[tuple[type[PauseField], ...]] = (IncrementSizeField,)

type _SubPhasesAndFields = tuple[tuple[type[SubPhase], ...], tuple[type[PauseField], ...]]

# Per struct-sequence type: the sub-phases and pause fields its records carry.
_BY_TYPE: dict[type, _SubPhasesAndFields] = {}

# The fields the `check`s read. A msgspec record holds `None` for any it
# lacks, so which of these it holds decides which sub-phases and pause fields
# it carries.
_CHECKED_FIELDS: Final = attrgetter(
    ALIVE_SIZE,
    INCREMENT_SIZE,
    TS_FILL_INCREMENT_START,
    TS_DEDUCE_UNREACHABLE_START,
    TS_HANDLE_WEAKREF_CALLBACKS_START,
    TS_HANDLE_WEAKREF_CALLBACKS_STOP,
    TS_FINALIZE_GARBAGE_STOP,
    TS_HANDLE_RESURRECTED_STOP,
    TS_CLEAR_WEAKREFS_STOP,
    TS_DELETE_GARBAGE_START,
)

# Per set of checked fields present: what a msgspec record carries. A
# capture holds a handful of shapes, however many records.
_BY_FIELDS: dict[tuple[bool, ...], _SubPhasesAndFields] = {}


def sub_phases(item: TGCStatsInfo) -> tuple[type[SubPhase], ...]:
    """The sub-phases whose `check` accepts *item*."""
    return sub_phases_and_fields(item)[0]


def sub_phases_and_fields(item: TGCStatsInfo) -> _SubPhasesAndFields:
    """The sub-phases, then the pause fields, whose `check` accepts *item*.

    A struct sequence's fields are fixed by its type, so the checks run on
    the first record of each type. A msgspec record holds `None` for a field
    it lacks and its type says nothing about which, so what it carries is
    cached on which optional fields it holds.
    """
    if isinstance(item, tuple):
        cache: dict[Any, _SubPhasesAndFields] = _BY_TYPE
        key: Any = type(item)
    else:
        cache = _BY_FIELDS
        key = tuple([value is None for value in _CHECKED_FIELDS(item)])
    carried = cache.get(key)
    if carried is None:
        carried = cache[key] = (
            tuple(phase for phase in SUB_PHASES if phase.check(item)),
            tuple(field for field in PAUSE_FIELDS if field.check(item)),
        )
    return carried
