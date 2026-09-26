from collections.abc import Mapping, Sequence
from typing import Any, Protocol, TypeGuard

import msgspec

from .names import (
    ALIVE_SIZE,
    COLLECTIONS,
    GEN,
    GENS,
    IID,
    INCREMENT_SIZE,
    LOST_COUNT,
    LOST_FROM,
    LOST_PAUSE_NS,
    NAME,
    OBSERVED_COUNT,
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
)

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


def has_pause_ts(item: object) -> TypeGuard[TGCStatsInfo]:
    # A loss record carries `ts_start` too, and it is no GC record.
    return is_gc_stats(item) and getattr(item, TS_START, None) is not None


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
        # A live record: a struct sequence, so a tuple of its fields.
        if isinstance(item, tuple):
            # Any: nothing typed says a struct sequence names its fields.
            structseq: Any = type(item)
            return dict(zip(structseq.__match_args__, item, strict=True))

        # A record read back from a capture, holding None for every field it lacks.
        if isinstance(item, msgspec.Struct):
            return {name: value for name, value in msgspec.structs.asdict(item).items() if value is not None}

    raise NotImplementedError(f"Unknown item type: {type(item)}")
