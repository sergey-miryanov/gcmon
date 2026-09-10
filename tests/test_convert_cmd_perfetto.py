"""Tests for the ``gcmon combine`` command's ``perfetto`` output format that
drive the real ``perfetto.trace_processor`` binary against combined traces
produced via the CLI.

The structural checks say the trace has the tracks and slices it should.
``TestTheTraceMatchesTheEventsItWasBuiltFrom`` is the stronger one: it reads
the trace back through a decoder gcmon did not write and compares it against
the ``list[TraceEvent]`` the same input produced.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any, Protocol

import pytest
from perfetto.trace_processor import TraceProcessor

from gcmon.analysis.jsonl_io import read_jsonl
from gcmon.exporters.trace_converter import convert_to_trace_format
from gcmon.model.trace_event import Slice, TraceEvent
from tests.helpers import create_mock_incremental_item, create_mock_stats_item, open_trace_processor


def _int(v: int | None) -> int:
    assert v is not None
    return v


class _NameRow(Protocol):
    name: str


# Multiple processes, multiple generations, multiple tids/iids per process.
# Counter-track-name coverage and thread-track coverage depend on these.
_PID_A: int = 999_001
_PID_B: int = 999_002
_IID_A1: int = 0
_IID_A2: int = 1
_IID_A3: int = 2
_IID_B1: int = 10
_TS_START: int = 1_500_000_000
_DURATION_NS: int = 5_000_000

# Counter-track names produced by the encoder for each generation. All three
# generations emit the same basic 3 per-gen metrics. `heap_size` is a single
# shared counter per (pid, iid) updated by every generation, not split per
# gen. `increment_size` is NOT a counter track; it lives on the `GC Pause`
# slice's args.
_G0_COUNTERS: frozenset[str] = frozenset(
    {
        "G0 collected",
        "G0 uncollectable",
        "G0 candidates",
    }
)
_G1_COUNTERS: frozenset[str] = frozenset(
    {
        "G1 collected",
        "G1 uncollectable",
        "G1 candidates",
    }
)
_G2_COUNTERS: frozenset[str] = frozenset(
    {
        "G2 collected",
        "G2 uncollectable",
        "G2 candidates",
    }
)
# One row per interpreter in the capture, and the capture holds four. The
# per-generation counters are one row per interpreter too, but they hang off
# each interpreter's own `GC Metrics` group and so collapse into one name
# here; these are siblings under the process track and cannot.
_HEAP_COUNTERS: frozenset[str] = frozenset(
    {
        "Thread 0 heap_size",
        "Thread 1 heap_size",
        "Thread 2 heap_size",
        "Thread 10 heap_size",
    }
)
_DURATION_COUNTERS: frozenset[str] = frozenset(
    {
        "G0 duration",
        "G1 duration",
        "G2 duration",
    }
)

# Pause slice args exposed via the trace processor.
_EXPECTED_PAUSE_ARGS: dict[str, int] = {
    "generation": 0,
    "iid": _IID_A1,
    "collections": 50,
    "heap_size": 52428800,
    "collected": 200,
    "uncollectable": 10,
    "candidates": 40,
}

# The namespace the trace processor puts a debug annotation under.
_ARG_PREFIX: str = "debug"


def _multi_dimensional_records() -> list[dict[str, int | float]]:
    """Build JSONL records exercising multiple pids, generations, iids.

    - pid=1001: 3 records (gen 0, 1, 2) across 3 distinct iids (0, 1, 2)
    - pid=2002: 1 record (gen 0) with iid=10
    """
    records: list[dict[str, int | float]] = []
    # pid=1001, iid=0, gen=0 (full collection, basic counters)
    item_g0 = create_mock_stats_item(
        gen=0,
        iid=_IID_A1,
        ts_start=_TS_START,
        ts_stop=_TS_START + _DURATION_NS,
    )
    records.append(
        {
            "pid": _PID_A,
            "tid": _IID_A1,
            "gen": item_g0.gen,
            "iid": item_g0.iid,
            "ts_start": item_g0.ts_start,
            "ts_stop": item_g0.ts_stop,
            "heap_size": item_g0.heap_size,
            "collections": item_g0.collections,
            "collected": item_g0.collected,
            "uncollectable": item_g0.uncollectable,
            "candidates": item_g0.candidates,
            "duration": item_g0.duration,
        }
    )
    # pid=1001, iid=1, gen=1 (incremental: exercises all sub-slices;
    # only `increment_size` is emitted as a G1 counter, the other
    # incremental fields appear in pause/sub-step args).
    item_g1 = create_mock_incremental_item(
        gen=1,
        iid=_IID_A2,
        ts_start=_TS_START + 100_000_000,
        ts_stop=_TS_START + 100_000_000 + _DURATION_NS,
    )
    records.append(
        {
            "pid": _PID_A,
            "tid": _IID_A2,
            "gen": item_g1.gen,
            "iid": item_g1.iid,
            "ts_start": item_g1.ts_start,
            "ts_stop": item_g1.ts_stop,
            "heap_size": item_g1.heap_size,
            "collections": item_g1.collections,
            "collected": item_g1.collected,
            "uncollectable": item_g1.uncollectable,
            "candidates": item_g1.candidates,
            "duration": item_g1.duration,
            # Incremental fields:
            "increment_size": _int(item_g1.increment_size),
            "alive_size": _int(item_g1.alive_size),
            "ts_mark_alive_start": _int(item_g1.ts_mark_alive_start),
            "ts_mark_alive_stop": _int(item_g1.ts_mark_alive_stop),
            "ts_fill_increment_start": _int(item_g1.ts_fill_increment_start),
            "ts_fill_increment_stop": _int(item_g1.ts_fill_increment_stop),
            "ts_deduce_unreachable_start": _int(item_g1.ts_deduce_unreachable_start),
            "ts_deduce_unreachable_stop": _int(item_g1.ts_deduce_unreachable_stop),
            "ts_handle_weakref_callbacks_start": _int(item_g1.ts_handle_weakref_callbacks_start),
            "ts_handle_weakref_callbacks_stop": _int(item_g1.ts_handle_weakref_callbacks_stop),
            "ts_finalize_garbage_stop": _int(item_g1.ts_finalize_garbage_stop),
            "finalized_garbage_count": _int(item_g1.finalized_garbage_count),
            "ts_handle_resurrected_stop": _int(item_g1.ts_handle_resurrected_stop),
            "ts_clear_weakrefs_stop": _int(item_g1.ts_clear_weakrefs_stop),
            "clear_weakrefs_count": _int(item_g1.clear_weakrefs_count),
            "ts_delete_garbage_start": _int(item_g1.ts_delete_garbage_start),
            "ts_delete_garbage_stop": _int(item_g1.ts_delete_garbage_stop),
            "deleted_garbage_count": _int(item_g1.deleted_garbage_count),
        }
    )
    # pid=1001, iid=2, gen=2 (full collection, basic counters)
    item_g2 = create_mock_stats_item(
        gen=2,
        iid=_IID_A3,
        ts_start=_TS_START + 200_000_000,
        ts_stop=_TS_START + 200_000_000 + _DURATION_NS,
    )
    records.append(
        {
            "pid": _PID_A,
            "tid": _IID_A3,
            "gen": item_g2.gen,
            "iid": item_g2.iid,
            "ts_start": item_g2.ts_start,
            "ts_stop": item_g2.ts_stop,
            "heap_size": item_g2.heap_size,
            "collections": item_g2.collections,
            "collected": item_g2.collected,
            "uncollectable": item_g2.uncollectable,
            "candidates": item_g2.candidates,
            "duration": item_g2.duration,
        }
    )
    # pid=2002, iid=10, gen=0 (second process, separate timeline)
    item_b = create_mock_stats_item(
        gen=0,
        iid=_IID_B1,
        ts_start=_TS_START + 300_000_000,
        ts_stop=_TS_START + 300_000_000 + _DURATION_NS,
    )
    records.append(
        {
            "pid": _PID_B,
            "tid": _IID_B1,
            "gen": item_b.gen,
            "iid": item_b.iid,
            "ts_start": item_b.ts_start,
            "ts_stop": item_b.ts_stop,
            "heap_size": item_b.heap_size,
            "collections": item_b.collections,
            "collected": item_b.collected,
            "uncollectable": item_b.uncollectable,
            "candidates": item_b.candidates,
            "duration": item_b.duration,
        }
    )
    return records


def _write_jsonl(records: list[dict[str, int | float]], path: Path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _run_combine(
    inputs: list[Path],
    output: Path,
    *,
    output_format: str = "perfetto",
    extra_args: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "-m", "gcmon", "combine"]
    cmd.extend(str(p) for p in inputs)
    cmd += ["-o", str(output), "--output-format", output_format]
    if extra_args:
        cmd.extend(extra_args)
    return subprocess.run(cmd, capture_output=True, text=True)


@pytest.fixture
def multi_pid_jsonl(tmp_path: Path) -> list[Path]:
    """Two JSONL files exercising multiple pids, generations, and iids."""
    records = _multi_dimensional_records()
    f1 = tmp_path / "trace_a.jsonl"
    f2 = tmp_path / "trace_b.jsonl"
    # Split across 2 files (file 1: pid=1001 records; file 2: pid=2002 record)
    _write_jsonl([r for r in records if r["pid"] == _PID_A], f1)
    _write_jsonl([r for r in records if r["pid"] == _PID_B], f2)
    return [f1, f2]


@pytest.fixture
def loaded_trace_processor(
    tmp_path: Path,
    multi_pid_jsonl: list[Path],
) -> Iterator[TraceProcessor]:
    """Combine two JSONL files into a Perfetto trace and load it."""
    out = tmp_path / "combined.pftrace"
    result = _run_combine(multi_pid_jsonl, out, output_format="perfetto")
    assert result.returncode == 0, (
        f"gcmon combine failed: rc={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    with open_trace_processor(out) as tp:
        yield tp


def _process_filter(pid: int) -> str:
    """SQL fragment scoping a query to the one process on *pid*.

    One join through ``process_track`` reaches every row gcmon writes for a
    process, the ones inside its ``Interpreters`` group included (ADR-0027).

    On the name, not on ``process.pid``: that column holds the pid gcmon
    writes for the row (ADR-0011).
    """
    return (
        f"JOIN process_track pt ON s.track_id = pt.id JOIN process p ON pt.upid = p.upid WHERE p.name = 'Process {pid}'"
    )


def _on_interpreter(iid: int) -> str:
    """SQL fragment narrowing a :func:`_process_filter` query to one
    interpreter, which the group its row hangs off is what names
    (ADR-0027)."""
    return f"AND EXISTS (SELECT 1 FROM track ig WHERE ig.id = pt.parent_id AND ig.name = 'Interpreter {iid}')"


def _row_set(rows: Iterable[_NameRow]) -> set[str]:
    """Extract the ``name`` column from a query result into a set of strings."""
    return {r.name for r in rows}


_Slice = tuple[str, int, tuple[tuple[str, object], ...]]


def _slices_from_events(events: Sequence[TraceEvent]) -> list[_Slice]:
    """Every slice the events describe: name, duration in nanoseconds, args.

    A `Slice` states both its ends, so this is a subtraction rather than a
    stack walk. What the walk it replaced could also check -- that no slice
    was left open -- is not a thing the converter can now get wrong.
    """
    return sorted(
        (event.name, event.ts_stop - event.ts_start, tuple(sorted(event.args.items())))
        for event in events
        if isinstance(event, Slice)
    )


def _slices_from_trace(tp: TraceProcessor) -> list[_Slice]:
    """The same shape, read out of the trace by the trace processor.

    Two slice kinds are dropped, both of them the Perfetto converter's own and
    neither built from a `TraceEvent`: the `Process {pid}` spans on the
    `Processes` track, and the `Lifetime` bar each process's own row carries
    over the interval gcmon observed it.
    """
    args_by_set: dict[int, dict[str, object]] = {}
    for row in tp.query("SELECT arg_set_id, flat_key, int_value, string_value, real_value FROM args"):
        if not row.flat_key.startswith(f"{_ARG_PREFIX}."):
            continue
        key = row.flat_key.removeprefix(f"{_ARG_PREFIX}.")
        if key == "name":
            continue
        # An args row fills one value column and leaves the others NULL,
        # which the stub's non-optional types do not describe.
        value: Any = row.int_value
        if value is None:
            text: Any = row.string_value
            value = text if text is not None else row.real_value
        args_by_set.setdefault(row.arg_set_id, {})[key] = value

    drawn: list[_Slice] = []
    for row in tp.query(
        "SELECT s.name, s.dur, s.arg_set_id, t.name AS track_name FROM slice s JOIN track t ON s.track_id = t.id"
    ):
        if row.track_name == "Processes" or row.name == "Lifetime":
            continue
        args = args_by_set.get(row.arg_set_id, {})
        drawn.append((row.name, row.dur, tuple(sorted(args.items()))))
    return sorted(drawn)


# ---------------------------------------------------------------------------
# Test classes
# ---------------------------------------------------------------------------


class TestCombinedTraceIsStructurallyComplete:
    """A combined trace has every track it should: a TrackDescriptor per pid,
    a thread track per iid, and the counter tracks for the right
    generations."""

    def test_counter_tracks_present(
        self,
        loaded_trace_processor: TraceProcessor,
    ) -> None:
        names = {
            r.name
            for r in loaded_trace_processor.query(
                "SELECT name FROM counter_track",
            )
        }
        expected = _G0_COUNTERS | _G1_COUNTERS | _G2_COUNTERS | _HEAP_COUNTERS | _DURATION_COUNTERS
        assert names == expected, (
            f"counter track names mismatch; missing: {expected - names}; unexpected: {names - expected}"
        )

    def test_no_increment_size_counter_track(
        self,
        loaded_trace_processor: TraceProcessor,
    ) -> None:
        rows = list(
            loaded_trace_processor.query(
                "SELECT name FROM counter_track WHERE name LIKE '%increment_size%'",
            )
        )
        assert rows == [], f"`increment_size` should not be a counter track; got: {[r.name for r in rows]}"

    def test_process_tracks_present(
        self,
        loaded_trace_processor: TraceProcessor,
    ) -> None:
        rows = sorted(
            r.name
            for r in loaded_trace_processor.query(
                "SELECT name FROM track WHERE name LIKE 'Process %'",
            )
        )
        assert rows == sorted([f"Process {_PID_A}", f"Process {_PID_B}"]), (
            f"expected process tracks for both PIDs, got {rows}"
        )

    def test_the_close_time_sweep_leaves_a_combined_process_alone(
        self,
        loaded_trace_processor: TraceProcessor,
    ) -> None:
        """`combine` reports no liveness, so every process here was described
        by the conversion pass and the sweep that describes a process gcmon
        only ever polled finds nothing left to do (ADR-0011). One bar per
        process, and no command line invented for it (ADR-0024)."""
        rows = list(
            loaded_trace_processor.query(
                "SELECT p.name AS pname, COUNT(*) AS n FROM slice s "
                "JOIN process_track pt ON s.track_id = pt.id "
                "JOIN process p ON p.upid = pt.upid "
                "WHERE s.name = 'Lifetime' GROUP BY p.name ORDER BY p.name"
            )
        )
        assert {r.pname: r.n for r in rows} == {f"Process {_PID_A}": 1, f"Process {_PID_B}": 1}

        described = list(
            loaded_trace_processor.query(
                "SELECT a.string_value AS description FROM args a "
                "JOIN process_track pt ON a.arg_set_id = pt.source_arg_set_id "
                "WHERE a.key = 'description'"
            )
        )
        assert described == []

    def test_a_converted_capture_says_how_much_of_it_was_read(
        self,
        loaded_trace_processor: TraceProcessor,
    ) -> None:
        """No exporter ran here: `combine` hands its events straight to the
        encoder. The count is taken in the convert pass, which is the one
        stage both paths share, so a converted capture reads its own records
        rather than reporting none."""
        rows = list(
            loaded_trace_processor.query(
                "SELECT p.name AS pname, a.int_value AS sampled FROM args a "
                "JOIN slice s ON s.arg_set_id = a.arg_set_id "
                "JOIN process_track pt ON s.track_id = pt.id "
                "JOIN process p ON p.upid = pt.upid "
                "WHERE s.name = 'Lifetime' AND a.flat_key = 'debug.sampled_count'"
            )
        )
        assert {r.pname: r.sampled for r in rows} == {f"Process {_PID_A}": 3, f"Process {_PID_B}": 1}

    def test_interpreter_groups_present(
        self,
        loaded_trace_processor: TraceProcessor,
    ) -> None:
        rows = sorted(
            r.name
            for r in loaded_trace_processor.query(
                "SELECT t.name FROM track t "
                "JOIN process_track lt ON t.parent_id = lt.id "
                "JOIN process p ON lt.upid = p.upid "
                f"WHERE lt.name = 'Interpreters' AND p.name = 'Process {_PID_A}'",
            )
        )
        for iid in (_IID_A1, _IID_A2, _IID_A3):
            assert f"Interpreter {iid}" in rows, f"missing 'Interpreter {iid}' under pid={_PID_A}; got {rows}"

    def test_pause_slice_exists(
        self,
        loaded_trace_processor: TraceProcessor,
    ) -> None:
        # 3 gen-0/gen-1/gen-2 slices for pid=1001 (iids 0,1,2),
        # 1 gen-0 slice for pid=2002 (iid 10) -> total 4 pause slices.
        rows = list(
            loaded_trace_processor.query(
                f"SELECT s.name FROM slice s {_process_filter(_PID_A)} AND s.name LIKE 'GC Pause(%)'",
            )
        )
        assert len(rows) == 3, f"expected 3 pause slices for pid={_PID_A}, got {rows}"
        rows_b = list(
            loaded_trace_processor.query(
                f"SELECT s.name FROM slice s {_process_filter(_PID_B)} AND s.name LIKE 'GC Pause(%)'",
            )
        )
        assert len(rows_b) == 1, f"expected 1 pause slice for pid={_PID_B}, got {rows_b}"


class TestCombineJsonlToPerfettoIntegration:
    """JSONL input path also produces a structurally complete Perfetto trace."""

    def test_pause_slice_args(
        self,
        loaded_trace_processor: TraceProcessor,
    ) -> None:
        rows = {
            r.flat_key: r.int_value
            for r in loaded_trace_processor.query(
                "SELECT flat_key, int_value FROM args "
                "WHERE arg_set_id IN ("
                f"  SELECT s.arg_set_id FROM slice s "
                f"  {_process_filter(_PID_A)} "
                "  AND s.name = 'GC Pause(0)' AND s.dur > 0 "
                f"  {_on_interpreter(_IID_A1)}"
                ")"
            )
        }
        for key, expected in _EXPECTED_PAUSE_ARGS.items():
            qualified = f"{_ARG_PREFIX}.{key}"
            assert qualified in rows, f"missing arg {qualified}; got {sorted(rows)}"
            assert rows[qualified] == expected, f"{qualified}: expected {expected}, got {rows[qualified]}"

    def test_full_gen1_sub_slices_present(
        self,
        loaded_trace_processor: TraceProcessor,
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
            r.name
            for r in loaded_trace_processor.query(
                f"SELECT DISTINCT s.name FROM slice s {_process_filter(_PID_A)}",
            )
        }
        missing = set(expected_sub_slices) - slice_names
        assert not missing, f"missing sub-slices for gen=1: {missing}"


class TestCombineNormalizePerfettoIntegration:
    """Per-file normalization zeroes each file's timeline independently."""

    def test_normalize_zeroes_per_file_minimum(
        self,
        tmp_path: Path,
        multi_pid_jsonl: list[Path],
    ) -> None:
        out = tmp_path / "combined_normalized.pftrace"
        result = _run_combine(
            multi_pid_jsonl,
            out,
            output_format="perfetto",
            extra_args=["--normalize"],
        )
        assert result.returncode == 0, result.stderr
        with open_trace_processor(out) as tp:
            # pid=1001 records are all in file 1. After per-file normalization
            # the first slice of pid=1001 has ts=0. Without --normalize, the
            # same slice would have ts=1_500_000_000 ns. Assert that the
            # minimum across the pid=1001 slice table is 0 (vs. 1.5B unnormalized).
            rows = list(
                tp.query(
                    f"SELECT MIN(ts) AS min_ts FROM slice s {_process_filter(_PID_A)}",
                )
            )
            assert len(rows) == 1
            assert rows[0].min_ts == 0, f"expected min_ts=0 after per-file normalize, got {rows[0].min_ts}"


