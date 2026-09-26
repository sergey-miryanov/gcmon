from collections.abc import Mapping, Sequence
from typing import Any, Protocol, TypeGuard

import msgspec

from .names import (
    COLLECTIONS,
    GEN,
    GENS,
    IID,
    LOST_COUNT,
    LOST_FROM,
    LOST_PAUSE_NS,
    NAME,
    OBSERVED_COUNT,
    TS,
    TS_START,
    TS_STOP,
    TYPE,
)

__all__ = [
    "JsonlRecord",
    "TGCStatsInfo",
    "TGenLoss",
    "TInstantMsg",
    "TItem",
    "TLossMsg",
    "TMapping",
    "TScalar",
    "TValue",
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
