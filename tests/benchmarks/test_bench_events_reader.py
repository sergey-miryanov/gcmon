"""Benchmarks for reaching a monitored process.

Benchmarks drive the real ``_remote_debugging`` against a real subprocess. A read
copies the whole fixed-size ring whatever the target is doing, so the target's
allocation rate does not move the measurement. Read ADR-0020 for more details.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from pytest_codspeed import BenchmarkFixture

from gcmon.model.protocol import TGCStatsInfo
from gcmon.monitoring.events_reader import RemoteEventsReader
from tests.monitoring.real_target import running_target

# Reads per measured call, enough that the work around the call does not set
# the figure. Both benchmarks repeat the same count, so the pair stays a ratio.
REPEATS = 50


@pytest.mark.benchmark
def test_remote_reader_reads_a_held_attachment(benchmark: BenchmarkFixture) -> None:
    """The steady state: every read after the first one of a pid.

    The first read is paid outside the measurement, so a regression that lets
    go of the attachment lands here as the attach cost below.
    """
    with running_target() as target:
        reader = RemoteEventsReader()
        reader.read(target.pid)

        def run() -> Sequence[TGCStatsInfo]:
            records: Sequence[TGCStatsInfo] = ()
            for _ in range(REPEATS):
                records = reader.read(target.pid)
            return records

        records = benchmark(run)

    assert {r.gen for r in records} == {0, 1, 2}, "a real read yields a row per generation"


@pytest.mark.benchmark
def test_remote_reader_attaches_and_reads(benchmark: BenchmarkFixture) -> None:
    """The first read of a pid, which is what gcmon used to pay every poll.

    Here to give the benchmark above something to be small against. It tracks
    CPython's attach cost rather than gcmon's, so a move in it is upstream news.
    """
    with running_target() as target:

        def run() -> Sequence[TGCStatsInfo]:
            records: Sequence[TGCStatsInfo] = ()
            for _ in range(REPEATS):
                records = RemoteEventsReader().read(target.pid)
            return records

        records = benchmark(run)

    assert {r.gen for r in records} == {0, 1, 2}, "a real read yields a row per generation"