class TestTheTraceMatchesTheEventsItWasBuiltFrom:
    """The strongest check in the suite, and the reason it exists.

    Everything else here compares a trace against expectations a human wrote
    from the same code, so a wrong field number is invisible: the constant is
    wrong on both sides. This one reads the `.pftrace` back through the trace
    processor, a decoder gcmon did not write, and compares what it saw against
    the `list[TraceEvent]` the same input produced. The events are the oracle,
    so nothing about the check depends on there being a second output format
    ([ADR-0001](../docs/adr/0001-hand-rolled-perfetto-protobuf-encoder.md)).
    """

    def test_every_slice_matches_the_events_behind_it(
        self,
        tmp_path: Path,
        multi_pid_jsonl: list[Path],
    ) -> None:
        out = tmp_path / "combined.pftrace"
        result = _run_combine(multi_pid_jsonl, out, output_format="perfetto")
        assert result.returncode == 0, result.stderr

        events: list[TraceEvent] = []
        for path in multi_pid_jsonl:
            events.extend(convert_to_trace_format(read_jsonl(path)))

        with open_trace_processor(out) as tp:
            read_back = _slices_from_trace(tp)
        expected = _slices_from_events(events)

        assert read_back == expected, (
            "the trace processor read something other than the events the trace was built from\n"
            f"only in the trace: {[s for s in read_back if s not in expected]}\n"
            f"only in the events: {[s for s in expected if s not in read_back]}"
        )
