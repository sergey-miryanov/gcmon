"""Record builders for the analysis subsystem's tests."""

from __future__ import annotations

from gcmon.model.data import GCStatsInfo
from tests.helpers import create_jsonl_record


def make_inc_item(
    gen: int = 0,
    ts_start: int = 1000,
    ts_stop: int = 2000,
    increment_size: int = 500,
    alive_size: int = 300,
) -> GCStatsInfo:
    return GCStatsInfo(
        gen=gen,
        iid=1,
        ts_start=ts_start,
        ts_stop=ts_stop,
        collections=1,
        heap_size=100,
        collected=10,
        uncollectable=0,
        candidates=5,
        duration=1.0,
        increment_size=increment_size,
        alive_size=alive_size,
        ts_mark_alive_start=ts_start,
        ts_mark_alive_stop=ts_start + 100,
        ts_fill_increment_start=ts_start + 100,
        ts_fill_increment_stop=ts_start + 200,
        ts_deduce_unreachable_start=ts_start + 200,
        ts_deduce_unreachable_stop=ts_start + 300,
        ts_handle_weakref_callbacks_start=ts_start + 300,
        ts_handle_weakref_callbacks_stop=ts_start + 400,
        ts_finalize_garbage_stop=ts_start + 500,
        finalized_garbage_count=42,
        ts_handle_resurrected_stop=ts_start + 600,
        ts_clear_weakrefs_stop=ts_start + 700,
        clear_weakrefs_count=7,
        ts_delete_garbage_start=ts_start + 800,
        ts_delete_garbage_stop=ts_start + 900,
        deleted_garbage_count=13,
    )


def make_inc_jsonl_record(
    pid: int = 1,
    gen: int = 0,
    ts_start: int = 1000,
    ts_stop: int = 2000,
    increment_size: int = 500,
    alive_size: int = 300,
) -> dict[str, int | float]:
    record = create_jsonl_record(pid=pid, gen=gen, ts_start=ts_start, ts_stop=ts_stop)
    record.update(
        {
            "increment_size": increment_size,
            "alive_size": alive_size,
            "ts_mark_alive_start": ts_start,
            "ts_mark_alive_stop": ts_start + 100,
            "ts_fill_increment_start": ts_start + 100,
            "ts_fill_increment_stop": ts_start + 200,
            "ts_deduce_unreachable_start": ts_start + 200,
            "ts_deduce_unreachable_stop": ts_start + 300,
            "ts_handle_weakref_callbacks_start": ts_start + 300,
            "ts_handle_weakref_callbacks_stop": ts_start + 400,
            "ts_finalize_garbage_stop": ts_start + 500,
            "finalized_garbage_count": 42,
            "ts_handle_resurrected_stop": ts_start + 600,
            "ts_clear_weakrefs_stop": ts_start + 700,
            "clear_weakrefs_count": 7,
            "ts_delete_garbage_start": ts_start + 800,
            "ts_delete_garbage_stop": ts_start + 900,
            "deleted_garbage_count": 13,
        }
    )
    return record
