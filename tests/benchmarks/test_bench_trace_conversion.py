"""Benchmarks for converting GC stats into Trace Event objects.

Converting captured GC records into Chrome Trace / Perfetto events is the main
CPU cost of every ``gcmon convert`` and export. These benchmarks measure both
per-item conversion and bulk conversion of a whole capture.
"""

from __future__ import annotations

import pytest
from pytest_codspeed import BenchmarkFixture

from gcmon.exporters.trace_converter import (
    convert_item_to_trace_format,
    convert_to_trace_format,
)
from gcmon.model.protocol import TGCStatsInfo, TInstantMsg
from tests.conftest import DEFAULT_PID
from tests.helpers import proc

from .conftest import make_gc_event

pytestmark = pytest.mark.benchmark

EVENT_COUNT = 5_000
ITEM_BATCH = 200


def test_convert_item_to_trace_format_batch(benchmark: BenchmarkFixture) -> None:
    """Per-item conversion, amortised over a batch.

    The measured callable used to convert one record. A single conversion grows
    the interpreter's event list once, so whichever call happened to trip the
    allocator into consolidating its free lists absorbed the cost of every chunk
    freed before it, a figure that moved with the process's heap history rather
    than with this function, and that flipped on branches touching nothing on
    the conversion path. Converting a batch spreads that over ``ITEM_BATCH``
    calls, so the number tracks the converter. Renamed on the way past: the unit
    is no longer one call, so the old series would not have continued honestly.
    """
    events = [make_gc_event(i, gen=i % 3) for i in range(ITEM_BATCH)]

    def run() -> int:
        count = 0
        for event in events:
            count += len(convert_item_to_trace_format(proc(DEFAULT_PID), event))
        return count

    assert benchmark(run) > ITEM_BATCH


def test_convert_to_trace_format_single_pid(benchmark: BenchmarkFixture) -> None:
    items: dict[int, list[TGCStatsInfo | TInstantMsg]] = {
        12345: [make_gc_event(i, gen=i % 3) for i in range(EVENT_COUNT)]
    }

    result = benchmark(convert_to_trace_format, items)
    assert len(result) > EVENT_COUNT


def test_convert_to_trace_format_many_pids(benchmark: BenchmarkFixture) -> None:
    items: dict[int, list[TGCStatsInfo | TInstantMsg]] = {}
    for i in range(EVENT_COUNT):
        pid = 1000 + (i % 16)
        iid = i % 4
        items.setdefault(pid, []).append(make_gc_event(i, pid=pid, iid=iid, gen=i % 3))

    result = benchmark(convert_to_trace_format, items)
    assert len(result) > EVENT_COUNT
