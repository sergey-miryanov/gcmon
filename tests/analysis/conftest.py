"""Record builders for the analysis subsystem's tests."""

from __future__ import annotations

from gcmon.model.data import GCStatsInfo
from gcmon.model.phases import (
    ClearWeakrefsSubPhase,
    DeduceUnreachableSubPhase,
    DeleteGarbageSubPhase,
    FillIncrementSubPhase,
    FinalizeGarbageSubPhase,
    HandleResurrectedSubPhase,
    HandleWeakrefsSubPhase,
    IncrementSizeField,
    MarkAliveSubPhase,
)
from tests.helpers import create_jsonl_record, create_mock_incremental_item


def make_inc_item(
    gen: int = 0,
    ts_start: int = 1000,
    ts_stop: int = 2000,
    increment_size: int = 500,
    alive_size: int = 300,
) -> GCStatsInfo:
    """A record whose sub-phases run back to back from *ts_start*.

    `make_inc_jsonl_record` below writes the same figures as a JSONL line, so
    a test can hand `combine` either shape and expect the same reading.
    """
    return create_mock_incremental_item(
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
            IncrementSizeField.INCREMENT_SIZE: increment_size,
            MarkAliveSubPhase.ALIVE_SIZE: alive_size,
            MarkAliveSubPhase.TS_MARK_ALIVE_START: ts_start,
            MarkAliveSubPhase.TS_MARK_ALIVE_STOP: ts_start + 100,
            FillIncrementSubPhase.TS_FILL_INCREMENT_START: ts_start + 100,
            FillIncrementSubPhase.TS_FILL_INCREMENT_STOP: ts_start + 200,
            DeduceUnreachableSubPhase.TS_DEDUCE_UNREACHABLE_START: ts_start + 200,
            DeduceUnreachableSubPhase.TS_DEDUCE_UNREACHABLE_STOP: ts_start + 300,
            HandleWeakrefsSubPhase.TS_HANDLE_WEAKREF_CALLBACKS_START: ts_start + 300,
            HandleWeakrefsSubPhase.TS_HANDLE_WEAKREF_CALLBACKS_STOP: ts_start + 400,
            FinalizeGarbageSubPhase.TS_FINALIZE_GARBAGE_STOP: ts_start + 500,
            FinalizeGarbageSubPhase.FINALIZED_GARBAGE_COUNT: 42,
            HandleResurrectedSubPhase.TS_HANDLE_RESURRECTED_STOP: ts_start + 600,
            ClearWeakrefsSubPhase.TS_CLEAR_WEAKREFS_STOP: ts_start + 700,
            ClearWeakrefsSubPhase.CLEAR_WEAKREFS_COUNT: 7,
            DeleteGarbageSubPhase.TS_DELETE_GARBAGE_START: ts_start + 800,
            DeleteGarbageSubPhase.TS_DELETE_GARBAGE_STOP: ts_start + 900,
            DeleteGarbageSubPhase.DELETED_GARBAGE_COUNT: 13,
        }
    )
    return record
