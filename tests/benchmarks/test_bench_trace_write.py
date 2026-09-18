"""Benchmark for the encoder's write path.

Converting a batch, compressing it and appending it to the trace is what a
monitored process pays for being observed, and it runs on the flush, while the
target is still working.

Conversion is benchmarked on its own in ``test_bench_trace_conversion``, so a
move here that does not show up there is the write itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pytest_codspeed import BenchmarkFixture

from gcmon.exporters.encoder import ProtobufEventEncoder
from gcmon.exporters.trace_converter import convert_item_to_trace_format
from gcmon.model.trace_event import TraceEvent
from tests.helpers import proc

from .conftest import make_gc_event

PID = 12345
EVENTS_PER_BATCH = 100
BATCHES = 10


def _batches() -> list[list[TraceEvent]]:
    """One run's worth of flushes, converted the way the buffer hands them
    over: the process and thread descriptors reach the first batch only,
    because that is where the first event naming those tracks lands."""
    batches: list[list[TraceEvent]] = []
    for batch in range(BATCHES):
        events: list[TraceEvent] = []
        for i in range(EVENTS_PER_BATCH):
            events.extend(
                convert_item_to_trace_format(proc(PID), make_gc_event(batch * EVENTS_PER_BATCH + i, gen=i % 3))
            )
        batches.append(events)
    return batches


@pytest.mark.benchmark
def test_write_a_run_of_batches(benchmark: BenchmarkFixture, tmp_path: Path) -> None:
    batches = _batches()
    path = tmp_path / "bench.pftrace"

    def run() -> int:
        encoder = ProtobufEventEncoder(sequence_id=1)
        encoder.open(path)
        for events in batches:
            encoder.write_events(events)
        encoder.close()
        return path.stat().st_size

    assert benchmark(run) > 0
