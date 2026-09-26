from collections.abc import Mapping, Sequence
from typing import Any, ClassVar, Protocol, TypeGuard

import msgspec

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
    GEN,
    GENERATION,
    GENS,
    HANDLE_RESURRECTED,
    HANDLE_WEAKREFS,
    HEAP_SIZE,
    IID,
    INCREMENT_SIZE,
    LOST_COUNT,
    LOST_FROM,
    LOST_PAUSE_NS,
    MARK_ALIVE,
    NAME,
    OBSERVED_COUNT,
    PAUSE,
    TS,
    TS_CLEAR_WEAKREFS_STOP,
    TS_DEDUCE_UNREACHABLE_START,
    TS_DELETE_GARBAGE_START,
    TS_FINALIZE_GARBAGE_STOP,
    TS_HANDLE_RESURRECTED_STOP,
    TS_HANDLE_WEAKREF_CALLBACKS_START,
    TS_START,
    TS_STOP,
    TYPE,
    UNCOLLECTABLE,
    Phase,
)
from .trace_event import EventArgs

__all__ = [
    "JsonlRecord",
    "TClearWeakrefsInfo",
    "TDeduceUnreachableInfo",
    "TDeleteGarbageInfo",
    "TFinalizeGarbageInfo",
    "TGCStatsInfo",
    "TGenLoss",
    "THandleResurrectedInfo",
    "THandleWeakrefsInfo",
    "TIncrementalInfo",
    "TInstantMsg",
    "TItem",
    "TLossMsg",
    "TMapping",
    "TMarkAliveInfo",
    "TScalar",
    "TValue",
    "has_clear_weakrefs",
    "has_deduce_unreachable",
    "has_delete_garbage",
    "has_finalize_garbage",
    "has_handle_resurrected",
    "has_handle_weakrefs",
    "has_incremental",
    "has_mark_alive",
    "has_pause_ts",
    "is_gc_stats",
    "is_instant",
    "is_loss",
    "structseq_fields",
    "structseq_hidden",
    "to_mapping",
]


class TGCStatsInfo(Protocol):
    gen: int
    iid: int
    ts_start: int
    ts_stop: int
    heap_size: int
    collections: int
    collected: int
    uncollectable: int
    candidates: int
    duration: float


class TIncrementalInfo(Protocol):
    increment_size: int
    ts_fill_increment_start: int
    ts_fill_increment_stop: int


class TMarkAliveInfo(Protocol):
    alive_size: int
    ts_mark_alive_start: int
    ts_mark_alive_stop: int


class TDeduceUnreachableInfo(Protocol):
    candidates: int
    ts_deduce_unreachable_start: int
    ts_deduce_unreachable_stop: int


class TFinalizeGarbageInfo(Protocol):
    finalized_garbage_count: int
    ts_handle_weakref_callbacks_stop: int
    ts_finalize_garbage_stop: int


class TDeleteGarbageInfo(Protocol):
    deleted_garbage_count: int
    ts_delete_garbage_start: int
    ts_delete_garbage_stop: int


class THandleWeakrefsInfo(Protocol):
    ts_handle_weakref_callbacks_start: int
    ts_handle_weakref_callbacks_stop: int


class TClearWeakrefsInfo(Protocol):
    clear_weakrefs_count: int
    ts_handle_resurrected_stop: int
    ts_clear_weakrefs_stop: int


class THandleResurrectedInfo(Protocol):
    ts_finalize_garbage_stop: int
    ts_handle_resurrected_stop: int


class TInstantMsg(Protocol):
    type: str
    name: str
    ts: int


class TGenLoss(Protocol):
    gen: int
    observed_count: int
    lost_count: int
    lost_pause_ns: int
    lost_from: int


class TLossMsg(Protocol):
    iid: int
    ts_start: int
    ts_stop: int

    @property
    def gens(self) -> Sequence[TGenLoss]: ...


# What one JSONL field decodes to. Only the loss record's `gens` holds more
# than a scalar, and the nested arm is there for it.
type TScalar = str | int | float
type TValue = TScalar | Sequence[Mapping[str, TScalar]]

# `TMapping` only reads; `JsonlRecord` is the dict a writer fills and owns.
type TMapping = Mapping[str, TValue]
type JsonlRecord = dict[str, TValue]

