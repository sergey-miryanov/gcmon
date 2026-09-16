"""Shared fixtures and helpers for CodSpeed benchmarks.

These helpers build realistic GC event payloads so the benchmarks exercise the
same pure-Python hot paths gcmon runs when ingesting and exporting garbage
collection data captured from a monitored process.
"""

from __future__ import annotations

from typing import Any

from gcmon.model.data import GCStatsInfo
from gcmon.model.names import (
    ALIVE_SIZE,
    CANDIDATES,
    COLLECTED,
    COLLECTIONS,
    DURATION,
    GEN,
    HEAP_SIZE,
    IID,
    INCREMENT_SIZE,
    PID,
    TS_FILL_INCREMENT_START,
    TS_FILL_INCREMENT_STOP,
    TS_MARK_ALIVE_START,
    TS_MARK_ALIVE_STOP,
    TS_START,
    TS_STOP,
    UNCOLLECTABLE,
)


def make_gc_event(i: int, *, pid: int = 12345, iid: int = 0, gen: int = 0) -> GCStatsInfo:
    """Build a fully-populated GCStatsInfo resembling a real GC pause record.

    All optional phase timestamps are filled in so the trace conversion and
    stats-recording paths take their most expensive branches. Phase durations
    are microsecond-scale (hundreds of microseconds inside a 4 ms pause), which
    matches what real collections look like and keeps the recorded nanosecond
    values away from the degenerate sub-microsecond range.
    """
    base = 1_000_000_000 + i * 5_000_000
    return GCStatsInfo(
        gen=gen,
        iid=iid,
        ts_start=base,
        ts_stop=base + 4_000_000,
        heap_size=20_000 + i,
        collections=5 + i,
        collected=50 + i,
        uncollectable=i % 3,
        candidates=10 + i,
        duration=0.004,
        increment_size=1_024 + i,
        alive_size=2_048 + i,
        ts_mark_alive_start=base + 10_000,
        ts_mark_alive_stop=base + 810_000,
        ts_fill_increment_start=base + 820_000,
        ts_fill_increment_stop=base + 1_620_000,
        ts_deduce_unreachable_start=base + 1_650_000,
        ts_deduce_unreachable_stop=base + 2_450_000,
        ts_handle_weakref_callbacks_start=base + 2_500_000,
        ts_handle_weakref_callbacks_stop=base + 2_700_000,
        ts_finalize_garbage_stop=base + 2_900_000,
        finalized_garbage_count=i % 5,
        ts_handle_resurrected_stop=base + 3_100_000,
        ts_clear_weakrefs_stop=base + 3_300_000,
        clear_weakrefs_count=i % 4,
        ts_delete_garbage_start=base + 3_400_000,
        ts_delete_garbage_stop=base + 3_900_000,
        deleted_garbage_count=i % 6,
    )


def make_jsonl_record(i: int, *, pid: int = 12345, iid: int = 0, gen: int = 0) -> dict[str, Any]:
    """Build a JSONL record dict matching the on-disk gcmon export format."""
    base = 1_000_000_000 + i * 5_000_000
    return {
        PID: pid,
        "tid": iid,
        GEN: gen,
        IID: iid,
        TS_START: base,
        TS_STOP: base + 4_000_000,
        HEAP_SIZE: 20_000 + i,
        COLLECTIONS: 5 + i,
        COLLECTED: 50 + i,
        UNCOLLECTABLE: i % 3,
        CANDIDATES: 10 + i,
        DURATION: 0.004,
        INCREMENT_SIZE: 1_024 + i,
        ALIVE_SIZE: 2_048 + i,
        TS_MARK_ALIVE_START: base + 10_000,
        TS_MARK_ALIVE_STOP: base + 810_000,
        TS_FILL_INCREMENT_START: base + 820_000,
        TS_FILL_INCREMENT_STOP: base + 1_620_000,
    }
