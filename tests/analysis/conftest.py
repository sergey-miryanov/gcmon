"""Record builders for the analysis subsystem's tests."""

from __future__ import annotations

from gcmon.model.data import GCStatsInfo
from gcmon.model.names import (
    ALIVE_SIZE,
    CLEAR_WEAKREFS_COUNT,
    DELETED_GARBAGE_COUNT,
    FINALIZED_GARBAGE_COUNT,
    INCREMENT_SIZE,
    TS_CLEAR_WEAKREFS_STOP,
    TS_DEDUCE_UNREACHABLE_START,
    TS_DEDUCE_UNREACHABLE_STOP,
    TS_DELETE_GARBAGE_START,
    TS_DELETE_GARBAGE_STOP,
    TS_FILL_INCREMENT_START,
    TS_FILL_INCREMENT_STOP,
    TS_FINALIZE_GARBAGE_STOP,
    TS_HANDLE_RESURRECTED_STOP,
    TS_HANDLE_WEAKREF_CALLBACKS_START,
    TS_HANDLE_WEAKREF_CALLBACKS_STOP,
    TS_MARK_ALIVE_START,
    TS_MARK_ALIVE_STOP,
)
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
            INCREMENT_SIZE: increment_size,
            ALIVE_SIZE: alive_size,
            TS_MARK_ALIVE_START: ts_start,
            TS_MARK_ALIVE_STOP: ts_start + 100,
            TS_FILL_INCREMENT_START: ts_start + 100,
            TS_FILL_INCREMENT_STOP: ts_start + 200,
            TS_DEDUCE_UNREACHABLE_START: ts_start + 200,
            TS_DEDUCE_UNREACHABLE_STOP: ts_start + 300,
            TS_HANDLE_WEAKREF_CALLBACKS_START: ts_start + 300,
            TS_HANDLE_WEAKREF_CALLBACKS_STOP: ts_start + 400,
            TS_FINALIZE_GARBAGE_STOP: ts_start + 500,
            FINALIZED_GARBAGE_COUNT: 42,
            TS_HANDLE_RESURRECTED_STOP: ts_start + 600,
            TS_CLEAR_WEAKREFS_STOP: ts_start + 700,
            CLEAR_WEAKREFS_COUNT: 7,
            TS_DELETE_GARBAGE_START: ts_start + 800,
            TS_DELETE_GARBAGE_STOP: ts_start + 900,
            DELETED_GARBAGE_COUNT: 13,
        }
    )
    return record
