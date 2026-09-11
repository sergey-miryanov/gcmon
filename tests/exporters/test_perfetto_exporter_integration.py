"""Tests for the Perfetto trace exporter that drive the real
``perfetto.trace_processor`` binary against synthetic ``GCStatsInfo`` traces
and assert on the SQL tables (``slice``, ``args``, ``track``,
``counter_track``) the trace processor exposes.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from perfetto.trace_processor import TraceProcessor

from gcmon.exporters import PerfettoExporter
from gcmon.exporters.trace_converter import duration_text
from tests.conftest import DEFAULT_PID
from tests.data_helpers import create_instant_msg
from tests.helpers import (
    create_mock_incremental_item,
    create_mock_loss_item,
    create_mock_stats_item,
    open_trace_processor,
    proc,
)

_PAUSE_NAME: str = "GC Pause(0)"
_INSTANT_NAME: str = "GC monitor started"
_SECOND_PID: int = 67890
_THIRD_PID: int = 54321

_GEN: int = 0
_IID: int = 0
_COLLECTIONS: int = 5
_HEAP_SIZE: int = 1000
_COLLECTED: int = 10
_UNCOLLECTABLE: int = 2
_CANDIDATES: int = 3
_DURATION: float = 0.005
_TS_START: int = 1_500_000_000
_TS_STOP: int = 1_500_005_000

_EXPECTED_PAUSE_ARGS: dict[str, int] = {
    "generation": _GEN,
    "iid": _IID,
    "collections": _COLLECTIONS,
    "heap_size": _HEAP_SIZE,
    "collected": _COLLECTED,
    "uncollectable": _UNCOLLECTABLE,
    "candidates": _CANDIDATES,
}

_EXPECTED_COUNTER_NAMES: frozenset[str] = frozenset(
    {
        "G0 collected",
        "G0 uncollectable",
        "G0 candidates",
        "G0 duration",
        "G1 collected",
        "G1 uncollectable",
        "G1 candidates",
        "G1 duration",
        # One row per interpreter, all three named the same: each hangs off
        # its own group, so the name has nothing to tell apart (ADR-0027).
        "heap_size",
    }
)

# The namespace the trace processor puts a debug annotation under.
_ARG_PREFIX: str = "debug"

_FAKE_CMDLINE: tuple[str, ...] = ("python3", "-m", "fake_target")
_FAKE_CMDLINE_JOINED: str = " ".join(_FAKE_CMDLINE)

# Name of the slice drawn on each process's own row over the interval
# gcmon observed that process. Must match ``_PROCESS_ROW_SLICE_NAME`` in
# ``gcmon.exporters.perfetto_process_lifetime``.
_PROCESS_ROW_SLICE_NAME: str = "Lifetime"

# Name of the shared top-level Perfetto track that holds one slice per
# pid spanning the first-to-last non-meta event timestamps for that
# pid. Must match ``_PROCESS_LIFETIME_TRACK_NAME`` in
# ``gcmon.exporters.perfetto_process_lifetime``.
_PROCESS_LIFETIME_TRACK_NAME: str = "Processes"


def _process_filter(pid: int) -> str:
    """SQL fragment to scope a query to a single ``pid``.

    Every track gcmon writes belongs to a process rather than to a thread
    (ADR-0027), so one join through ``process_track`` reaches them all: the
    process's own row, and the rows nested under its ``Python
    Interpreters`` group, which the trace processor gives the same ``upid``.

    Scoped on the name, not on ``process.pid``: that column holds the pid
    gcmon writes for the row rather than the operating system's (ADR-0011).
    Every fixture using this holds one process per pid, so the unsuffixed
    name is the whole of it; a run that handed a pid on names the successors
    ``Process <pid>#2`` and up.
    """
    return (
        f"JOIN process_track pt ON s.track_id = pt.id JOIN process p ON pt.upid = p.upid WHERE p.name = 'Process {pid}'"
    )


def _process_row_filter(pid: int) -> str:
    """SQL fragment to scope a query to a process's *own* row.

    :func:`_process_filter` reaches every row under the process's ``upid``,
    the ones inside its ``Python Interpreters`` group included. This one
    stops at the process track itself, which carries the ``Lifetime`` bar, the marks
    and the RSS (ADR-0024).
    """
    return f"JOIN process_track pt ON s.track_id = pt.id WHERE pt.name = 'Process {pid}'"


def _on_interpreter(iid: int) -> str:
    """SQL fragment narrowing a :func:`_process_filter` query to one
    interpreter.

    Every interpreter's pauses are drawn on a row named ``GC Pauses``, so
    what names the interpreter is the group that row hangs off (ADR-0027).
    """
    return f"AND EXISTS (SELECT 1 FROM track ig WHERE ig.id = pt.parent_id AND ig.name = 'Interpreter {iid}')"


def _process_filter_instant(pid: int) -> str:
    """SQL fragment to scope an instant-event query to a single ``pid``.

    :func:`_process_filter` narrowed to the zero-width slices, which is what
    an instant event (e.g. ``GC monitor started``) reads back as.
    """
    return (
        f"JOIN process_track pt ON s.track_id = pt.id "
        f"JOIN process p ON pt.upid = p.upid "
        f"WHERE p.name = 'Process {pid}' AND s.dur = 0"
    )


def _write_trace(tmp: Path, cmdline: tuple[str, ...] | None = None) -> Path:
    path = tmp / "trace.pb"
    exporter = PerfettoExporter(output_path=path, flush_threshold=1000)
    exporter.add_process_cmdline(proc(DEFAULT_PID), cmdline)
    exporter.add_process_cmdline(proc(_SECOND_PID), cmdline)
    exporter.add_instant_event(
        proc(DEFAULT_PID),
        create_instant_msg(name=_INSTANT_NAME, ts=_TS_START - 1_000_000),
    )
    exporter.add_event(
        proc(DEFAULT_PID),
        create_mock_stats_item(
            gen=_GEN,
            iid=_IID,
            collections=_COLLECTIONS,
            collected=_COLLECTED,
            uncollectable=_UNCOLLECTABLE,
            candidates=_CANDIDATES,
            heap_size=_HEAP_SIZE,
        ),
    )
    exporter.add_event(proc(DEFAULT_PID), create_mock_incremental_item(gen=1, iid=1))
    exporter.add_event(
        proc(DEFAULT_PID),
        create_mock_stats_item(
            gen=_GEN,
            iid=2,
            collections=_COLLECTIONS,
            collected=_COLLECTED,
            uncollectable=_UNCOLLECTABLE,
            candidates=_CANDIDATES,
            heap_size=_HEAP_SIZE,
        ),
    )
    exporter.add_instant_event(
        proc(_SECOND_PID),
        create_instant_msg(name=_INSTANT_NAME, ts=_TS_START - 2_000_000),
    )
    exporter.add_event(
        proc(_SECOND_PID),
        create_mock_stats_item(
            gen=_GEN,
            iid=0,
            collections=_COLLECTIONS,
            collected=_COLLECTED,
            uncollectable=_UNCOLLECTABLE,
            candidates=_CANDIDATES,
            heap_size=_HEAP_SIZE,
        ),
    )
    exporter.close()
    return path


def _write_trace_no_instant(tmp: Path, cmdline: tuple[str, ...] | None) -> Path:
    path = tmp / "trace.pb"
    exporter = PerfettoExporter(output_path=path, flush_threshold=1000)
    exporter.add_process_cmdline(proc(DEFAULT_PID), cmdline)
    exporter.add_process_cmdline(proc(_SECOND_PID), cmdline)
    exporter.add_event(
        proc(DEFAULT_PID),
        create_mock_stats_item(
            gen=_GEN,
            iid=_IID,
            collections=_COLLECTIONS,
            collected=_COLLECTED,
            uncollectable=_UNCOLLECTABLE,
            candidates=_CANDIDATES,
            heap_size=_HEAP_SIZE,
        ),
    )
    exporter.add_event(
        proc(_SECOND_PID),
        create_mock_stats_item(
            gen=_GEN,
            iid=0,
            collections=_COLLECTIONS,
            collected=_COLLECTED,
            uncollectable=_UNCOLLECTABLE,
            candidates=_CANDIDATES,
            heap_size=_HEAP_SIZE,
        ),
    )
    exporter.close()
    return path


def _misplaced_end_events(tp: TraceProcessor) -> int:
    """Return the trace processor's ``misplaced_end_event`` counter.

    The ``stats`` table is the trace processor's own diagnostics: each
    row is a named counter the parser bumps when it hits something
    wrong. ``misplaced_end_event`` (severity ``data_loss``) counts slice
    ENDs that had nothing to close and were therefore thrown away.
    """
    rows = list(tp.query("SELECT value FROM stats WHERE name = 'misplaced_end_event'"))
    return int(rows[0].value) if rows else 0


# Timestamps for the crossing-span trace: pid A is observed first and
# dies first, but pid B starts while A is still running, so the two
# spans cross rather than nest.
_CROSS_A_START: int = 100_000_000
_CROSS_B_START: int = 200_000_000
_CROSS_A_STOP: int = 400_000_000
_CROSS_B_STOP: int = 600_000_000


def _write_crossing_trace(tmp: Path) -> Path:
    """Write a Perfetto trace whose two pids have crossing spans."""
    path = tmp / "crossing.pb"
    exporter = PerfettoExporter(output_path=path, flush_threshold=1000)
    for pid, ts in (
        (DEFAULT_PID, _CROSS_A_START),
        (_SECOND_PID, _CROSS_B_START),
        (DEFAULT_PID, _CROSS_A_STOP),
        (_SECOND_PID, _CROSS_B_STOP),
    ):
        exporter.add_instant_event(proc(pid), create_instant_msg(name=_INSTANT_NAME, ts=ts))
    exporter.close()
    return path


@pytest.fixture
def crossing_trace_processor(tmp_path: Path) -> Iterator[TraceProcessor]:
    path = _write_crossing_trace(tmp_path)
    with open_trace_processor(path) as tp:
        yield tp


# Timestamps for the zero-duration trace. _THIRD_PID is seen at a single
# instant, so its span is zero-length as observed. DEFAULT_PID's span is
# clipped to zero by _SECOND_PID starting one nanosecond later.
_ZERO_INSTANT_TS: int = 100_000_000
_ZERO_CLIPPED_START: int = 300_000_000
_ZERO_CLIPPED_STOP: int = 800_000_000
_ZERO_CROSSER_START: int = 300_000_001
# The crosser must *outlive* the clipped span, or the two nest instead
# of crossing and nothing is clipped at all.
_ZERO_CROSSER_STOP: int = 900_000_000


def _write_zero_duration_trace(tmp: Path) -> Path:
    """Write a Perfetto trace containing both ways a ``Processes`` slice
    can end up zero-length: a pid observed at a single instant, and a pid
    clipped down to nothing by a pid starting 1ns later."""
    path = tmp / "zero.pb"
    exporter = PerfettoExporter(output_path=path, flush_threshold=1000)
    for pid, ts in (
        (_THIRD_PID, _ZERO_INSTANT_TS),
        (DEFAULT_PID, _ZERO_CLIPPED_START),
        (_SECOND_PID, _ZERO_CROSSER_START),
        (DEFAULT_PID, _ZERO_CLIPPED_STOP),
        (_SECOND_PID, _ZERO_CROSSER_STOP),
    ):
        exporter.add_instant_event(proc(pid), create_instant_msg(name=_INSTANT_NAME, ts=ts))
    exporter.close()
    return path


# A process polled either side of the collection it ran and the mark its
# workload wrote. Every other fixture's marks *are* the observations that bound
# the span, so they land on its edge; here liveness widens the span and both
# fall strictly inside it.
_MARK_SPAN_START: int = 700_000_000
_MARK_GC_START: int = 710_000_000
_MARK_GC_STOP: int = 715_000_000
_MARK_TS: int = 750_000_000
_MARK_SPAN_STOP: int = 800_000_000


def _write_nested_mark_trace(tmp: Path) -> Path:
    path = tmp / "nested_mark.pb"
    exporter = PerfettoExporter(output_path=path, flush_threshold=1000)
    exporter.add_process_liveness({proc(DEFAULT_PID)}, _MARK_SPAN_START)
    exporter.add_event(
        proc(DEFAULT_PID),
        create_mock_stats_item(gen=_GEN, iid=_IID, ts_start=_MARK_GC_START, ts_stop=_MARK_GC_STOP),
    )
    exporter.add_instant_event(proc(DEFAULT_PID), create_instant_msg(name=_INSTANT_NAME, ts=_MARK_TS))
    exporter.add_process_liveness({proc(DEFAULT_PID)}, _MARK_SPAN_STOP)
    exporter.close()
    return path


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


# Timestamps for the liveness trace. DEFAULT_PID collects once, early,
# and is then merely observed for the rest of the run; _SECOND_PID is
# only ever observed. Both spans start at the first observation and end
# at the last, but DEFAULT_PID's reaches back to a GC event that
# happened before gcmon ever polled it -- get_gc_stats returns
# collections that already happened.
_LIVE_TICKS: tuple[int, ...] = (300_000_000, 400_000_000, 500_000_000)
_LIVE_GC_START: int = 100_000_000
_LIVE_GC_STOP: int = 200_000_000
# Distinct per process, so a descriptor built at close can be caught carrying
# the wrong one.
_LIVE_BUSY_CMDLINE: tuple[str, ...] = ("python3", "-m", "busy_target")
_LIVE_QUIET_CMDLINE: tuple[str, ...] = ("python3", "-m", "quiet_target")


def _write_liveness_trace(tmp: Path) -> Path:
    """Write a Perfetto trace where one pid has both events and liveness
    and another has liveness only."""
    path = tmp / "liveness.pb"
    exporter = PerfettoExporter(output_path=path, flush_threshold=1000)
    exporter.add_process_cmdline(proc(DEFAULT_PID), _LIVE_BUSY_CMDLINE)
    exporter.add_process_cmdline(proc(_SECOND_PID), _LIVE_QUIET_CMDLINE)
    exporter.add_event(
        proc(DEFAULT_PID),
        create_mock_stats_item(
            gen=_GEN,
            iid=_IID,
            ts_start=_LIVE_GC_START,
            ts_stop=_LIVE_GC_STOP,
        ),
    )
    for ts in _LIVE_TICKS:
        exporter.add_process_liveness({proc(DEFAULT_PID), proc(_SECOND_PID)}, ts)
    exporter.close()
    return path


@pytest.fixture
def liveness_trace_processor(tmp_path: Path) -> Iterator[TraceProcessor]:
    path = _write_liveness_trace(tmp_path)
    with open_trace_processor(path) as tp:
        yield tp


def _write_liveness_only_trace(tmp: Path) -> Path:
    """Write a Perfetto trace with no events whatsoever: every pid
    answered every poll and none of them ever collected."""
    path = tmp / "liveness_only.pb"
    exporter = PerfettoExporter(output_path=path, flush_threshold=1000)
    for ts in _LIVE_TICKS:
        exporter.add_process_liveness({proc(DEFAULT_PID), proc(_SECOND_PID)}, ts)
    exporter.close()
    return path


# A run that never reaches ``close()``: one process retired part way through,
# one still running, and then the trace stops where a SIGKILL would stop it.
# ``flush_threshold=1`` so the batch after the retirement is written, which is
# where the retired process's row goes.
_KILL_GC_START: int = 600_000_000
_KILL_GC_STOP: int = 610_000_000
_KILL_TICK: int = 650_000_000
_KILL_LAST_GC_START: int = 700_000_000
_KILL_LAST_GC_STOP: int = 710_000_000


def _write_killed_run_trace(tmp: Path) -> Path:
    path = tmp / "killed.pb"
    exporter = PerfettoExporter(output_path=path, flush_threshold=1)
    for pid in (DEFAULT_PID, _SECOND_PID):
        exporter.add_process_cmdline(proc(pid), _FAKE_CMDLINE)
        exporter.add_event(
            proc(pid),
            create_mock_stats_item(gen=_GEN, iid=_IID, ts_start=_KILL_GC_START, ts_stop=_KILL_GC_STOP),
        )
    exporter.add_process_liveness({proc(DEFAULT_PID), proc(_SECOND_PID)}, _KILL_TICK)
    exporter.add_process_retired(proc(_SECOND_PID))
    exporter.add_event(
        proc(DEFAULT_PID),
        create_mock_stats_item(gen=_GEN, iid=_IID, ts_start=_KILL_LAST_GC_START, ts_stop=_KILL_LAST_GC_STOP),
    )
    # No close(). Everything after this point is what the kill takes.
    return path


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


# A pid the operating system handed out twice. The first process collects
# and dies; the second claims the pid and collects again, running a
# different program. Every value below differs between the two, so an
# assertion that reads one where it should read the other fails rather
# than passing on a number they happen to share.
_REUSED_PID: int = 24680
_REUSE_FIRST_CMDLINE: tuple[str, ...] = ("python3", "-m", "first_target")
_REUSE_SECOND_CMDLINE: tuple[str, ...] = ("python3", "-m", "second_target")
_REUSE_FIRST_START: int = 100_000_000
_REUSE_FIRST_STOP: int = 140_000_000
_REUSE_SECOND_START: int = 300_000_000
_REUSE_SECOND_STOP: int = 340_000_000
_REUSE_FIRST_COLLECTED: int = 11
_REUSE_SECOND_COLLECTED: int = 22
_REUSE_LOSS_WINDOW_NS: int = 10_000_000
_REUSE_LOST_PAUSE_NS: int = 3_316_458_100

_REUSE_FIRST_NAME: str = f"Process {_REUSED_PID}"
_REUSE_SECOND_NAME: str = f"Process {_REUSED_PID}#2"


def _write_reused_pid_trace(tmp: Path) -> Path:
    """Write a Perfetto trace for a run where one pid named two
    processes."""
    path = tmp / "reused.pb"
    exporter = PerfettoExporter(output_path=path, flush_threshold=1000)
    first, second = proc(_REUSED_PID, 1), proc(_REUSED_PID, 2)
    exporter.add_process_cmdline(first, _REUSE_FIRST_CMDLINE)
    exporter.add_process_cmdline(second, _REUSE_SECOND_CMDLINE)
    for process, ts_start, ts_stop, collected in (
        (first, _REUSE_FIRST_START, _REUSE_FIRST_STOP, _REUSE_FIRST_COLLECTED),
        (second, _REUSE_SECOND_START, _REUSE_SECOND_STOP, _REUSE_SECOND_COLLECTED),
    ):
        exporter.add_event(
            process,
            create_mock_stats_item(
                gen=_GEN,
                iid=_IID,
                ts_start=ts_start,
                ts_stop=ts_stop,
                collected=collected,
            ),
        )
        exporter.add_loss_event(
            process,
            create_mock_loss_item(
                iid=_IID,
                gen=_GEN,
                ts_start=ts_stop,
                ts_stop=ts_stop + _REUSE_LOSS_WINDOW_NS,
                lost_count=collected,
                lost_pause_ns=_REUSE_LOST_PAUSE_NS,
            ),
        )
    exporter.close()
    return path


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


class TestSliceArgs:
    """The GC Pause slice carries all pause args visible to the trace processor."""

    def test_pause_slice_exists(self, trace_processor: TraceProcessor) -> None:
        rows = list(
            trace_processor.query(
                f"SELECT s.name FROM slice s {_process_filter(DEFAULT_PID)} AND s.name = '{_PAUSE_NAME}'"
            )
        )
        assert len(rows) == 2, f"expected two '{_PAUSE_NAME}' slices, got {rows}"

    def test_pause_slice_has_all_expected_args(
        self,
        trace_processor: TraceProcessor,
    ) -> None:
        prefix = _ARG_PREFIX
        rows = {
            r.flat_key: r.int_value
            for r in trace_processor.query(
                f"SELECT flat_key, int_value FROM args "
                f"WHERE arg_set_id IN ("
                f"  SELECT s.arg_set_id FROM slice s "
                f"  {_process_filter(DEFAULT_PID)} "
                f"  AND s.name = '{_PAUSE_NAME}' AND s.dur > 0 "
                f"  {_on_interpreter(0)}"
                f")"
            )
        }
        for key, expected in _EXPECTED_PAUSE_ARGS.items():
            qualified = f"{prefix}.{key}"
            assert qualified in rows, f"missing arg {qualified}; got {sorted(rows)}"
            assert rows[qualified] == expected, f"{qualified}: expected {expected}, got {rows[qualified]}"

    def test_full_gen1_pause_slice_exists(
        self,
        trace_processor: TraceProcessor,
    ) -> None:
        rows = list(
            trace_processor.query(
                f"SELECT s.name FROM slice s {_process_filter(DEFAULT_PID)} AND s.name = 'GC Pause(1)'"
            )
        )
        assert len(rows) == 1, f"expected exactly one 'GC Pause(1)' slice, got {rows}"

    def test_full_fields_pause_encodes_all_optional_fields(
        self,
        trace_processor: TraceProcessor,
    ) -> None:
        expected_sub_slices = [
            "Mark Alive(1)",
            "Fill increment(1)",
            "Deduce Unreachable(1)",
            "Handle Weakrefs Callbacks(1)",
            "Finalize Garbage(1)",
            "Handle Resurrected(1)",
            "Clear Weakrefs(1)",
            "Delete Garbage(1)",
        ]
        slice_names = {
            r.name for r in trace_processor.query(f"SELECT DISTINCT s.name FROM slice s {_process_filter(DEFAULT_PID)}")
        }
        missing = set(expected_sub_slices) - slice_names
        assert not missing, f"missing sub-slices: {missing}"

    def test_deduce_unreachable_slice_args_has_candidates(
        self,
        trace_processor: TraceProcessor,
    ) -> None:
        prefix = _ARG_PREFIX
        rows = {
            r.flat_key: r.int_value
            for r in trace_processor.query(
                "SELECT flat_key, int_value FROM args "
                "WHERE arg_set_id IN ("
                f"  SELECT s.arg_set_id FROM slice s "
                f"  {_process_filter(DEFAULT_PID)} "
                "  AND s.name = 'Deduce Unreachable(1)' AND s.dur > 0 "
                f"  {_on_interpreter(1)}"
                ")"
            )
        }
        assert f"{prefix}.candidates" in rows, (
            f"missing {prefix}.candidates on Deduce Unreachable(1); got {sorted(rows)}"
        )

        prefix = _ARG_PREFIX
        pause_args = {
            r.flat_key: r.int_value
            for r in trace_processor.query(
                "SELECT flat_key, int_value FROM args "
                "WHERE arg_set_id IN ("
                "  SELECT s.arg_set_id FROM slice s "
                f"  {_process_filter(DEFAULT_PID)} "
                "  AND s.name = 'GC Pause(1)' AND s.dur > 0 "
                f"  {_on_interpreter(1)}"
                ")"
            )
        }
        for key in (
            "increment_size",
            "alive_size",
            "finalized_garbage_count",
            "deleted_garbage_count",
            "clear_weakrefs_count",
        ):
            qualified = f"{prefix}.{key}"
            assert qualified in pause_args, f"missing arg {qualified}; got {sorted(pause_args)}"


class TestCounterTracks:
    """The per-gen counter metrics (collected/uncollectable/candidates/
    duration) each have a counter track with the expected name, plus a
    shared `heap_size` track per (pid, tid). No extra counter tracks are
    emitted; in particular `increment_size` is not a counter track (it
    lives on the pause slice's args). The set comparison is robust to
    multiple processes emitting the same counter-track names."""

    def test_counter_track_names_match_expected(
        self,
        trace_processor: TraceProcessor,
    ) -> None:
        rows = {r.name for r in trace_processor.query("SELECT name FROM counter_track")}
        # One `heap_size` row per interpreter, each naming its own. The two
        # are siblings under the process track, so unqualified they would
        # read as one row drawn twice.
        normalized = {r.strip() for r in rows}
        missing = _EXPECTED_COUNTER_NAMES - normalized
        unexpected = normalized - _EXPECTED_COUNTER_NAMES
        assert not missing and not unexpected, (
            f"counter track names mismatch; missing: {missing or 'none'}; unexpected: {unexpected or 'none'}"
        )

    def test_uncollectable_counter_omitted_when_zero(
        self,
        tmp_path: Path,
    ) -> None:
        path = tmp_path / "trace.pb"
        exporter = PerfettoExporter(
            output_path=path,
            flush_threshold=1000,
        )
        exporter.add_event(
            proc(DEFAULT_PID),
            create_mock_stats_item(
                gen=0,
                iid=0,
                uncollectable=0,
                heap_size=_HEAP_SIZE,
            ),
        )
        exporter.close()
        with open_trace_processor(path) as tp:
            names = {r.name for r in tp.query("SELECT name FROM counter_track")}
            assert "G0 uncollectable" not in {n.strip() for n in names}, (
                f"uncollectable counter should be omitted when 0; got {names}"
            )
            assert "G0 collected" in {n.strip() for n in names}
            assert "G0 candidates" in {n.strip() for n in names}

    def test_duration_counter_track_present(
        self,
        trace_processor: TraceProcessor,
    ) -> None:
        names = {
            r.name.strip()
            for r in trace_processor.query(
                "SELECT name FROM counter_track",
            )
        }
        for gen in (0, 1):
            assert f"G{gen} duration" in names, f"G{gen} duration counter should be present; got {names}"
        assert "duration" not in names, f"shared 'duration' counter should NOT be present; got {names}"

    def test_duration_counter_value_is_double(
        self,
        trace_processor: TraceProcessor,
    ) -> None:
        # The `counter` table stores both int and double values in a single
        # `value` column (DOUBLE). For the per-gen `G0 duration` track, that
        # value should equal the per-pause duration (0.005 for the default
        # fixture).
        rows = list(
            trace_processor.query(
                "SELECT id, name FROM counter_track WHERE name = 'G0 duration'",
            )
        )
        assert rows, "no G0 duration counter track found"
        for r in rows:
            values = list(
                trace_processor.query(
                    f"SELECT value FROM counter WHERE track_id = {r.id}",
                )
            )
            assert values, f"no counter values for G0 duration track {r.id}"
            assert any(abs(v.value - 0.005) < 1e-9 for v in values)

    def test_duration_counter_parented_to_gc_metrics_group(
        self,
        trace_processor: TraceProcessor,
    ) -> None:
        # Every per-gen `G{gen} duration` track should be parented to a
        # `GC Metrics` group (one per pid/iid combination).
        rows = list(
            trace_processor.query(
                "SELECT id, parent_id, name FROM track WHERE name LIKE 'G_ duration'",
            )
        )
        assert rows, "no G{gen} duration tracks found"
        for r in rows:
            assert r.parent_id, f"{r.name} track has no parent"
            parents = list(
                trace_processor.query(
                    f"SELECT name FROM track WHERE id = {r.parent_id}",
                )
            )
            assert len(parents) == 1
            assert parents[0].name == "GC Metrics"


class TestCounterYAxisShareKey:
    """SQL-level tests for the new ``y_axis_share_key`` field on
    ``CounterDescriptor``.

    The wire-level tests in ``TestCounterTrackYAxisShareKey``
    (``test_perfetto_counter_tracks.py``) are the source of truth for the
    values. This class is a forward-looking check that the values also
    survive the round-trip through the Perfetto trace processor into
    the ``counter_track`` SQL table.

    As of Perfetto 0.56.0 (pinned in ``pyproject.toml:49``), the
    ``counter_track`` SQL table does not expose ``y_axis_share_key`` as
    a column. Both tests are therefore marked ``xfail`` unconditionally
    with ``strict=False``: they will start passing automatically when
    a future Perfetto version surfaces the column, and ``strict=False``
    prevents an XPASS-and-fail flip from happening at that point.
    """

    @pytest.mark.xfail(
        reason="counter_track.y_axis_share_key not exposed in Perfetto 0.56.0",
        strict=False,
    )
    def test_y_axis_share_key_shared_across_generations(
        self,
        trace_processor: TraceProcessor,
    ) -> None:
        """``G0 collected`` / ``G1 collected`` / ``G2 collected`` all
        carry the same ``y_axis_share_key`` value, and that value
        matches the metric suffix verbatim. Same for ``candidates`` and
        ``duration``. Verified via ``counter_track.y_axis_share_key``.
        """
        rows = list(
            trace_processor.query(
                "SELECT name, y_axis_share_key FROM counter_track "
                "WHERE name LIKE 'G_ %' AND name != 'heap_size' "
                "ORDER BY name",
            )
        )
        assert rows, "expected at least one G{N} <metric> track"
        by_suffix: dict[str, set[str]] = {}
        for r in rows:
            suffix = r.name.split(" ", 1)[1]
            by_suffix.setdefault(suffix, set()).add(r.y_axis_share_key)
        for suffix, keys in by_suffix.items():
            assert keys == {suffix}, (
                f"expected y_axis_share_key for metric {suffix!r} to be exactly the metric name; got {keys}"
            )

    @pytest.mark.xfail(
        reason="counter_track.y_axis_share_key not exposed in Perfetto 0.56.0",
        strict=False,
    )
    def test_heap_size_y_axis_share_key_is_null(
        self,
        trace_processor: TraceProcessor,
    ) -> None:
        """The top-level ``heap_size`` track has no ``y_axis_share_key``:
        the SQL value is NULL or empty string, depending on how the
        trace processor surfaces an absent optional string field.
        """
        rows = list(
            trace_processor.query(
                "SELECT name, y_axis_share_key FROM counter_track WHERE name = 'heap_size'",
            )
        )
        assert len(rows) == 1, f"expected exactly one heap_size row, got {len(rows)}"
        r = rows[0]
        assert r.y_axis_share_key == "", f"heap_size should have no y_axis_share_key, got {r.y_axis_share_key!r}"


class TestTrackDescriptors:
    """The Perfetto exporter emits a process track descriptor with the
    expected ``Process <pid>`` name, and no thread descriptor at all.
    (Chrome JSON does not produce a separate process track; the test is
    therefore Perfetto-only.)"""

    def test_process_track_present(self, trace_processor: TraceProcessor) -> None:
        rows = sorted(r.name for r in trace_processor.query("SELECT name FROM track WHERE name LIKE 'Process %'"))
        assert rows == sorted([f"Process {DEFAULT_PID}", f"Process {_SECOND_PID}"]), (
            f"expected process tracks for both PIDs, got {rows}"
        )

    def test_gcmon_writes_no_thread_row_of_its_own(
        self,
        trace_processor: TraceProcessor,
    ) -> None:
        """Every row an interpreter owns is a custom track (ADR-0027), so
        what is left in ``thread`` is the one nameless row the trace
        processor builds per process out of the ``ProcessDescriptor``,
        carrying the row's ``pid`` as its ``tid``. It is what
        ``is_main_thread`` marks, and gcmon cannot drop it while it writes
        processes.
        """
        rows = list(
            trace_processor.query(
                "SELECT p.name AS pname, COALESCE(th.name, '') AS tname, th.tid AS tid, "
                "th.is_main_thread AS ismain, (tt.id IS NULL) AS untracked "
                "FROM thread th JOIN process p ON th.upid = p.upid "
                "LEFT JOIN thread_track tt ON tt.utid = th.utid "
                "WHERE p.name LIKE 'Process %' ORDER BY p.name, th.tid"
            )
        )

        assert [(r.pname, r.tname, r.tid, r.ismain, r.untracked) for r in rows] == [
            (f"Process {DEFAULT_PID}", "", 1, 1, 1),
            (f"Process {_SECOND_PID}", "", 2, 1, 1),
        ], f"unexpected thread rows: {[dict(r.__dict__) for r in rows]}"

    def test_no_slice_is_drawn_on_a_thread_track(
        self,
        trace_processor: TraceProcessor,
    ) -> None:
        """The reading that made the old shape wrong: a pause reaches its
        process through ``process_track`` now, and nothing gcmon writes goes
        anywhere near ``thread_track``."""
        rows = list(
            trace_processor.query("SELECT COUNT(*) AS cnt FROM slice s JOIN thread_track tt ON s.track_id = tt.id")
        )
        assert [r.cnt for r in rows] == [0], f"expected no thread-attached slices, got {[r.cnt for r in rows]}"


class TestDiagnosticTrackSchema:
    """Diagnostic: dump the track table to understand what columns are
    populated. Run with ``pytest -m integration -k TestDiagnosticTrackSchema -s``
    to see the output."""

    def test_dump_track_schema(self, trace_processor: TraceProcessor) -> None:
        rows = list(trace_processor.query("PRAGMA table_info(track)"))
        for r in rows:
            print(f"COLUMN name={r.name!r} type={r.type!r} notnull={r.notnull} pk={r.pk}")

    def test_dump_track_table(self, trace_processor: TraceProcessor) -> None:
        rows = list(trace_processor.query("SELECT id, name, type, parent_id FROM track ORDER BY id"))
        for r in rows:
            print(f"TRACK id={r.id} name={r.name!r} type={r.type!r} parent_id={r.parent_id}")

    def test_dump_process_table(self, trace_processor: TraceProcessor) -> None:
        rows = list(trace_processor.query("SELECT * FROM process"))
        for r in rows:
            print(f"PROCESS {dict(r.__dict__)}")


class TestInstantEvents:
    """The instant event emitted at monitor start is visible to the trace
    processor as a dur=0 slice."""

    def test_instant_event_present(
        self,
        trace_processor: TraceProcessor,
    ) -> None:
        rows = list(
            trace_processor.query(
                f"SELECT s.name FROM slice s {_process_filter_instant(DEFAULT_PID)} AND s.name = '{_INSTANT_NAME}'"
            )
        )
        assert len(rows) == 1, f"expected exactly one '{_INSTANT_NAME}' dur=0 slice for DEFAULT_PID, got {rows}"


class TestCmdlineEncoding:
    """When ``cmdline_provider`` returns a non-``None`` list, the joined
    cmdline string is exposed by the trace processor as the process track's
    ``description`` arg. The Perfetto trace processor does not surface the
    per-argv ``ProcessDescriptor.CMDLINE`` repeated fields in its SQL
    tables, so the description is the only SQL-visible check."""

    def _description(self, trace_processor: TraceProcessor, name: str) -> str | None:
        """The description on the process track called *name*.

        Scoped on the name rather than the pid, which two processes share
        where one was handed on. ``TestReusedPidDrawsTwoOfEveryRow``
        covers that case; this one asks about a single process.
        """
        rows = list(
            trace_processor.query(
                f"SELECT a.string_value FROM args a "
                f"JOIN process_track pt ON a.arg_set_id = pt.source_arg_set_id "
                f"JOIN process p ON p.upid = pt.upid "
                f"WHERE p.name = '{name}' AND a.key = 'description'"
            )
        )
        return rows[0].string_value if rows else None

    def test_cmdline_description_appears_for_known_pid(
        self,
        trace_processor_with_cmdline: TraceProcessor,
    ) -> None:
        assert self._description(trace_processor_with_cmdline, f"Process {DEFAULT_PID}") == _FAKE_CMDLINE_JOINED
        assert self._description(trace_processor_with_cmdline, f"Process {_SECOND_PID}") == _FAKE_CMDLINE_JOINED

    def test_cmdline_absent_for_pid_outside_provider(
        self,
        trace_processor_with_cmdline: TraceProcessor,
    ) -> None:
        assert self._description(trace_processor_with_cmdline, "Process 1") is None

    def test_cmdline_none_for_unknown_pid(
        self,
        trace_processor: TraceProcessor,
    ) -> None:
        assert self._description(trace_processor, f"Process {DEFAULT_PID}") is None
        assert self._description(trace_processor, f"Process {_SECOND_PID}") is None


class TestProcessRowLifetimeSlice:
    """Every process's own row carries one ``Lifetime`` slice spanning the
    interval gcmon observed that process.

    The ``Processes`` track shortens a span that crosses a sibling's to keep
    its slice stack laminar. A process's own row holds one slice and the
    workload's marks, which nest without closing anything, so nothing on it
    can cross and nothing is clipped. The two rows therefore disagree for a
    clipped process, and this one is the row telling the truth (ADR-0011).
    """

    def _lifetimes(self, tp: TraceProcessor) -> dict[str, tuple[int, int]]:
        """``{process name: (ts, dur)}`` for every ``Lifetime`` slice, read
        through ``process_track`` so a slice on any other row is invisible
        here."""
        rows = list(
            tp.query(
                f"SELECT p.name AS name, s.ts AS ts, s.dur AS dur FROM slice s "
                f"JOIN process_track pt ON s.track_id = pt.id "
                f"JOIN process p ON pt.upid = p.upid "
                f"WHERE s.name = '{_PROCESS_ROW_SLICE_NAME}' "
                f"ORDER BY p.name"
            )
        )
        return {r.name: (r.ts, r.dur) for r in rows}

    def test_one_slice_per_process_row(self, trace_processor: TraceProcessor) -> None:
        """One pair per process, on the process's own track, drawing the
        interval gcmon observed rather than the one the sweep left."""
        default_start = _TS_START - 1_000_000
        assert self._lifetimes(trace_processor) == {
            f"Process {DEFAULT_PID}": (default_start, 10_000_000),
            f"Process {_SECOND_PID}": (_TS_START - 2_000_000, 7_000_000),
        }

    def test_clipped_process_draws_longer_on_its_own_row(
        self,
        trace_processor: TraceProcessor,
    ) -> None:
        """The two-row divergence, asserted on both rows at once.

        ``_SECOND_PID`` crosses ``DEFAULT_PID``, so the sweep pulls its
        ``Processes`` span back to 1ms. Its own row keeps the 7ms gcmon
        measured. A test reading only one of the two rows would pass on an
        implementation that clipped both.
        """
        shared = list(
            trace_processor.query(
                f"SELECT s.dur AS dur FROM slice s "
                f"JOIN track t ON s.track_id = t.id "
                f"WHERE t.name = '{_PROCESS_LIFETIME_TRACK_NAME}' "
                f"AND s.name = 'Process {_SECOND_PID}'"
            )
        )
        assert [r.dur for r in shared] == [999_999], "expected the shared row to draw the clipped span"
        assert self._lifetimes(trace_processor)[f"Process {_SECOND_PID}"][1] == 7_000_000

    def test_carries_no_real_ts_annotations(self, trace_processor: TraceProcessor) -> None:
        """``ts`` and ``dur`` *are* the observed pair here, so copying it into
        annotations would state one fact twice."""
        rows = list(
            trace_processor.query(
                f"SELECT a.flat_key AS flat_key FROM args a "
                f"JOIN slice s ON s.arg_set_id = a.arg_set_id "
                f"WHERE s.name = '{_PROCESS_ROW_SLICE_NAME}' "
                f"AND a.flat_key IN ('{_ARG_PREFIX}.real_start_ts', '{_ARG_PREFIX}.real_end_ts')"
            )
        )
        assert rows == []

    def test_carries_cmdline_pid_and_epoch(
        self,
        trace_processor_with_cmdline: TraceProcessor,
    ) -> None:
        """Click the bar and the Args panel says what the process was running,
        which pid the operating system gave it and which epoch of that pid it
        is.

        ``pid`` is on the bar because it is nowhere else a reader can reach:
        ``process.pid`` holds the row's, one gcmon hands out per process so
        that a pid handed on draws a row per process (ADR-0011)."""
        rows = list(
            trace_processor_with_cmdline.query(
                f"SELECT p.name AS name, a.flat_key AS flat_key, "
                f"a.string_value AS string_value, a.int_value AS int_value "
                f"FROM args a "
                f"JOIN slice s ON s.arg_set_id = a.arg_set_id "
                f"JOIN process_track pt ON s.track_id = pt.id "
                f"JOIN process p ON pt.upid = p.upid "
                f"WHERE s.name = '{_PROCESS_ROW_SLICE_NAME}' "
                f"AND a.flat_key LIKE '{_ARG_PREFIX}.%' "
                f"ORDER BY p.name, a.flat_key"
            )
        )
        assert {(r.name, r.flat_key) for r in rows} == {
            (f"Process {pid}", f"{_ARG_PREFIX}.{key}")
            for pid in (DEFAULT_PID, _SECOND_PID)
            for key in (
                "cmdline",
                "pid",
                "pid_epoch",
                "interpreters",
                "sampled_count",
                "lost_count",
                "lost_pause",
                "lost_pause_ns",
            )
        }
        # Read each annotation out of the column its type puts it in, so a
        # `pid_epoch` written as a string reads back as a missing int.
        assert {r.name: r.string_value for r in rows if r.flat_key.endswith("cmdline")} == {
            f"Process {DEFAULT_PID}": _FAKE_CMDLINE_JOINED,
            f"Process {_SECOND_PID}": _FAKE_CMDLINE_JOINED,
        }
        assert {r.name: r.int_value for r in rows if r.flat_key.endswith("pid_epoch")} == {
            f"Process {DEFAULT_PID}": 1,
            f"Process {_SECOND_PID}": 1,
        }
        assert {r.name: r.int_value for r in rows if r.flat_key.endswith(".pid")} == {
            f"Process {DEFAULT_PID}": DEFAULT_PID,
            f"Process {_SECOND_PID}": _SECOND_PID,
        }

    def test_carries_the_interpreter_count(
        self,
        trace_processor: TraceProcessor,
    ) -> None:
        """How many interpreters the process ran, which the row's name cannot
        say. ``DEFAULT_PID`` collected on three, ``_SECOND_PID`` on one."""
        rows = list(
            trace_processor.query(
                f"SELECT p.name AS name, a.int_value AS int_value "
                f"FROM args a "
                f"JOIN slice s ON s.arg_set_id = a.arg_set_id "
                f"JOIN process_track pt ON s.track_id = pt.id "
                f"JOIN process p ON pt.upid = p.upid "
                f"WHERE s.name = '{_PROCESS_ROW_SLICE_NAME}' "
                f"AND a.flat_key = '{_ARG_PREFIX}.interpreters'"
            )
        )
        assert {r.name: r.int_value for r in rows} == {
            f"Process {DEFAULT_PID}": 3,
            f"Process {_SECOND_PID}": 1,
        }

    def test_carries_what_gcmon_read_and_what_it_missed(
        self,
        trace_processor: TraceProcessor,
    ) -> None:
        """The completeness of the capture, per process. This run lost
        nothing, so each bar has to say how much it read rather than the
        zero a count summed off the ``GC Loss`` rows would give."""
        rows = list(
            trace_processor.query(
                f"SELECT p.name AS name, a.flat_key AS flat_key, "
                f"a.int_value AS int_value, a.string_value AS string_value "
                f"FROM args a "
                f"JOIN slice s ON s.arg_set_id = a.arg_set_id "
                f"JOIN process_track pt ON s.track_id = pt.id "
                f"JOIN process p ON pt.upid = p.upid "
                f"WHERE s.name = '{_PROCESS_ROW_SLICE_NAME}' "
                f"AND a.flat_key LIKE '{_ARG_PREFIX}.%'"
            )
        )
        counts = {(r.name, r.flat_key.rsplit(".", 1)[1]): r.int_value for r in rows}
        assert counts[(f"Process {DEFAULT_PID}", "sampled_count")] == 3
        assert counts[(f"Process {_SECOND_PID}", "sampled_count")] == 1
        assert counts[(f"Process {DEFAULT_PID}", "lost_count")] == 0
        assert counts[(f"Process {DEFAULT_PID}", "lost_pause_ns")] == 0
        text = {(r.name, r.flat_key.rsplit(".", 1)[1]): r.string_value for r in rows}
        assert text[(f"Process {DEFAULT_PID}", "lost_pause")] == "0ns"

    def test_two_processes_on_one_pid_count_their_own_capture(
        self,
        reused_pid_trace_processor: TraceProcessor,
    ) -> None:
        """The totals follow the process, not the pid. Each of the two ran
        one collection and went blind over its own interval."""
        rows = list(
            reused_pid_trace_processor.query(
                f"SELECT p.name AS name, a.flat_key AS flat_key, "
                f"a.int_value AS int_value, a.string_value AS string_value "
                f"FROM args a "
                f"JOIN slice s ON s.arg_set_id = a.arg_set_id "
                f"JOIN process_track pt ON s.track_id = pt.id "
                f"JOIN process p ON pt.upid = p.upid "
                f"WHERE s.name = '{_PROCESS_ROW_SLICE_NAME}' "
                f"AND a.flat_key LIKE '{_ARG_PREFIX}.%'"
            )
        )
        counts = {(r.name, r.flat_key.rsplit(".", 1)[1]): r.int_value for r in rows}
        assert counts[(_REUSE_FIRST_NAME, "sampled_count")] == 1
        assert counts[(_REUSE_SECOND_NAME, "sampled_count")] == 1
        assert counts[(_REUSE_FIRST_NAME, "lost_count")] == _REUSE_FIRST_COLLECTED
        assert counts[(_REUSE_SECOND_NAME, "lost_count")] == _REUSE_SECOND_COLLECTED
        assert counts[(_REUSE_FIRST_NAME, "lost_pause_ns")] == _REUSE_LOST_PAUSE_NS
        text = {(r.name, r.flat_key.rsplit(".", 1)[1]): r.string_value for r in rows}
        assert text[(_REUSE_FIRST_NAME, "lost_pause")] == duration_text(_REUSE_LOST_PAUSE_NS)

    def test_no_cmdline_annotation_without_a_cmdline(
        self,
        trace_processor: TraceProcessor,
    ) -> None:
        """A process gcmon read no command line for carries no ``cmdline``
        annotation rather than an empty one."""
        rows = list(
            trace_processor.query(
                f"SELECT a.flat_key AS flat_key FROM args a "
                f"JOIN slice s ON s.arg_set_id = a.arg_set_id "
                f"WHERE s.name = '{_PROCESS_ROW_SLICE_NAME}' "
                f"AND a.flat_key = '{_ARG_PREFIX}.cmdline'"
            )
        )
        assert rows == []

    def test_a_mark_nests_inside_the_bar(
        self,
        nested_mark_trace_processor: TraceProcessor,
    ) -> None:
        """The bar opens before the workload's mark and closes after it, so
        the trace processor reads the bar at depth 0 and the mark at 1."""
        rows = list(
            nested_mark_trace_processor.query(
                f"SELECT s.name AS name, s.depth AS depth FROM slice s "
                f"JOIN process_track pt ON s.track_id = pt.id "
                f"JOIN process p ON pt.upid = p.upid "
                f"WHERE p.name = 'Process {DEFAULT_PID}' "
                f"AND s.name IN ('{_PROCESS_ROW_SLICE_NAME}', '{_INSTANT_NAME}') "
                f"ORDER BY s.depth"
            )
        )
        assert [(r.name, r.depth) for r in rows] == [
            (_PROCESS_ROW_SLICE_NAME, 0),
            (_INSTANT_NAME, 1),
        ]

    def test_a_mark_at_the_bars_own_start_sits_beside_it(
        self,
        trace_processor: TraceProcessor,
    ) -> None:
        """A mark that *is* the process's first observation shares the bar's
        timestamp, and lands beside the bar rather than in it.

        The bar goes out at close, last in the stream, and the trace processor
        breaks a timestamp tie by position in the sequence, so a BEGIN written
        after the mark opens after it. Accepted (ADR-0010): the alternative is
        opening the bar a tick early, at a start gcmon never observed.
        """
        rows = list(
            trace_processor.query(
                f"SELECT s.name AS name, s.ts AS ts, s.depth AS depth FROM slice s "
                f"{_process_row_filter(DEFAULT_PID)} ORDER BY s.ts, s.depth"
            )
        )
        assert [(r.name, r.depth) for r in rows] == [
            (_INSTANT_NAME, 0),
            (_PROCESS_ROW_SLICE_NAME, 0),
        ]
        assert len({r.ts for r in rows}) == 1, "the fixture's mark is the bar's own start"

    def test_drawn_without_a_user_instant(
        self,
        trace_processor_no_instant: TraceProcessor,
    ) -> None:
        """The regression case ADR-0010 exists for: the caller sent no
        ``Instant`` for either pid, so the bar is the only thing on the row.
        Perfetto hides a row holding no events, and the ``description`` with
        it.
        """
        assert sorted(self._lifetimes(trace_processor_no_instant)) == [
            f"Process {DEFAULT_PID}",
            f"Process {_SECOND_PID}",
        ]
        descriptions = list(
            trace_processor_no_instant.query(
                "SELECT p.name AS name, a.string_value AS description FROM args a "
                "JOIN process_track pt ON a.arg_set_id = pt.source_arg_set_id "
                "JOIN process p ON p.upid = pt.upid "
                "WHERE a.key = 'description'"
            )
        )
        assert {r.name: r.description for r in descriptions} == {
            f"Process {DEFAULT_PID}": _FAKE_CMDLINE_JOINED,
            f"Process {_SECOND_PID}": _FAKE_CMDLINE_JOINED,
        }

    def test_allocates_no_track(self, trace_processor: TraceProcessor) -> None:
        """The bar reuses the process track uuid. A uuid of its own would draw
        a second row, with no descriptor behind it to name the process."""
        rows = list(
            trace_processor.query(
                f"SELECT s.name AS name, s.track_id AS track_id FROM slice s "
                f"JOIN process_track pt ON s.track_id = pt.id "
                f"JOIN process p ON pt.upid = p.upid "
                f"WHERE p.name = 'Process {DEFAULT_PID}' "
                f"AND s.name IN ('{_PROCESS_ROW_SLICE_NAME}', '{_INSTANT_NAME}')"
            )
        )
        assert len({r.track_id for r in rows}) == 1
        assert sorted(r.name for r in rows) == [_INSTANT_NAME, _PROCESS_ROW_SLICE_NAME]

    def test_single_observation_draws_a_zero_length_bar(
        self,
        zero_duration_trace_processor: TraceProcessor,
    ) -> None:
        """``_THIRD_PID`` was observed once, so its bar reads ``dur = 0``
        rather than ``-1``, which is what BEGIN-before-END buys.

        ``DEFAULT_PID`` is the other half of the point: the sweep clips it to
        nothing on the shared row, and its own row keeps the 500ms it was
        observed for.
        """
        lifetimes = self._lifetimes(zero_duration_trace_processor)
        assert lifetimes[f"Process {_THIRD_PID}"] == (_ZERO_INSTANT_TS, 0)
        assert lifetimes[f"Process {DEFAULT_PID}"] == (
            _ZERO_CLIPPED_START,
            _ZERO_CLIPPED_STOP - _ZERO_CLIPPED_START,
        )


class TestProcessesTrack:
    """The Perfetto encoder emits a single shared top-level track named
    ``Processes`` that holds one ``TYPE_SLICE_BEGIN`` /
    ``TYPE_SLICE_END`` pair per pid, spanning the first-to-last
    non-counter non-meta event timestamps for that pid.
    """

    def test_track_present(
        self,
        trace_processor: TraceProcessor,
    ) -> None:
        """The ``Processes`` track is present exactly once."""
        rows = list(trace_processor.query(f"SELECT name FROM track WHERE name = '{_PROCESS_LIFETIME_TRACK_NAME}'"))
        assert len(rows) == 1, (
            f"expected exactly one {_PROCESS_LIFETIME_TRACK_NAME!r} track, got {[r.name for r in rows]}"
        )

    def test_slice_per_pid(
        self,
        trace_processor: TraceProcessor,
    ) -> None:
        """There is exactly one BEGIN+END pair per pid on the
        ``Processes`` track, at the right timestamps.

        Asserting the timestamps and not just the row count matters:
        a crossing pair leaves the row count intact while silently
        handing one pid a duration that is not its own.

        This fixture's two spans cross. ``_SECOND_PID`` is observed from
        ``_TS_START - 2ms`` to ``_TS_START + 5ms``; ``DEFAULT_PID`` starts
        1ms later and runs 4ms longer. So ``_SECOND_PID``'s end is
        clipped back to just before ``DEFAULT_PID`` begins, collapsing a
        7ms span to 1ms, and ``DEFAULT_PID`` keeps its full 10ms. Before
        the clip, the trace processor reported ``DEFAULT_PID`` as
        6_000_000ns long against a real span of 10_000_000ns.
        """
        rows = list(
            trace_processor.query(
                f"SELECT s.name, s.ts, s.dur FROM slice s "
                f"JOIN track t ON s.track_id = t.id "
                f"WHERE t.name = '{_PROCESS_LIFETIME_TRACK_NAME}' "
                f"ORDER BY s.name"
            )
        )
        assert [r.name for r in rows] == [
            f"Process {DEFAULT_PID}",
            f"Process {_SECOND_PID}",
        ], f"expected exactly one dur-bearing Process <pid> slice per pid, got {[(r.name, r.dur) for r in rows]}"
        for r in rows:
            assert r.dur > 0, f"slice {r.name!r} has dur={r.dur}, expected > 0"
        spans = {r.name: (r.ts, r.ts + r.dur) for r in rows}
        default_start = _TS_START - 1_000_000
        assert spans == {
            f"Process {DEFAULT_PID}": (default_start, _TS_START + 9_000_000),
            f"Process {_SECOND_PID}": (_TS_START - 2_000_000, default_start - 1),
        }

    def test_every_slice_records_its_real_span(
        self,
        trace_processor: TraceProcessor,
    ) -> None:
        """Both slices carry the span gcmon observed, whether or not the
        drawing survived it. ``_SECOND_PID`` is the one clipped in this
        fixture: its slice draws to ``default_start - 1`` but records the
        real end 5ms later. ``DEFAULT_PID`` is untouched and records the
        same span it draws -- read the same way, no branch needed."""
        rows = list(
            trace_processor.query(
                f"SELECT s.name AS name, a.flat_key AS flat_key, a.int_value AS int_value "
                f"FROM args a "
                f"JOIN slice s ON s.arg_set_id = a.arg_set_id "
                f"JOIN track t ON s.track_id = t.id "
                f"WHERE t.name = '{_PROCESS_LIFETIME_TRACK_NAME}' "
                f"AND a.flat_key IN ('debug.real_start_ts', 'debug.real_end_ts') "
                f"ORDER BY s.name, a.flat_key"
            )
        )
        assert {(r.name, r.flat_key): r.int_value for r in rows} == {
            (f"Process {DEFAULT_PID}", "debug.real_start_ts"): _TS_START - 1_000_000,
            (f"Process {DEFAULT_PID}", "debug.real_end_ts"): _TS_START + 9_000_000,
            (f"Process {_SECOND_PID}", "debug.real_start_ts"): _TS_START - 2_000_000,
            (f"Process {_SECOND_PID}", "debug.real_end_ts"): _TS_START + 5_000_000,
        }

    def test_no_misplaced_end_events(
        self,
        trace_processor: TraceProcessor,
    ) -> None:
        """The trace processor discards nothing.

        ``misplaced_end_event`` counts every ``TYPE_SLICE_END`` that had
        no slice to close. It is the trace processor reporting data loss
        directly, rather than an inference from the slice table.
        """
        assert _misplaced_end_events(trace_processor) == 0

    def test_slice_name_format(
        self,
        trace_processor: TraceProcessor,
    ) -> None:
        """Every slice name on the ``Processes`` track matches the
        ``Process <pid>`` pattern, with the ``#N`` a successor on a
        reused pid carries."""
        rows = list(
            trace_processor.query(
                f"SELECT s.name FROM slice s "
                f"JOIN track t ON s.track_id = t.id "
                f"WHERE t.name = '{_PROCESS_LIFETIME_TRACK_NAME}'"
            )
        )
        pat = re.compile(r"^Process \d+(#\d+)?$")
        for r in rows:
            assert pat.match(r.name), (
                f"slice name {r.name!r} on the {_PROCESS_LIFETIME_TRACK_NAME!r} track "
                f"must match 'Process <pid>' or 'Process <pid>#N'"
            )

    def test_begin_end_match_first_last_event(
        self,
        trace_processor: TraceProcessor,
    ) -> None:
        """For each pid, the slice BEGIN is at the first non-meta event
        ts, and the slice END (BEGIN + dur) is at the last Begin/End/
        Instant event ts (counter events excluded)."""
        # The fixture adds an instant event at _TS_START - 1_000_000,
        # then a GC item at _TS_START / _TS_START + dur, then a second
        # item etc. The first non-meta event for each pid is the
        # instant event. The last non-counter non-meta event for each
        # pid is the end of the last GC item's pause.
        #
        # We compare against SQL: take the min(ts) of every Begin/End/
        # Instant event for the pid, all of them reached by one join
        # through process_track (ADR-0027), then verify the slice matches.
        for pid in (DEFAULT_PID, _SECOND_PID):
            candidates = [
                r.ts for r in trace_processor.query(f"SELECT MIN(s.ts) AS ts FROM slice s {_process_filter(pid)}")
            ]
            assert candidates, f"no first event found for pid {pid}"
            expected_first = min(candidates)

            slice_rows = list(
                trace_processor.query(
                    f"SELECT s.ts, s.dur FROM slice s "
                    f"JOIN track t ON s.track_id = t.id "
                    f"WHERE t.name = '{_PROCESS_LIFETIME_TRACK_NAME}' "
                    f"AND s.name = 'Process {pid}' "
                    f"AND s.dur > 0"
                )
            )
            assert len(slice_rows) == 1, f"expected exactly one duration-bearing Process {pid} slice, got {slice_rows}"
            assert slice_rows[0].ts == expected_first, (
                f"slice begin ts mismatch for pid {pid}: got {slice_rows[0].ts}, expected {expected_first}"
            )

    def test_cmdline_arg_present(
        self,
        trace_processor_with_cmdline: TraceProcessor,
    ) -> None:
        """Each ``Process <pid>`` slice on the ``Processes`` track
        carries a ``cmdline`` debug annotation whose value is the
        argv joined with single spaces."""
        for pid in (DEFAULT_PID, _SECOND_PID):
            rows = list(
                trace_processor_with_cmdline.query(
                    f"SELECT a.string_value AS string_value "
                    f"FROM args a "
                    f"WHERE a.flat_key = 'debug.cmdline' "
                    f"AND a.arg_set_id IN ("
                    f"  SELECT s.arg_set_id FROM slice s "
                    f"  JOIN track t ON s.track_id = t.id "
                    f"  WHERE t.name = '{_PROCESS_LIFETIME_TRACK_NAME}' "
                    f"  AND s.name = 'Process {pid}'"
                    f")"
                )
            )
            assert len(rows) == 1, f"expected exactly one debug.cmdline arg for pid {pid}, got {rows}"
            assert rows[0].string_value == _FAKE_CMDLINE_JOINED, (
                f"debug.cmdline for pid {pid}: expected {_FAKE_CMDLINE_JOINED!r}, got {rows[0].string_value!r}"
            )


class TestCrossingProcessSpans:
    """Two pids whose observed spans cross rather than nest.

    Slices on one Perfetto track are a stack, so a crossing pair cannot
    be expressed: the trace processor closes both slices at the earlier
    END and discards the later one. Before the encoder clipped these
    spans, this trace produced ``misplaced_end_event: 1`` and handed
    ``_SECOND_PID`` a duration ending at ``DEFAULT_PID``'s last event.
    """

    def test_no_misplaced_end_events(self, crossing_trace_processor: TraceProcessor) -> None:
        assert _misplaced_end_events(crossing_trace_processor) == 0

    def test_earlier_span_is_clipped_and_later_span_is_intact(
        self,
        crossing_trace_processor: TraceProcessor,
    ) -> None:
        rows = list(
            crossing_trace_processor.query(
                f"SELECT s.name, s.ts, s.dur FROM slice s "
                f"JOIN track t ON s.track_id = t.id "
                f"WHERE t.name = '{_PROCESS_LIFETIME_TRACK_NAME}' "
                f"ORDER BY s.ts"
            )
        )
        spans = {r.name: (r.ts, r.ts + r.dur) for r in rows}
        assert spans == {
            # Clipped to one nanosecond before the later pid begins.
            f"Process {DEFAULT_PID}": (_CROSS_A_START, _CROSS_B_START - 1),
            # Untouched: this is the span that used to be truncated.
            f"Process {_SECOND_PID}": (_CROSS_B_START, _CROSS_B_STOP),
        }

    def test_every_slice_records_its_real_span(
        self,
        crossing_trace_processor: TraceProcessor,
    ) -> None:
        """Both slices carry ``real_start_ts`` / ``real_end_ts``, so the
        drawn duration can always be told apart from the observed one --
        including for the clipped slice, whose drawn end is 200ms short
        of the truth."""
        rows = list(
            crossing_trace_processor.query(
                f"SELECT s.name AS name, a.flat_key AS flat_key, a.int_value AS int_value "
                f"FROM args a "
                f"JOIN slice s ON s.arg_set_id = a.arg_set_id "
                f"JOIN track t ON s.track_id = t.id "
                f"WHERE t.name = '{_PROCESS_LIFETIME_TRACK_NAME}' "
                f"AND a.flat_key IN ('debug.real_start_ts', 'debug.real_end_ts') "
                f"ORDER BY s.name, a.flat_key"
            )
        )
        assert {(r.name, r.flat_key): r.int_value for r in rows} == {
            (f"Process {DEFAULT_PID}", "debug.real_start_ts"): _CROSS_A_START,
            (f"Process {DEFAULT_PID}", "debug.real_end_ts"): _CROSS_A_STOP,
            (f"Process {_SECOND_PID}", "debug.real_start_ts"): _CROSS_B_START,
            (f"Process {_SECOND_PID}", "debug.real_end_ts"): _CROSS_B_STOP,
        }

    def test_every_slice_says_whether_the_sweep_moved_it(
        self,
        crossing_trace_processor: TraceProcessor,
    ) -> None:
        """``clipped`` is the verdict the sweep reached, on the one row the
        sweep decides. It goes out either way, so a consumer reads the value
        rather than the presence of the annotation.

        ``value_type`` pins it as a bool: written as an int it would read
        back as ``1`` and ``0`` in both the UI and SQL.
        """
        rows = list(
            crossing_trace_processor.query(
                f"SELECT s.name AS name, a.value_type AS value_type, a.int_value AS int_value "
                f"FROM args a "
                f"JOIN slice s ON s.arg_set_id = a.arg_set_id "
                f"JOIN track t ON s.track_id = t.id "
                f"WHERE t.name = '{_PROCESS_LIFETIME_TRACK_NAME}' "
                f"AND a.flat_key = '{_ARG_PREFIX}.clipped'"
            )
        )
        assert {r.name: r.int_value for r in rows} == {
            # Pulled back 200ms short of its last event.
            f"Process {DEFAULT_PID}": 1,
            f"Process {_SECOND_PID}": 0,
        }
        assert {r.value_type for r in rows} == {"bool"}


class TestZeroDurationProcessSpans:
    """A ``Processes`` slice that ends up zero-length is still drawn.

    Two ways to get one: a pid observed at a single instant, and a pid
    clipped down to nothing by a pid starting one nanosecond later. Both
    are in this fixture. Dropping such a slice would leave the pid off
    the track with nothing to indicate it was ever monitored, and a
    reader has no way to notice an absence.
    """

    def test_no_misplaced_end_events(self, zero_duration_trace_processor: TraceProcessor) -> None:
        """A zero-duration slice is a BEGIN and an END at the same ts.
        The trace processor must pair them, not orphan the END."""
        assert _misplaced_end_events(zero_duration_trace_processor) == 0

    def test_every_pid_keeps_a_slice(
        self,
        zero_duration_trace_processor: TraceProcessor,
    ) -> None:
        """All three pids appear, two of them with ``dur = 0``."""
        rows = list(
            zero_duration_trace_processor.query(
                f"SELECT s.name AS name, s.ts AS ts, s.dur AS dur FROM slice s "
                f"JOIN track t ON s.track_id = t.id "
                f"WHERE t.name = '{_PROCESS_LIFETIME_TRACK_NAME}' "
                f"ORDER BY s.ts"
            )
        )
        assert {r.name: (r.ts, r.dur) for r in rows} == {
            f"Process {_THIRD_PID}": (_ZERO_INSTANT_TS, 0),
            f"Process {DEFAULT_PID}": (_ZERO_CLIPPED_START, 0),
            f"Process {_SECOND_PID}": (_ZERO_CROSSER_START, _ZERO_CROSSER_STOP - _ZERO_CROSSER_START),
        }

    def test_zero_duration_slices_still_record_their_real_span(
        self,
        zero_duration_trace_processor: TraceProcessor,
    ) -> None:
        """This is the whole point of drawing them: ``DEFAULT_PID`` draws
        as ``dur = 0`` but was observed for 500ms, and that is readable
        from the trace."""
        rows = list(
            zero_duration_trace_processor.query(
                f"SELECT s.name AS name, a.flat_key AS flat_key, a.int_value AS int_value "
                f"FROM args a "
                f"JOIN slice s ON s.arg_set_id = a.arg_set_id "
                f"JOIN track t ON s.track_id = t.id "
                f"WHERE t.name = '{_PROCESS_LIFETIME_TRACK_NAME}' "
                f"AND a.flat_key IN ('debug.real_start_ts', 'debug.real_end_ts') "
                f"ORDER BY s.name, a.flat_key"
            )
        )
        assert {(r.name, r.flat_key): r.int_value for r in rows} == {
            (f"Process {DEFAULT_PID}", "debug.real_start_ts"): _ZERO_CLIPPED_START,
            (f"Process {DEFAULT_PID}", "debug.real_end_ts"): _ZERO_CLIPPED_STOP,
            (f"Process {_SECOND_PID}", "debug.real_start_ts"): _ZERO_CROSSER_START,
            (f"Process {_SECOND_PID}", "debug.real_end_ts"): _ZERO_CROSSER_STOP,
            (f"Process {_THIRD_PID}", "debug.real_start_ts"): _ZERO_INSTANT_TS,
            (f"Process {_THIRD_PID}", "debug.real_end_ts"): _ZERO_INSTANT_TS,
        }


class TestMonitorReportedLiveness:
    """``Processes`` slices span what gcmon *observed*, not what it saw
    collect, so the monitor loop's per-tick liveness reports reach the
    track alongside the events. See ADR-0011.
    """

    def test_no_misplaced_end_events(self, liveness_trace_processor: TraceProcessor) -> None:
        """The two spans co-terminate on the last tick, so the later one
        nests inside the earlier and both ENDs land on one timestamp.
        The trace processor must still pair them."""
        assert _misplaced_end_events(liveness_trace_processor) == 0

    def test_liveness_only_pid_gets_exactly_one_slice(
        self,
        liveness_trace_processor: TraceProcessor,
    ) -> None:
        """``_SECOND_PID`` produced no events at all, only three liveness
        observations, and they bound one slice on the shared row."""
        rows = list(
            liveness_trace_processor.query(
                f"SELECT s.ts AS ts, s.dur AS dur FROM slice s "
                f"JOIN track t ON s.track_id = t.id "
                f"WHERE t.name = '{_PROCESS_LIFETIME_TRACK_NAME}' AND s.name = 'Process {_SECOND_PID}'"
            )
        )
        assert len(rows) == 1, f"expected exactly one slice for the liveness-only pid, got {len(rows)}"
        assert (rows[0].ts, rows[0].ts + rows[0].dur) == (_LIVE_TICKS[0], _LIVE_TICKS[-1])

    def test_a_pid_with_both_spans_their_union(
        self,
        liveness_trace_processor: TraceProcessor,
    ) -> None:
        """Liveness folds in alongside events rather than replacing them.
        ``DEFAULT_PID``'s GC event predates every observation -- a poll
        returns collections that already happened -- so the start is the
        event's and the end is the last tick's."""
        rows = list(
            liveness_trace_processor.query(
                f"SELECT a.flat_key AS flat_key, a.int_value AS int_value FROM args a "
                f"JOIN slice s ON s.arg_set_id = a.arg_set_id "
                f"JOIN track t ON s.track_id = t.id "
                f"WHERE t.name = '{_PROCESS_LIFETIME_TRACK_NAME}' AND s.name = 'Process {DEFAULT_PID}' "
                f"AND a.flat_key IN ('debug.real_start_ts', 'debug.real_end_ts')"
            )
        )
        assert {r.flat_key: r.int_value for r in rows} == {
            "debug.real_start_ts": _LIVE_GC_START,
            "debug.real_end_ts": _LIVE_TICKS[-1],
        }

    def test_the_quiet_process_gets_a_row_of_its_own(
        self,
        liveness_trace_processor: TraceProcessor,
    ) -> None:
        """``_SECOND_PID`` named no track all run, so nothing described it
        before close. It resolves to its own ``upid`` all the same, opening at
        its own first observation and carrying its own command line, and its
        row holds the one ``Lifetime`` bar and nothing else (ADR-0011)."""
        rows = list(
            liveness_trace_processor.query(
                f"SELECT p.upid AS upid, p.start_ts AS start_ts, s.name AS sname, "
                f"s.ts AS ts, s.dur AS dur FROM process p "
                f"JOIN process_track pt ON pt.upid = p.upid "
                f"JOIN slice s ON s.track_id = pt.id "
                f"WHERE p.name = 'Process {_SECOND_PID}'"
            )
        )
        assert len(rows) == 1, f"expected one slice on the quiet process's row, got {rows}"
        row = rows[0]
        assert row.start_ts == _LIVE_TICKS[0]
        assert (row.sname, row.ts, row.ts + row.dur) == (
            _PROCESS_ROW_SLICE_NAME,
            _LIVE_TICKS[0],
            _LIVE_TICKS[-1],
        )

        busy = list(
            liveness_trace_processor.query(
                f"SELECT p.upid AS upid FROM process p WHERE p.name = 'Process {DEFAULT_PID}'"
            )
        )
        assert [row.upid] != [r.upid for r in busy], "the two processes must not share a upid"

    def test_each_process_row_carries_its_own_command_line(
        self,
        liveness_trace_processor: TraceProcessor,
    ) -> None:
        """The descriptor built at close reads the same per-process command
        line every other descriptor does, rather than the busy process's."""
        rows = list(
            liveness_trace_processor.query(
                "SELECT p.name AS pname, a.string_value AS description FROM args a "
                "JOIN process_track pt ON a.arg_set_id = pt.source_arg_set_id "
                "JOIN process p ON p.upid = pt.upid "
                "WHERE a.key = 'description' ORDER BY p.name"
            )
        )
        assert {r.pname: r.description for r in rows} == {
            f"Process {DEFAULT_PID}": " ".join(_LIVE_BUSY_CMDLINE),
            f"Process {_SECOND_PID}": " ".join(_LIVE_QUIET_CMDLINE),
        }

    def test_every_polled_pid_appears_exactly_once(
        self,
        liveness_trace_processor: TraceProcessor,
    ) -> None:
        rows = list(
            liveness_trace_processor.query(
                f"SELECT s.name AS name, COUNT(*) AS n FROM slice s "
                f"JOIN track t ON s.track_id = t.id "
                f"WHERE t.name = '{_PROCESS_LIFETIME_TRACK_NAME}' GROUP BY s.name"
            )
        )
        assert {r.name: r.n for r in rows} == {
            f"Process {DEFAULT_PID}": 1,
            f"Process {_SECOND_PID}": 1,
        }


class TestARunKilledMidFlight:
    """The file a ``SIGKILL`` leaves: batches on disk and no closeout.

    A process gcmon had already let go of keeps its row, because its bar went
    out with the first batch after it retired rather than at close (ADR-0011).
    A process still running loses its, which is the part this does not reach.
    """

    def test_the_retired_process_keeps_its_row(
        self,
        killed_run_trace_processor: TraceProcessor,
    ) -> None:
        rows = list(
            killed_run_trace_processor.query(
                f"SELECT s.name AS sname, s.ts AS ts, s.dur AS dur FROM slice s {_process_row_filter(_SECOND_PID)}"
            )
        )
        assert [(r.sname, r.ts, r.ts + r.dur) for r in rows] == [(_PROCESS_ROW_SLICE_NAME, _KILL_GC_START, _KILL_TICK)]

    def test_the_retired_process_keeps_its_description(
        self,
        killed_run_trace_processor: TraceProcessor,
    ) -> None:
        """The bar keeps the row rendered, and the command line hangs off the
        row."""
        rows = list(
            killed_run_trace_processor.query(
                f"SELECT a.string_value AS description FROM args a "
                f"JOIN process_track pt ON a.arg_set_id = pt.source_arg_set_id "
                f"JOIN process p ON p.upid = pt.upid "
                f"WHERE a.key = 'description' AND p.name = 'Process {_SECOND_PID}'"
            )
        )
        assert [r.description for r in rows] == [_FAKE_CMDLINE_JOINED]

    def test_the_still_running_process_draws_nothing_on_its_row(
        self,
        killed_run_trace_processor: TraceProcessor,
    ) -> None:
        """What the kill still costs. ``DEFAULT_PID`` was alive when the trace
        stopped, so its bar never went out."""
        rows = list(
            killed_run_trace_processor.query(f"SELECT s.name AS sname FROM slice s {_process_row_filter(DEFAULT_PID)}")
        )
        assert rows == []

    def test_no_processes_track(self, killed_run_trace_processor: TraceProcessor) -> None:
        """The minimap is a whole-run artifact: every slice on it is clipped
        against every other, so none of it can go out early."""
        rows = list(
            killed_run_trace_processor.query(f"SELECT name FROM track WHERE name = '{_PROCESS_LIFETIME_TRACK_NAME}'")
        )
        assert rows == []

    def test_the_pauses_are_still_there(
        self,
        killed_run_trace_processor: TraceProcessor,
    ) -> None:
        """A row hidden for want of a bar is the loss worth minimising: the
        pause rows under it reached the file either way."""
        rows = list(
            killed_run_trace_processor.query(
                "SELECT s.name AS sname FROM slice s "
                "JOIN process_track pt ON s.track_id = pt.id "
                "WHERE pt.name = 'GC Pauses' AND s.name LIKE 'GC Pause%'"
            )
        )
        assert len(rows) == 3


class TestLivenessOnlyTrace:
    """A whole run in which nothing ever collected: no events, so no thread
    tracks and no counters, and every descriptor in the file was written at
    close. The trace processor still has to accept it, since an idle or
    short-lived target is an ordinary thing to monitor."""

    def test_every_polled_pid_draws_a_row(
        self,
        liveness_only_trace_processor: TraceProcessor,
    ) -> None:
        """Nothing named a track all run, so without the close-time
        descriptors this trace would draw no rows at all."""
        rows = list(
            liveness_only_trace_processor.query(
                "SELECT p.name AS pname, COUNT(s.id) AS n FROM process p "
                "JOIN process_track pt ON pt.upid = p.upid "
                "JOIN slice s ON s.track_id = pt.id GROUP BY p.name ORDER BY p.name"
            )
        )
        assert {r.pname: r.n for r in rows} == {
            f"Process {DEFAULT_PID}": 1,
            f"Process {_SECOND_PID}": 1,
        }

    def test_no_misplaced_end_events(self, liveness_only_trace_processor: TraceProcessor) -> None:
        assert _misplaced_end_events(liveness_only_trace_processor) == 0

    def test_both_pids_span_the_observed_range(
        self,
        liveness_only_trace_processor: TraceProcessor,
    ) -> None:
        rows = list(
            liveness_only_trace_processor.query(
                f"SELECT s.name AS name, s.ts AS ts, s.dur AS dur FROM slice s "
                f"JOIN track t ON s.track_id = t.id "
                f"WHERE t.name = '{_PROCESS_LIFETIME_TRACK_NAME}' ORDER BY s.name"
            )
        )
        span = (_LIVE_TICKS[0], _LIVE_TICKS[-1] - _LIVE_TICKS[0])
        assert {r.name: (r.ts, r.dur) for r in rows} == {
            f"Process {DEFAULT_PID}": span,
            f"Process {_SECOND_PID}": span,
        }


class TestReusedPidDrawsTwoOfEveryRow:
    """A pid held twice reaches a reader as two processes.

    The wire-level tests pin the bytes gcmon writes. This class asks the
    trace processor what it made of them: two ``ProcessDescriptor`` messages
    carrying one pid could have collapsed to a single ``upid``, and every
    byte assertion in the suite would still have passed (ADR-0011).

    Every query here scopes on ``upid`` or on the process name. Scoping on
    ``pid`` cannot tell a split from a merge, since the pid is equal by
    construction, and neither can counting rows.
    """

    def _processes(self, tp: TraceProcessor) -> dict[str, tuple[int, int]]:
        """``{name: (upid, start_ts)}`` for the reused pid's processes."""
        return {
            r.name: (r.upid, r.start_ts)
            for r in tp.query(f"SELECT upid, name, start_ts FROM process WHERE name GLOB 'Process {_REUSED_PID}*'")
        }

    def test_the_pid_gives_two_upids_each_with_its_own_start(
        self,
        reused_pid_trace_processor: TraceProcessor,
    ) -> None:
        """Two rows in ``process`` for one pid is two ``upid``s, since
        ``upid`` is that table's key, and each opens
        where its own process was first observed rather than where the
        pid was. A merge leaves one row here and every other test in the
        class red."""
        processes = self._processes(reused_pid_trace_processor)

        assert sorted(processes) == [_REUSE_FIRST_NAME, _REUSE_SECOND_NAME]
        assert {name: start for name, (_, start) in processes.items()} == {
            _REUSE_FIRST_NAME: _REUSE_FIRST_START,
            _REUSE_SECOND_NAME: _REUSE_SECOND_START,
        }

    def test_each_process_draws_its_pauses_on_its_own_row(
        self,
        reused_pid_trace_processor: TraceProcessor,
    ) -> None:
        """Both processes run an interpreter 0, so both draw a row named
        ``GC Pauses`` under a group named ``Interpreter 0``. What keeps their
        pauses apart is the ``upid``: a process row each, since the
        descriptor names the row pid gcmon counted for that process
        (ADR-0011)."""
        rows = list(
            reused_pid_trace_processor.query(
                f"SELECT p.name AS pname, pt.id AS ctrack_id, pt.upid AS upid, s.ts AS ts "
                f"FROM slice s "
                f"JOIN process_track pt ON s.track_id = pt.id "
                f"JOIN process p ON pt.upid = p.upid "
                f"WHERE s.name = '{_PAUSE_NAME}' ORDER BY p.name"
            )
        )

        assert [(r.pname, r.ts) for r in rows] == [
            (_REUSE_FIRST_NAME, _REUSE_FIRST_START),
            (_REUSE_SECOND_NAME, _REUSE_SECOND_START),
        ]
        assert len({r.ctrack_id for r in rows}) == 2, f"expected a pause row per process, got {rows}"
        assert len({r.upid for r in rows}) == 2, f"expected a process row per process, got {rows}"

    def test_each_process_draws_its_counters_on_its_own_tracks(
        self,
        reused_pid_trace_processor: TraceProcessor,
    ) -> None:
        """The two ``G0 collected`` values are apart, so a merged row
        would show one line stepping between them."""
        rows = list(
            reused_pid_trace_processor.query(
                "SELECT p.name AS pname, ct.id AS ctrack_id, c.value AS value "
                "FROM counter c "
                "JOIN process_counter_track ct ON c.track_id = ct.id "
                "JOIN process p ON p.upid = ct.upid "
                "WHERE ct.name = 'G0 collected' ORDER BY p.name"
            )
        )

        assert [(r.pname, r.value) for r in rows] == [
            (_REUSE_FIRST_NAME, float(_REUSE_FIRST_COLLECTED)),
            (_REUSE_SECOND_NAME, float(_REUSE_SECOND_COLLECTED)),
        ]
        assert len({r.ctrack_id for r in rows}) == 2, f"expected a counter track per process, got {rows}"

    def test_each_process_tiles_its_blind_intervals_on_its_own_loss_row(
        self,
        reused_pid_trace_processor: TraceProcessor,
    ) -> None:
        """One `GC Loss` row per process, so no span holds across the
        handover. The two windows carry different counts, which a shared
        row would draw as one sequence."""
        rows = list(
            reused_pid_trace_processor.query(
                "SELECT p.name AS pname, s.track_id AS track_id, s.ts AS ts, "
                "EXTRACT_ARG(s.arg_set_id, 'debug.lost_count') AS lost "
                "FROM slice s "
                "JOIN process_track pt ON s.track_id = pt.id "
                "JOIN process p ON pt.upid = p.upid "
                "JOIN track t ON t.id = s.track_id "
                "WHERE t.name LIKE 'GC Loss%' ORDER BY p.name"
            )
        )

        assert [(r.pname, r.ts, r.lost) for r in rows] == [
            (_REUSE_FIRST_NAME, _REUSE_FIRST_STOP, _REUSE_FIRST_COLLECTED),
            (_REUSE_SECOND_NAME, _REUSE_SECOND_STOP, _REUSE_SECOND_COLLECTED),
        ]
        assert len({r.track_id for r in rows}) == 2, f"expected a loss row per process, got {rows}"

    def test_each_process_gets_its_own_lifetime_bar(
        self,
        reused_pid_trace_processor: TraceProcessor,
    ) -> None:
        """Each bar covers its own process, so the gap between the two is
        visible on the rows rather than papered over by one bar spanning
        both."""
        rows = list(
            reused_pid_trace_processor.query(
                f"SELECT p.name AS pname, s.ts AS ts, s.dur AS dur FROM slice s "
                f"JOIN process_track pt ON s.track_id = pt.id "
                f"JOIN process p ON pt.upid = p.upid "
                f"WHERE s.name = '{_PROCESS_ROW_SLICE_NAME}' ORDER BY p.name"
            )
        )

        assert [(r.pname, r.ts, r.ts + r.dur) for r in rows] == [
            (_REUSE_FIRST_NAME, _REUSE_FIRST_START, _REUSE_FIRST_STOP + _REUSE_LOSS_WINDOW_NS),
            (_REUSE_SECOND_NAME, _REUSE_SECOND_START, _REUSE_SECOND_STOP + _REUSE_LOSS_WINDOW_NS),
        ]

    def test_each_process_track_carries_its_own_command_line(
        self,
        reused_pid_trace_processor: TraceProcessor,
    ) -> None:
        """The field that was wrong rather than merged: one command line
        was read per process all along, and the successor's had nowhere
        to go."""
        rows = list(
            reused_pid_trace_processor.query(
                "SELECT p.name AS pname, a.string_value AS description FROM args a "
                "JOIN process_track pt ON a.arg_set_id = pt.source_arg_set_id "
                "JOIN process p ON p.upid = pt.upid "
                "WHERE a.key = 'description' ORDER BY p.name"
            )
        )

        assert [(r.pname, r.description) for r in rows] == [
            (_REUSE_FIRST_NAME, " ".join(_REUSE_FIRST_CMDLINE)),
            (_REUSE_SECOND_NAME, " ".join(_REUSE_SECOND_CMDLINE)),
        ]

    def test_a_process_track_and_its_span_name_the_same_program(
        self,
        reused_pid_trace_processor: TraceProcessor,
    ) -> None:
        """The two disagreed before the split. A command line was read
        per process all along and the span drew the right one, while the
        single process track above both spans named the first process's
        program.
        """
        on_the_track = {
            r.pname: r.cmdline
            for r in reused_pid_trace_processor.query(
                "SELECT p.name AS pname, a.string_value AS cmdline FROM args a "
                "JOIN process_track pt ON a.arg_set_id = pt.source_arg_set_id "
                "JOIN process p ON p.upid = pt.upid WHERE a.key = 'description'"
            )
        }
        on_the_span = {
            r.sname: r.cmdline
            for r in reused_pid_trace_processor.query(
                f"SELECT s.name AS sname, a.string_value AS cmdline FROM args a "
                f"JOIN slice s ON s.arg_set_id = a.arg_set_id "
                f"JOIN track t ON s.track_id = t.id "
                f"WHERE t.name = '{_PROCESS_LIFETIME_TRACK_NAME}' "
                f"AND a.flat_key = '{_ARG_PREFIX}.cmdline'"
            )
        }

        assert on_the_span == {
            _REUSE_FIRST_NAME: " ".join(_REUSE_FIRST_CMDLINE),
            _REUSE_SECOND_NAME: " ".join(_REUSE_SECOND_CMDLINE),
        }
        assert on_the_track == on_the_span

    def test_a_span_pairs_with_its_process_track_by_name(
        self,
        reused_pid_trace_processor: TraceProcessor,
    ) -> None:
        """Equal names are the whole of the pairing. The epoch reaches no
        column of its own, so this is how a reader joins a span's drawn
        duration to a per-process aggregate (ADR-0011)."""
        spans = {
            r.name
            for r in reused_pid_trace_processor.query(
                f"SELECT s.name FROM slice s JOIN track t ON s.track_id = t.id "
                f"WHERE t.name = '{_PROCESS_LIFETIME_TRACK_NAME}'"
            )
        }

        assert spans == set(self._processes(reused_pid_trace_processor))

    def test_no_non_info_stat_is_raised(
        self,
        reused_pid_trace_processor: TraceProcessor,
    ) -> None:
        """The trace processor accepts what gcmon now writes: no END
        dropped and no descriptor rejected.

        It says nothing about the merge. A merge raises no stat, which is
        why every other test here reads a table instead.
        """
        rows = list(
            reused_pid_trace_processor.query(
                "SELECT name, severity, value FROM stats WHERE value != 0 AND severity != 'info'"
            )
        )

        assert [(r.name, r.severity, r.value) for r in rows] == []


_HELD_PID: int = 31415
_HELD_EPOCHS: int = 4
_HELD_STEP: int = 100_000_000
_HELD_SPAN: int = 40_000_000


def _held_start(epoch: int) -> int:
    """Where the *epoch*-th process on ``_HELD_PID`` was first observed."""
    return epoch * _HELD_STEP


def _held_name(epoch: int) -> str:
    return f"Process {_HELD_PID}" + ("" if epoch == 1 else f"#{epoch}")


def _write_pid_held_four_times_trace(tmp: Path) -> Path:
    """Write a trace for a run where one pid named four processes, one
    after another with no overlap."""
    path = tmp / "held.pb"
    exporter = PerfettoExporter(output_path=path, flush_threshold=1000)
    for epoch in range(1, _HELD_EPOCHS + 1):
        process = proc(_HELD_PID, epoch)
        exporter.add_process_cmdline(process, ("python3", "-m", f"target{epoch}"))
        exporter.add_event(
            process,
            create_mock_stats_item(
                gen=_GEN,
                iid=_IID,
                ts_start=_held_start(epoch),
                ts_stop=_held_start(epoch) + _HELD_SPAN,
                collected=epoch,
            ),
        )
    exporter.close()
    return path


@pytest.fixture
def pid_held_four_times_trace_processor(tmp_path: Path) -> Iterator[TraceProcessor]:
    path = _write_pid_held_four_times_trace(tmp_path)
    with open_trace_processor(path) as tp:
        yield tp


class TestAPidHeldFourTimesDrawsFourRows:
    """A pid handed on three times over, which is where a two-process trace
    stops being enough to catch a merge.

    The trace processor folds the third descriptor on a pid into the second's
    row and opens another for the fourth, so these four processes would come
    back as three rows, one carrying two ``Lifetime`` bars. gcmon writes a pid
    of its own per process to stop that (ADR-0011), which is why these queries
    scope on the name and read the operating system's pid off the ``pid``
    annotation.
    """

    def test_every_process_gets_a_row_of_its_own(
        self,
        pid_held_four_times_trace_processor: TraceProcessor,
    ) -> None:
        """Four descriptors, four ``upid``s, each stamped where its own
        process was first observed."""
        rows = list(
            pid_held_four_times_trace_processor.query(
                "SELECT upid, name, start_ts FROM process WHERE name GLOB 'Process *' ORDER BY start_ts"
            )
        )

        assert [r.name for r in rows] == [_held_name(e) for e in range(1, _HELD_EPOCHS + 1)]
        assert [r.start_ts for r in rows] == [_held_start(e) for e in range(1, _HELD_EPOCHS + 1)]
        assert len({r.upid for r in rows}) == _HELD_EPOCHS

    def test_every_row_draws_exactly_one_lifetime_bar(
        self,
        pid_held_four_times_trace_processor: TraceProcessor,
    ) -> None:
        """A merged row is what carries two, so counting bars per row is
        the assertion that fails without the synthetic pid."""
        rows = list(
            pid_held_four_times_trace_processor.query(
                "SELECT p.name AS pname, COUNT(*) AS bars "
                "FROM slice s "
                "JOIN process_track pt ON s.track_id = pt.id "
                "JOIN process p ON pt.upid = p.upid "
                "WHERE s.name = 'Lifetime' GROUP BY p.upid ORDER BY p.name"
            )
        )

        assert [(r.pname, r.bars) for r in rows] == [(_held_name(e), 1) for e in range(1, _HELD_EPOCHS + 1)]

    def test_every_row_keeps_its_own_pauses(
        self,
        pid_held_four_times_trace_processor: TraceProcessor,
    ) -> None:
        """One pause each, so a merged row shows two and an empty one none."""
        rows = list(
            pid_held_four_times_trace_processor.query(
                "SELECT p.name AS pname, s.ts AS ts "
                "FROM slice s "
                "JOIN process_track pt ON s.track_id = pt.id "
                "JOIN process p ON pt.upid = p.upid "
                f"WHERE s.name = '{_PAUSE_NAME}' ORDER BY s.ts"
            )
        )

        assert [(r.pname, r.ts) for r in rows] == [(_held_name(e), _held_start(e)) for e in range(1, _HELD_EPOCHS + 1)]

    def test_the_rows_carry_the_operating_system_pid_between_them(
        self,
        pid_held_four_times_trace_processor: TraceProcessor,
    ) -> None:
        """``process.pid`` is gcmon's own, one per process, and the pid the
        operating system handed out four times is what remains of it modulo
        none of them. It is on every bar as the ``pid`` annotation, which is
        what a reader joins on to gather the four back together."""
        rows = list(
            pid_held_four_times_trace_processor.query(
                "SELECT p.name AS pname, p.pid AS pid, "
                "EXTRACT_ARG(s.arg_set_id, 'debug.pid') AS os_pid "
                "FROM slice s "
                "JOIN process_track pt ON s.track_id = pt.id "
                "JOIN process p ON pt.upid = p.upid "
                "WHERE s.name = 'Lifetime' ORDER BY p.name"
            )
        )

        assert [r.os_pid for r in rows] == [_HELD_PID] * _HELD_EPOCHS
        row_pids = {r.pid for r in rows}
        assert len(row_pids) == _HELD_EPOCHS
        assert _HELD_PID not in row_pids


@pytest.mark.stress
class TestMultiFlushProcessesTrack:
    """Multi-flush stress test for the ``Processes`` track slice END.

    When the buffered exporter's ``flush_threshold`` is small enough to
    force many flushes for a single pid, the ``Processes``-track slice
    for that pid must end at the very last non-counter non-meta event
    ts across all flushes, not the first batch's last event. (Without
    the fix, the closeout emitted a slice END at the end of every
    convert call, so the trace processor paired the BEGIN with the
    first END and dropped the rest as orphan ENDs.)
    """

    def test_slice_end_is_last_event_ts(self, tmp_path: Path) -> None:
        pid = DEFAULT_PID
        n_items = 30
        # flush_threshold=5 forces ~6+ flushes for the n_items=30 GC
        # items plus the leading instant event.
        path = tmp_path / "trace.pftrace"
        exporter = PerfettoExporter(output_path=path, flush_threshold=5)
        try:
            exporter.add_instant_event(
                proc(pid),
                create_instant_msg(name=_INSTANT_NAME, ts=0),
            )
            for i in range(n_items):
                ts_start = 1_000_000 * (i + 1)
                ts_stop = ts_start + 50_000
                exporter.add_event(
                    proc(pid),
                    create_mock_stats_item(
                        gen=0,
                        iid=i,
                        collections=1,
                        collected=10,
                        uncollectable=0,
                        candidates=5,
                        heap_size=1000,
                        ts_start=ts_start,
                        ts_stop=ts_stop,
                    ),
                )
        finally:
            exporter.close()

        with open_trace_processor(path) as tp:
            rows = list(
                tp.query(
                    f"SELECT s.ts, s.dur FROM slice s "
                    f"JOIN track t ON s.track_id = t.id "
                    f"WHERE t.name = '{_PROCESS_LIFETIME_TRACK_NAME}' "
                    f"AND s.name = 'Process {pid}'"
                )
            )
            assert len(rows) == 1, f"expected exactly one Processes-track slice for pid {pid}, got {rows}"
            slice_ts = rows[0].ts
            slice_dur = rows[0].dur
            slice_end = slice_ts + slice_dur
            # Expected end: the end of the last GC item's pause.
            expected_end = 1_000_000 * n_items + 50_000
            assert slice_end == expected_end, (
                f"slice end mismatch: got {slice_end}, expected "
                f"{expected_end} (last non-counter non-meta event ts); "
                f"dur={slice_dur}, ts={slice_ts}"
            )
            # Also assert BEGIN is at the first non-meta event ts
            # (the instant event at ts=0).
            assert slice_ts == 0, f"slice begin ts mismatch: got {slice_ts}, expected 0"


class TestProcessOrderingIntegration:
    """Schema-validity guard for the new root track descriptor and the
    per-process ``sibling_order_rank`` field.

    The wire-level tests in ``TestProcessOrderingByFirstTs`` (test_perfetto_ordering.py)
    are the source of truth for the rank values; this class verifies that the
    Perfetto trace processor accepts the new protobuf layout (root descriptor
    with ``process_ordering`` / ``thread_ordering`` and process descriptors
    with ``sibling_order_rank``) and that the existing ``process`` / ``track``
    SQL tables are not regressed by the new fields.

    The trace processor does not expose ``sibling_order_rank`` as a SQL
    column - it is a UI rendering hint consumed by the Perfetto UI at
    render time. Therefore these tests can verify the trace is *valid*
    and that the process tracks are still recognized, but not the
    actual UI display order. UI ordering is verifiable only in the
    Perfetto UI itself.
    """

    def test_root_descriptor_does_not_appear_as_a_track_row(
        self,
        trace_processor: TraceProcessor,
    ) -> None:
        """The root descriptor (``uuid=0``) carries no ``name`` and no
        ``process``/``thread``/``counter`` sub-message, so it must NOT
        produce a row in the ``track`` SQL table. We check for this by
        asserting that no track has a NULL ``type`` column; every track
        in the table should be a recognized kind (process_track_event,
        thread_execution, counter, etc.). The NULL-name rows in the
        table correspond to ``thread_execution`` tracks and are
        therefore not related to the root descriptor.
        """
        rows = list(
            trace_processor.query(
                "SELECT id FROM track WHERE type IS NULL",
            )
        )
        assert rows == [], (
            f"root track descriptor should not create a track row with unknown type; got ids {[r.id for r in rows]}"
        )

    def test_process_table_unchanged_after_ranking(
        self,
        trace_processor: TraceProcessor,
    ) -> None:
        """The ``process`` SQL table must still contain one row per
        process that emitted events, and no more. Adding
        ``sibling_order_rank`` to the process track descriptor must not
        change the cardinality.

        Scoped on the name: ``process.pid`` is the pid gcmon writes for
        the row, and each row carries one of its own (ADR-0011).
        """
        rows = list(
            trace_processor.query(
                f"SELECT name, pid FROM process "
                f"WHERE name IN ('Process {DEFAULT_PID}', 'Process {_SECOND_PID}') ORDER BY name",
            )
        )
        assert [r.name for r in rows] == sorted([f"Process {DEFAULT_PID}", f"Process {_SECOND_PID}"]), (
            f"expected one process row per process; got {[r.name for r in rows]}"
        )
        assert len({r.pid for r in rows}) == 2, f"two processes share a row pid: {[r.pid for r in rows]}"

    def test_process_track_rows_still_present_after_ranking(
        self,
        trace_processor: TraceProcessor,
    ) -> None:
        """Regression guard: the process track rows (one per pid) must
        still be present in the ``track`` table after the new fields
        are added to the descriptor. The rank field is not asserted
        here (it is a UI concern); the existence of the rows is the
        contract.
        """
        rows = list(
            trace_processor.query(
                "SELECT name FROM track WHERE name LIKE 'Process %' ORDER BY name",
            )
        )
        assert [r.name for r in rows] == sorted(
            [
                f"Process {DEFAULT_PID}",
                f"Process {_SECOND_PID}",
            ]
        ), f"expected process track rows for both pids; got {[r.name for r in rows]}"

    def test_process_track_order_matches_rank(
        self,
        trace_processor: TraceProcessor,
    ) -> None:
        """The trace processor must order process tracks by
        ``sibling_order_rank`` when ``process_ordering=EXPLICIT`` is
        set on the root descriptor. The fixture emits ``_SECOND_PID``
        with an earlier first event ts (``_TS_START - 2_000_000``) and
        ``DEFAULT_PID`` with a later one (``_TS_START - 1_000_000``), so
        ``_SECOND_PID`` is expected to be ranked first (rank 0).

        The track table is what the Perfetto UI uses to render tracks;
        the track with the lower ``id`` appears first in the UI. We
        therefore assert that the row for ``_SECOND_PID`` has a lower
        track id than the row for ``DEFAULT_PID`` in the
        ``process_track_event`` rows.
        """
        rows = list(
            trace_processor.query(
                f"""
            SELECT t.id, p.name
            FROM track t
            JOIN process_track pt ON t.id = pt.id
            JOIN process p ON pt.upid = p.upid
            WHERE t.type = 'process_track_event'
              AND p.name IN ('Process {DEFAULT_PID}', 'Process {_SECOND_PID}')
        """
            )
        )
        name_to_id = {r.name: r.id for r in rows}
        first, second = f"Process {DEFAULT_PID}", f"Process {_SECOND_PID}"
        assert first in name_to_id
        assert second in name_to_id
        assert name_to_id[second] < name_to_id[first], (
            f"expected _SECOND_PID (earlier first event) to have lower track id; got {name_to_id}"
        )

    def test_process_table_start_ts_matches_first_event(
        self,
        trace_processor: TraceProcessor,
    ) -> None:
        """The ``process.start_ts`` column in the trace processor's
        SQL table must reflect the first non-meta event timestamp
        for each pid (i.e. the ``start_timestamp_ns`` written to the
        ``ProcessDescriptor`` by the encoder).

        The fixture emits ``DEFAULT_PID`` with its first event at
        ``_TS_START - 1_000_000`` and ``_SECOND_PID`` at
        ``_TS_START - 2_000_000`` (earlier).
        """
        rows = list(
            trace_processor.query(
                f"""
            SELECT name, start_ts
            FROM process
            WHERE name IN ('Process {DEFAULT_PID}', 'Process {_SECOND_PID}')
            ORDER BY name
        """
            )
        )
        start_ts = {r.name: r.start_ts for r in rows}
        assert start_ts == {
            f"Process {DEFAULT_PID}": _TS_START - 1_000_000,
            f"Process {_SECOND_PID}": _TS_START - 2_000_000,
        }, f"unexpected start_ts values: {start_ts}"


_RSS_PID_1: int = DEFAULT_PID
_RSS_PID_2: int = _SECOND_PID
_RSS_VAL_1: int = 4_194_304  # 4 MB
_RSS_VAL_2: int = 8_388_608  # 8 MB
_RSS_VAL_3: int = 2_097_152  # 2 MB
_RSS_TS_1: int = 500_000_000
_RSS_TS_2: int = 1_500_000_000
_RSS_TS_3: int = 2_500_000_000


def _write_trace_with_rss(tmp: Path) -> Path:
    path = tmp / "trace_with_rss.pb"
    exporter: PerfettoExporter = PerfettoExporter(output_path=path, flush_threshold=1000)
    exporter.add_rss_sample(proc(_RSS_PID_1), _RSS_VAL_1, _RSS_TS_1)
    exporter.add_rss_sample(proc(_RSS_PID_2), _RSS_VAL_2, _RSS_TS_2)
    exporter.add_rss_sample(proc(_RSS_PID_1), _RSS_VAL_3, _RSS_TS_3)
    exporter.close()
    return path


@pytest.fixture
def trace_processor_with_rss(tmp_path: Path) -> Iterator[TraceProcessor]:
    path = _write_trace_with_rss(tmp_path)
    with open_trace_processor(path) as tp:
        yield tp


class TestRssCounterTrackIntegration:
    """Integration tests verifying RSS counter tracks are populated in
    Perfetto traces and queryable through the trace processor."""

    def test_rss_counter_track_present(
        self,
        trace_processor_with_rss: TraceProcessor,
    ) -> None:
        rows = list(trace_processor_with_rss.query("SELECT name FROM counter_track WHERE name = 'rss'"))
        assert len(rows) >= 1, "expected at least one 'rss' counter track"
        for r in rows:
            assert r.name == "rss"

    def test_rss_counter_values_match(
        self,
        trace_processor_with_rss: TraceProcessor,
    ) -> None:
        """Values written via ``add_rss_sample`` must appear in the
        ``counter`` table with the correct timestamp and value."""
        for expected_ts, expected_val in (
            (_RSS_TS_1, _RSS_VAL_1),
            (_RSS_TS_3, _RSS_VAL_3),
        ):
            rows = list(
                trace_processor_with_rss.query(
                    f"SELECT c.value, c.ts FROM counter c "
                    f"JOIN counter_track ct ON c.track_id = ct.id "
                    f"WHERE ct.name = 'rss' AND c.ts = {expected_ts}"
                )
            )
            matching = [r for r in rows if abs(r.value - expected_val) < 1]
            assert matching, (
                f"no counter row found for ts={expected_ts} val={expected_val}; got {[(r.ts, r.value) for r in rows]}"
            )

    def test_rss_counter_outside_gc_metrics_group(
        self,
        trace_processor_with_rss: TraceProcessor,
    ) -> None:
        """RSS counter track must NOT be parented inside a ``GC Metrics``
        group; it should be a top-level counter. Since the trace processor
        may not surface ``parent_id`` for OS-scoped parent relationships,
        verify by checking there is no ``GC Metrics`` track in the trace."""
        gc_metrics_rows = list(trace_processor_with_rss.query("SELECT name FROM track WHERE name = 'GC Metrics'"))
        assert not gc_metrics_rows, f"GC Metrics track should NOT appear in an RSS-only trace; got {gc_metrics_rows}"

    def test_rss_counter_tracks_per_pid(
        self,
        trace_processor_with_rss: TraceProcessor,
    ) -> None:
        """Each PID gets its own RSS counter track. Verify by counting
        distinct RSS counter track ids and total counter values."""
        # Two distinct RSS counter tracks (one per PID).
        rss_track_ids = list(
            trace_processor_with_rss.query("SELECT DISTINCT id FROM counter_track WHERE name = 'rss' ORDER BY id")
        )
        assert len(rss_track_ids) == 2, (
            f"expected 2 distinct RSS counter track ids (one per PID), got {len(rss_track_ids)}"
        )

        # Total counter values should be 3: PID 1 has 2 samples,
        # PID 2 has 1 sample.
        total_values = list(
            trace_processor_with_rss.query(
                "SELECT COUNT(*) AS cnt FROM counter c "
                "JOIN counter_track ct ON c.track_id = ct.id "
                "WHERE ct.name = 'rss'"
            )
        )
        assert total_values[0].cnt == 3, (
            f"expected 3 RSS counter values total (2 for PID 1, 1 for PID 2), got {total_values[0].cnt}"
        )

        # Both expected PIDs appear in the process table.
        for pid in (_RSS_PID_1, _RSS_PID_2):
            proc_rows = list(trace_processor_with_rss.query(f"SELECT name FROM process WHERE name = 'Process {pid}'"))
            assert len(proc_rows) == 1, f"expected process row for PID {pid}"

    def test_rss_counter_track_name_and_unit(
        self,
        trace_processor_with_rss: TraceProcessor,
    ) -> None:
        """The RSS counter track is named ``rss``. Its unit column comes
        back as ``None`` or ``''``: gcmon sets no explicit unit."""
        rows = list(trace_processor_with_rss.query("SELECT name, unit FROM counter_track WHERE name = 'rss'"))
        assert len(rows) >= 1
        for r in rows:
            assert r.name == "rss"

    def test_rss_does_not_affect_gc_counters(
        self,
        tmp_path: Path,
    ) -> None:
        """Adding RSS samples must not remove or alter existing GC counter
        tracks: writing both GC events and RSS samples preserves GC tracks."""
        path = tmp_path / "trace_combined.pb"
        exporter = PerfettoExporter(output_path=path, flush_threshold=1000)
        exporter.add_event(
            proc(DEFAULT_PID),
            create_mock_stats_item(
                gen=0,
                iid=0,
                collections=_COLLECTIONS,
                collected=_COLLECTED,
                uncollectable=_UNCOLLECTABLE,
                candidates=_CANDIDATES,
                heap_size=_HEAP_SIZE,
            ),
        )
        exporter.add_rss_sample(proc(DEFAULT_PID), _RSS_VAL_1, _RSS_TS_1)
        exporter.close()

        with open_trace_processor(path) as tp:
            counter_tracks = {r.name.strip() for r in tp.query("SELECT name FROM counter_track")}
            # GC counter tracks should still be present.
            for expected in ("G0 collected", "G0 candidates", "heap_size"):
                assert expected in counter_tracks, (
                    f"GC counter track {expected!r} missing after adding RSS; got {sorted(counter_tracks)}"
                )
            assert "rss" in counter_tracks, (
                f"RSS counter track missing after adding RSS + GC events; got {sorted(counter_tracks)}"
            )


class TestTwoInterpretersHeapSizes:
    """Two interpreters in one process draw two `heap_size` rows.

    Both are named `heap_size`. What keeps them apart is the group each hangs
    off (ADR-0027): sharing a name *and* a parent is what the trace processor
    merges, and the qualified name ADR-0024 wrote stood in for the parent
    before there was one to hang off.
    """

    @pytest.fixture(scope="class")
    def two_interpreters(self, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TraceProcessor]:
        path = tmp_path_factory.mktemp("two_iids") / "trace.pb"
        exporter = PerfettoExporter(output_path=path, flush_threshold=1000)
        for iid, heap_size in ((0, 1_000), (1, 9_000)):
            exporter.add_event(
                proc(DEFAULT_PID),
                create_mock_stats_item(gen=0, iid=iid, heap_size=heap_size, ts_start=_TS_START, ts_stop=_TS_STOP),
            )
        exporter.add_rss_sample(proc(DEFAULT_PID), _RSS_VAL_1, _RSS_TS_1)
        exporter.close()
        with open_trace_processor(path) as tp:
            yield tp

    def test_each_interpreter_gets_a_row_of_its_own(self, two_interpreters: TraceProcessor) -> None:
        rows = list(
            two_interpreters.query(
                "SELECT ig.name AS iname, ct.id AS id FROM counter_track ct "
                "JOIN track ig ON ct.parent_id = ig.id "
                "WHERE ct.name = 'heap_size' ORDER BY ig.name"
            )
        )
        assert [r.iname for r in rows] == ["Interpreter 0", "Interpreter 1"]
        assert len({r.id for r in rows}) == 2, f"the two rows merged: {[dict(r.__dict__) for r in rows]}"

    def test_a_query_matching_the_bare_name_finds_it(self, two_interpreters: TraceProcessor) -> None:
        """What ADR-0024's qualifier broke and ADR-0027 gives back."""
        names = {r.name.strip() for r in two_interpreters.query("SELECT name FROM counter_track")}
        assert "heap_size" in names

    def test_a_query_can_select_one_heap(self, two_interpreters: TraceProcessor) -> None:
        """One hop up the parent chain, which is what names the interpreter
        now that the track name does not."""
        rows = list(
            two_interpreters.query(
                "SELECT c.value AS value FROM counter c "
                "JOIN counter_track ct ON c.track_id = ct.id "
                "JOIN track ig ON ct.parent_id = ig.id "
                "WHERE ct.name = 'heap_size' AND ig.name = 'Interpreter 1'"
            )
        )
        assert [r.value for r in rows] == [9_000]

    def test_each_row_parents_to_its_own_interpreter_group(self, two_interpreters: TraceProcessor) -> None:
        parents = [
            r.parent_id for r in two_interpreters.query("SELECT parent_id FROM counter_track WHERE name = 'heap_size'")
        ]
        assert len(parents) == 2
        assert len(set(parents)) == 2, f"both heap rows share a parent: {parents}"

    def test_rss_stays_bare(self, two_interpreters: TraceProcessor) -> None:
        """Its owner is the process, and a process holds one."""
        names = {r.name.strip() for r in two_interpreters.query("SELECT name FROM counter_track")}
        assert "rss" in names

    def test_every_other_counter_name_is_what_it_was(self, two_interpreters: TraceProcessor) -> None:
        names = {r.name.strip() for r in two_interpreters.query("SELECT name FROM counter_track")}
        assert {"G0 collected", "G0 candidates", "G0 duration"} <= names