# What a whole JSONL line decodes to, and what the converters accept.
type TItem = TGCStatsInfo | TInstantMsg | TLossMsg


# Per struct-sequence type: its visible field names, and whether it has
# hidden ones, which only `__reduce__` returns.
_STRUCTSEQ_FIELDS: dict[type, tuple[tuple[str, ...], bool]] = {}


def structseq_fields(t: Any) -> tuple[tuple[str, ...], bool]:
    """The visible field names of struct-sequence type *t*, and whether it
    has hidden fields as well."""
    fields = _STRUCTSEQ_FIELDS.get(t)
    if fields is None:
        fields = _STRUCTSEQ_FIELDS[t] = (t.__match_args__, t.n_fields > t.n_sequence_fields)
    return fields


def structseq_hidden(item: Any) -> dict[str, Any]:
    """The hidden fields of struct sequence *item*, by name."""
    hidden: dict[str, Any] = item.__reduce__()[1][1]
    return hidden


def has_pause_ts(item: object) -> TypeGuard[TGCStatsInfo]:
    # A loss record carries `ts_start` too, and it is no GC record.
    return is_gc_stats(item) and getattr(item, TS_START, None) is not None


class _Row(Protocol):
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
Data: list[type[_Row]] = [
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


def has_incremental(item: object) -> TypeGuard[TIncrementalInfo]:
    return getattr(item, INCREMENT_SIZE, None) is not None


def has_mark_alive(item: object) -> TypeGuard[TMarkAliveInfo]:
    return getattr(item, ALIVE_SIZE, None) is not None


def has_deduce_unreachable(item: object) -> TypeGuard[TDeduceUnreachableInfo]:
    return getattr(item, TS_DEDUCE_UNREACHABLE_START, None) is not None


def has_handle_weakrefs(item: object) -> TypeGuard[THandleWeakrefsInfo]:
    return getattr(item, TS_HANDLE_WEAKREF_CALLBACKS_START, None) is not None


def has_finalize_garbage(item: object) -> TypeGuard[TFinalizeGarbageInfo]:
    return getattr(item, TS_FINALIZE_GARBAGE_STOP, None) is not None


def has_handle_resurrected(item: object) -> TypeGuard[THandleResurrectedInfo]:
    return getattr(item, TS_HANDLE_RESURRECTED_STOP, None) is not None


def has_clear_weakrefs(item: object) -> TypeGuard[TClearWeakrefsInfo]:
    return getattr(item, TS_CLEAR_WEAKREFS_STOP, None) is not None


def has_delete_garbage(item: object) -> TypeGuard[TDeleteGarbageInfo]:
    return getattr(item, TS_DELETE_GARBAGE_START, None) is not None


def is_gc_stats(item: object) -> TypeGuard[TGCStatsInfo]:
    """A GC record is the one record type built around ``collections``."""
    return hasattr(item, COLLECTIONS)


def is_instant(item: object) -> TypeGuard[TInstantMsg]:
    return hasattr(item, TYPE)


def is_loss(item: object) -> TypeGuard[TLossMsg]:
    return hasattr(item, GENS)


def to_mapping(item: TItem) -> JsonlRecord:
    if is_instant(item):
        return {
            TYPE: item.type,
            NAME: item.name,
            TS: item.ts,
        }

    if is_loss(item):
        return {
            IID: item.iid,
            TS_START: item.ts_start,
            TS_STOP: item.ts_stop,
            GENS: [
                {
                    GEN: gen.gen,
                    OBSERVED_COUNT: gen.observed_count,
                    LOST_FROM: gen.lost_from,
                    LOST_COUNT: gen.lost_count,
                    LOST_PAUSE_NS: gen.lost_pause_ns,
                }
                for gen in item.gens
            ],
        }

    if is_gc_stats(item):
        # A live record: a struct sequence, so a tuple of its visible fields.
        if isinstance(item, tuple):
            names, hidden = structseq_fields(type(item))
            m: JsonlRecord = dict(zip(names, item, strict=True))
            if hidden:
                m.update((name, value) for name, value in structseq_hidden(item).items() if value is not None)
            return m

        # A record read back from a capture, holding None for every field it lacks.
        if isinstance(item, msgspec.Struct):
            return {name: value for name, value in msgspec.structs.asdict(item).items() if value is not None}

    raise NotImplementedError(f"Unknown item type: {type(item)}")
