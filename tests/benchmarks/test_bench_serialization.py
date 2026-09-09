"""Benchmarks for the JSONL serialization / deserialization hot paths.

gcmon streams GC events to and from JSONL. These benchmarks cover the msgspec
based decode (``from_mapping``), the encode-side mapping (``to_mapping``), and a
full file read + trace-conversion round trip.
"""

from __future__ import annotations

from pathlib import Path

import msgspec
import pytest
from pytest_codspeed import BenchmarkFixture

from gcmon.analysis.jsonl_io import (
    convert_jsonl_to_trace_format,
    read_jsonl,
    write_jsonl,
)
from gcmon.model.data import from_mapping
from gcmon.model.protocol import TGCStatsInfo, TInstantMsg, to_mapping

from .conftest import make_gc_event, make_jsonl_record

EVENT_COUNT = 5_000


@pytest.mark.benchmark
def test_from_mapping_decode(benchmark: BenchmarkFixture) -> None:
    records = [make_jsonl_record(i) for i in range(EVENT_COUNT)]

    def run() -> int:
        count = 0
        for record in records:
            from_mapping(record)
            count += 1
        return count

    assert benchmark(run) == EVENT_COUNT


@pytest.mark.benchmark
def test_to_mapping_encode(benchmark: BenchmarkFixture) -> None:
    events = [make_gc_event(i, gen=i % 3) for i in range(EVENT_COUNT)]

    def run() -> int:
        count = 0
        for event in events:
            to_mapping(event)
            count += 1
        return count

    assert benchmark(run) == EVENT_COUNT


@pytest.mark.benchmark
def test_json_decode_and_from_mapping(benchmark: BenchmarkFixture) -> None:
    lines = [msgspec.json.encode(make_jsonl_record(i)) for i in range(EVENT_COUNT)]

    def run() -> int:
        count = 0
        for line in lines:
            from_mapping(msgspec.json.decode(line))
            count += 1
        return count

    assert benchmark(run) == EVENT_COUNT


@pytest.mark.benchmark
def test_read_jsonl(benchmark: BenchmarkFixture, tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    items: dict[int, list[TGCStatsInfo | TInstantMsg]] = {
        12345: [make_gc_event(i, gen=i % 3) for i in range(EVENT_COUNT)]
    }
    write_jsonl(path, items)

    result = benchmark(read_jsonl, path)
    assert sum(len(v) for v in result.values()) == EVENT_COUNT


@pytest.mark.benchmark
def test_convert_jsonl_to_trace_format(benchmark: BenchmarkFixture, tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    items: dict[int, list[TGCStatsInfo | TInstantMsg]] = {
        12345: [make_gc_event(i, gen=i % 3) for i in range(EVENT_COUNT)]
    }
    write_jsonl(path, items)

    result = benchmark(convert_jsonl_to_trace_format, path)
    assert len(result) > EVENT_COUNT
