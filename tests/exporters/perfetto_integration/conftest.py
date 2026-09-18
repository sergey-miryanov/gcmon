"""One fixture per trace in `traces.py`, opened in the real trace processor.

Every one is function-scoped. A trace processor holds the single trace it
was handed, so two tests asking different things of one trace each need it
opened again.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from perfetto.trace_processor import TraceProcessor

from tests.exporters.perfetto_integration.traces import (
    _FAKE_CMDLINE,
    _write_crossing_trace,
    _write_every_row_trace,
    _write_killed_run_trace,
    _write_liveness_only_trace,
    _write_liveness_trace,
    _write_nested_mark_trace,
    _write_pid_held_four_times_trace,
    _write_reused_pid_trace,
    _write_trace,
    _write_trace_no_instant,
    _write_trace_with_rss,
    _write_zero_duration_trace,
)
from tests.helpers import open_trace_processor


@pytest.fixture
def every_row_trace_processor(tmp_path: Path) -> Iterator[TraceProcessor]:
    with open_trace_processor(_write_every_row_trace(tmp_path)) as tp:
        yield tp


@pytest.fixture
def crossing_trace_processor(tmp_path: Path) -> Iterator[TraceProcessor]:
    path = _write_crossing_trace(tmp_path)
    with open_trace_processor(path) as tp:
        yield tp


@pytest.fixture
def nested_mark_trace_processor(tmp_path: Path) -> Iterator[TraceProcessor]:
    path = _write_nested_mark_trace(tmp_path)
    with open_trace_processor(path) as tp:
        yield tp


@pytest.fixture
def zero_duration_trace_processor(tmp_path: Path) -> Iterator[TraceProcessor]:
    path = _write_zero_duration_trace(tmp_path)
    with open_trace_processor(path) as tp:
        yield tp


@pytest.fixture
def liveness_trace_processor(tmp_path: Path) -> Iterator[TraceProcessor]:
    path = _write_liveness_trace(tmp_path)
    with open_trace_processor(path) as tp:
        yield tp


@pytest.fixture
def killed_run_trace_processor(tmp_path: Path) -> Iterator[TraceProcessor]:
    path = _write_killed_run_trace(tmp_path)
    with open_trace_processor(path) as tp:
        yield tp


@pytest.fixture
def liveness_only_trace_processor(tmp_path: Path) -> Iterator[TraceProcessor]:
    path = _write_liveness_only_trace(tmp_path)
    with open_trace_processor(path) as tp:
        yield tp


@pytest.fixture
def reused_pid_trace_processor(tmp_path: Path) -> Iterator[TraceProcessor]:
    path = _write_reused_pid_trace(tmp_path)
    with open_trace_processor(path) as tp:
        yield tp


@pytest.fixture
def trace_processor(tmp_path: Path) -> Iterator[TraceProcessor]:
    path = _write_trace(tmp_path)
    with open_trace_processor(path) as tp:
        yield tp


@pytest.fixture
def trace_processor_with_cmdline(
    tmp_path: Path,
) -> Iterator[TraceProcessor]:
    path = _write_trace(tmp_path, cmdline=_FAKE_CMDLINE)
    with open_trace_processor(path) as tp:
        yield tp


@pytest.fixture
def trace_processor_no_instant(
    tmp_path: Path,
) -> Iterator[TraceProcessor]:
    path = _write_trace_no_instant(tmp_path, _FAKE_CMDLINE)
    with open_trace_processor(path) as tp:
        yield tp


@pytest.fixture
def pid_held_four_times_trace_processor(tmp_path: Path) -> Iterator[TraceProcessor]:
    path = _write_pid_held_four_times_trace(tmp_path)
    with open_trace_processor(path) as tp:
        yield tp


@pytest.fixture
def trace_processor_with_rss(tmp_path: Path) -> Iterator[TraceProcessor]:
    path = _write_trace_with_rss(tmp_path)
    with open_trace_processor(path) as tp:
        yield tp
