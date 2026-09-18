"""Benchmarks for RSS sampler read latencies.

Validates that ``psutil.Process(memory_info).rss`` is fast enough at the
configured ``--rss-interval`` (1 Hz default).  Measures both self-PID
(same process) and child-PID (external process) to compare overhead.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Generator

import pytest
from pytest_codspeed import BenchmarkFixture

from gcmon.monitoring.rss_sampler import _default_rss_sampler, _noop_rss_sampler


@pytest.fixture(scope="module")
def child_pid() -> Generator[int]:
    """Start a child process that stays alive during the benchmark."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    yield proc.pid
    proc.terminate()
    proc.wait()


def loop(sampler: Callable[[int], int], pid: int) -> int:
    result = 0
    for _ in range(1000):
        result = sampler(pid)
    return result


@pytest.mark.benchmark
def test_rss_sampler_read_latency_self(benchmark: BenchmarkFixture) -> None:
    def _run(pid: int) -> int:
        return loop(_default_rss_sampler, pid)

    result = benchmark(_run, os.getpid())
    assert result > 0


@pytest.mark.benchmark
def test_rss_sampler_read_latency_child(benchmark: BenchmarkFixture, child_pid: int) -> None:
    def _run(pid: int) -> int:
        return loop(_default_rss_sampler, pid)

    result = benchmark(_run, child_pid)
    assert result > 0


@pytest.mark.benchmark
def test_rss_sampler_noop_latency(benchmark: BenchmarkFixture) -> None:
    def _run(pid: int) -> int:
        return loop(_noop_rss_sampler, pid)

    result = benchmark(_run, os.getpid())
    assert result == 0
