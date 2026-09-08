from collections.abc import Mapping, Sequence
from typing import Protocol, TypeGuard

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
    "TNewIncrementalInfo",
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
    "has_new_incremental",
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


class TNewIncrementalInfo(Protocol):
    old_work: int
    auto_collect: int
    aging_threshold: int
    aging_spaces: int
    aging_next: int
    survivor_count: int
    increment_size: int
    heap_size_stop: int


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
    return getattr(item, "ts_start", None) is not None


def has_incremental(item: object) -> TypeGuard[TIncrementalInfo]:
    return getattr(item, "ts_fill_increment_start", None) is not None


def has_mark_alive(item: object) -> TypeGuard[TMarkAliveInfo]:
    return getattr(item, "alive_size", None) is not None


def has_deduce_unreachable(item: object) -> TypeGuard[TDeduceUnreachableInfo]:
    return getattr(item, "ts_deduce_unreachable_start", None) is not None


def has_handle_weakrefs(item: object) -> TypeGuard[THandleWeakrefsInfo]:
    return getattr(item, "ts_handle_weakref_callbacks_start", None) is not None


def has_finalize_garbage(item: object) -> TypeGuard[TFinalizeGarbageInfo]:
    return getattr(item, "ts_finalize_garbage_stop", None) is not None


def has_handle_resurrected(item: object) -> TypeGuard[THandleResurrectedInfo]:
    return getattr(item, "ts_handle_resurrected_stop", None) is not None


def has_clear_weakrefs(item: object) -> TypeGuard[TClearWeakrefsInfo]:
    return getattr(item, "ts_clear_weakrefs_stop", None) is not None


def has_delete_garbage(item: object) -> TypeGuard[TDeleteGarbageInfo]:
    return getattr(item, "ts_delete_garbage_start", None) is not None


def has_new_incremental(item: object) -> TypeGuard[TNewIncrementalInfo]:
    return getattr(item, "old_work", None) is not None


def is_gc_stats(item: object) -> TypeGuard[TGCStatsInfo]:
    """A GC record is the one record type built around ``collections``."""
    return hasattr(item, "collections")


def is_instant(item: object) -> TypeGuard[TInstantMsg]:
    return hasattr(item, "type")


def is_loss(item: object) -> TypeGuard[TLossMsg]:
    return hasattr(item, "gens")


def to_mapping(item: TItem) -> JsonlRecord:
    if is_instant(item):
        return {
            "type": item.type,
            "name": item.name,
            "ts": item.ts,
        }

    if is_loss(item):
        return {
            "iid": item.iid,
            "ts_start": item.ts_start,
            "ts_stop": item.ts_stop,
            "gens": [
                {
                    "gen": gen.gen,
                    "observed_count": gen.observed_count,
                    "lost_from": gen.lost_from,
                    "lost_count": gen.lost_count,
                    "lost_pause_ns": gen.lost_pause_ns,
                }
                for gen in item.gens
            ],
        }

    if is_gc_stats(item):
        gen = item.gen
        m: JsonlRecord = {
            "gen": item.gen,
            "iid": item.iid,
            "ts_start": item.ts_start,
            "ts_stop": item.ts_stop,
            "heap_size": item.heap_size,
            "collections": item.collections,
            "collected": item.collected,
            "uncollectable": item.uncollectable,
            "candidates": item.candidates,
            "duration": item.duration,
        }

        if has_incremental(item):
            m["increment_size"] = item.increment_size
            m["ts_fill_increment_start"] = item.ts_fill_increment_start
            m["ts_fill_increment_stop"] = item.ts_fill_increment_stop

        if has_mark_alive(item):
            m["alive_size"] = item.alive_size
            m["ts_mark_alive_start"] = item.ts_mark_alive_start
            m["ts_mark_alive_stop"] = item.ts_mark_alive_stop

        if has_deduce_unreachable(item):
            m["ts_deduce_unreachable_start"] = item.ts_deduce_unreachable_start
            m["ts_deduce_unreachable_stop"] = item.ts_deduce_unreachable_stop

        if has_handle_weakrefs(item):
            m["ts_handle_weakref_callbacks_start"] = item.ts_handle_weakref_callbacks_start
            m["ts_handle_weakref_callbacks_stop"] = item.ts_handle_weakref_callbacks_stop

        if has_finalize_garbage(item):
            m["ts_finalize_garbage_stop"] = item.ts_finalize_garbage_stop
            m["finalized_garbage_count"] = item.finalized_garbage_count

        if has_handle_resurrected(item):
            m["ts_handle_resurrected_stop"] = item.ts_handle_resurrected_stop

        if has_clear_weakrefs(item):
            m["ts_clear_weakrefs_stop"] = item.ts_clear_weakrefs_stop
            m["clear_weakrefs_count"] = item.clear_weakrefs_count

        if has_delete_garbage(item):
            m["ts_delete_garbage_start"] = item.ts_delete_garbage_start
            m["ts_delete_garbage_stop"] = item.ts_delete_garbage_stop
            m["deleted_garbage_count"] = item.deleted_garbage_count

        if has_new_incremental(item):
            m["old_work"] = item.old_work
            m["auto_collect"] = item.auto_collect
            m["aging_threshold"] = item.aging_threshold
            m["aging_spaces"] = item.aging_spaces
            m["aging_next"] = item.aging_next
            m["survivor_count"] = item.survivor_count
            m["heap_size_stop"] = item.heap_size_stop
            if gen == 1:
                m["increment_size"] = item.increment_size

        return m

    raise NotImplementedError(f"Unknown item type: {type(item)}")
