"""Tests for the ``gcmon combine`` command's ``perfetto`` output format that
drive the real ``perfetto.trace_processor`` binary against combined traces
produced via the CLI.

The structural checks say the trace has the tracks and slices it should.
``TestTheTraceMatchesTheEventsItWasBuiltFrom`` is the stronger one: it reads
the trace back through a decoder gcmon did not write and compares it against
the ``list[TraceEvent]`` the same input produced.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any, Protocol

import pytest
from perfetto.trace_processor import TraceProcessor

from gcmon.analysis.jsonl_io import read_jsonl
from gcmon.exporters import JsonlExporter
from gcmon.exporters.perfetto_format import _INTERPRETER_LIST_NAME, _interpreter_group_name
from gcmon.exporters.perfetto_process_lifetime import (
    _PROCESS_LIFETIME_TRACK_NAME,
    _PROCESS_ROW_PREFIX,
    _PROCESS_ROW_SLICE_NAME,
    process_track_name,
)
from gcmon.exporters.trace_converter import convert_to_trace_format
from gcmon.model.data import GCStatsInfo
from gcmon.model.names import (
    CANDIDATES,
    CLEAR_WEAKREFS,
    COLLECTED,
    COLLECTIONS,
    DEDUCE_UNREACHABLE,
    DELETE_GARBAGE,
    DURATION,
    FILL_INCREMENT,
    FINALIZE_GARBAGE,
    GC_PAUSE_NAME,
    GENERATION,
    HANDLE_RESURRECTED,
    HANDLE_WEAKREFS,
    HEAP_SIZE,
    IID,
    MARK_ALIVE,
    NAME,
    SAMPLED_COUNT,
    UNCOLLECTABLE,
    counter_display_name,
    gc_pause_slice_name,
    phase_slice_name,
)
from gcmon.model.trace_event import Slice, TraceEvent
from gcmon.support.vocabulary import CMD_COMBINE, FORMAT_PERFETTO, PROGRAM_NAME
from tests.exporters.perfetto_integration.traces import _ARG_PREFIX, _on_interpreter, _process_filter
from tests.helpers import (
    SUBPROCESS_WATCHDOG,
    create_mock_incremental_item,
    create_mock_stats_item,
    open_trace_processor,
    proc,
)


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

# What each process's row is called, built the way the exporter builds it
# rather than spelled out again here.
_NAME_A: str = process_track_name(proc(_PID_A))
_NAME_B: str = process_track_name(proc(_PID_B))
_DURATION_NS: int = 5_000_000

# Counter-track names produced by the encoder for each generation. All three
# generations emit the same basic 3 per-gen metrics. `heap_size` is a single
# shared counter per (pid, iid) updated by every generation, not split per
# gen. `increment_size` is NOT a counter track; it lives on the `GC Pause`
# slice's args.
_G0_COUNTERS: frozenset[str] = frozenset(
    {
        counter_display_name(0, COLLECTED),
        counter_display_name(0, UNCOLLECTABLE),
        counter_display_name(0, CANDIDATES),
    }
)
_G1_COUNTERS: frozenset[str] = frozenset(
    {
        counter_display_name(1, COLLECTED),
        counter_display_name(1, UNCOLLECTABLE),
        counter_display_name(1, CANDIDATES),
    }
)
_G2_COUNTERS: frozenset[str] = frozenset(
    {
        counter_display_name(2, COLLECTED),
        counter_display_name(2, UNCOLLECTABLE),
        counter_display_name(2, CANDIDATES),
    }
)
# One row per interpreter in the capture, and the capture holds four. Each
# sits in that interpreter's own group, so the four collapse into one name
# here, the way the per-generation counters under `GC Metrics` already do.
_HEAP_COUNTERS: frozenset[str] = frozenset({HEAP_SIZE})
_DURATION_COUNTERS: frozenset[str] = frozenset(
    {
        counter_display_name(0, DURATION),
        counter_display_name(1, DURATION),
        counter_display_name(2, DURATION),
    }
)

# Pause slice args exposed via the trace processor.
_EXPECTED_PAUSE_ARGS: dict[str, int] = {
    GENERATION: 0,
    IID: _IID_A1,
    COLLECTIONS: 50,
    HEAP_SIZE: 52428800,
    COLLECTED: 200,
    UNCOLLECTABLE: 10,
    CANDIDATES: 40,
}


def _captures() -> dict[int, list[GCStatsInfo]]:
    """Records by pid, over several generations and interpreters.

    - ``_PID_A``: generations 0, 1 and 2, each on its own interpreter. The
      generation-1 record is incremental, so every sub-slice is drawn.
    - ``_PID_B``: one generation-0 record, on a timeline of its own.
    """
    return {
        _PID_A: [
            create_mock_stats_item(gen=0, iid=_IID_A1, ts_start=_TS_START, ts_stop=_TS_START + _DURATION_NS),
            create_mock_incremental_item(
                gen=1,
                iid=_IID_A2,
                ts_start=_TS_START + 100_000_000,
                ts_stop=_TS_START + 100_000_000 + _DURATION_NS,
            ),
            create_mock_stats_item(
                gen=2,
                iid=_IID_A3,
                ts_start=_TS_START + 200_000_000,
                ts_stop=_TS_START + 200_000_000 + _DURATION_NS,
            ),
        ],
        _PID_B: [
            create_mock_stats_item(
                gen=0,
                iid=_IID_B1,
                ts_start=_TS_START + 300_000_000,
                ts_stop=_TS_START + 300_000_000 + _DURATION_NS,
            ),
        ],
    }


def _write_jsonl(pid: int, records: list[GCStatsInfo], path: Path) -> None:
    """Through the exporter, so the file holds what a monitored run writes."""
    exporter = JsonlExporter(output_path=path)
    for record in records:
        exporter.add_event(proc(pid), record)
    exporter.close()


def _run_combine(
    inputs: list[Path],
    output: Path,
    *,
    output_format: str = FORMAT_PERFETTO,
    extra_args: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "-m", PROGRAM_NAME, CMD_COMBINE]
    cmd.extend(str(p) for p in inputs)
    cmd += ["-o", str(output), "--output-format", output_format]
    if extra_args:
        cmd.extend(extra_args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=SUBPROCESS_WATCHDOG)


@pytest.fixture
def multi_pid_jsonl(tmp_path: Path) -> list[Path]:
    """Two JSONL files exercising multiple pids, generations, and iids."""
    captures = _captures()
    f1 = tmp_path / "trace_a.jsonl"
    f2 = tmp_path / "trace_b.jsonl"
    _write_jsonl(_PID_A, captures[_PID_A], f1)
    _write_jsonl(_PID_B, captures[_PID_B], f2)
    return [f1, f2]


@pytest.fixture
def loaded_trace_processor(
    tmp_path: Path,
    multi_pid_jsonl: list[Path],
) -> Iterator[TraceProcessor]:
    """Combine two JSONL files into a Perfetto trace and load it."""
    out = tmp_path / "combined.pftrace"
    result = _run_combine(multi_pid_jsonl, out, output_format=FORMAT_PERFETTO)
    assert result.returncode == 0, (
        f"gcmon combine failed: rc={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    with open_trace_processor(out) as tp:
        yield tp


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
    `Processes` row, and the `Lifetime` bar each process's own row carries
    over the interval gcmon observed it.
    """
    args_by_set: dict[int, dict[str, object]] = {}
    for row in tp.query("SELECT arg_set_id, flat_key, int_value, string_value, real_value FROM args"):
        if not row.flat_key.startswith(f"{_ARG_PREFIX}."):
            continue
        key = row.flat_key.removeprefix(f"{_ARG_PREFIX}.")
        if key == NAME:
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
        if row.track_name == _PROCESS_LIFETIME_TRACK_NAME or row.name == _PROCESS_ROW_SLICE_NAME:
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
                f"SELECT name FROM track WHERE name LIKE '{_PROCESS_ROW_PREFIX}%'",
            )
        )

        assert rows == sorted([_NAME_A, _NAME_B]), f"expected process tracks for both PIDs, got {rows}"

    def test_the_close_time_sweep_leaves_a_combined_process_alone(
        self,
        loaded_trace_processor: TraceProcessor,
    ) -> None:
        """`combine` reports no liveness, so every process here was described
        by the conversion pass and the sweep that describes a process gcmon
        only ever polled finds nothing left to do (ADR-0028). One bar per
        process, and no command line invented for it (ADR-0024)."""
        rows = list(
            loaded_trace_processor.query(
                "SELECT p.name AS pname, COUNT(*) AS n FROM slice s "
                "JOIN process_track pt ON s.track_id = pt.id "
                "JOIN process p ON p.upid = pt.upid "
                f"WHERE s.name = '{_PROCESS_ROW_SLICE_NAME}' GROUP BY p.name ORDER BY p.name"
            )
        )
        assert {r.pname: r.n for r in rows} == {_NAME_A: 1, _NAME_B: 1}

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
                f"WHERE s.name = '{_PROCESS_ROW_SLICE_NAME}' AND a.flat_key = '{_ARG_PREFIX}.{SAMPLED_COUNT}'"
            )
        )

        assert {r.pname: r.sampled for r in rows} == {_NAME_A: 3, _NAME_B: 1}

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
                f"WHERE lt.name = '{_INTERPRETER_LIST_NAME}' AND p.name = '{_NAME_A}'",
            )
        )

        for iid in (_IID_A1, _IID_A2, _IID_A3):
            assert _interpreter_group_name(iid) in rows, (
                f"missing '{_interpreter_group_name(iid)}' under pid={_PID_A}; got {rows}"
            )

    def test_pause_slice_exists(
        self,
        loaded_trace_processor: TraceProcessor,
    ) -> None:
        # 3 gen-0/gen-1/gen-2 slices for pid=1001 (iids 0,1,2),
        # 1 gen-0 slice for pid=2002 (iid 10) -> total 4 pause slices.
        rows = list(
            loaded_trace_processor.query(
                f"SELECT s.name FROM slice s {_process_filter(_PID_A)} AND s.name LIKE '{GC_PAUSE_NAME}(%)'",
            )
        )
        assert len(rows) == 3, f"expected 3 pause slices for pid={_PID_A}, got {rows}"

        rows_b = list(
            loaded_trace_processor.query(
                f"SELECT s.name FROM slice s {_process_filter(_PID_B)} AND s.name LIKE '{GC_PAUSE_NAME}(%)'",
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
                f"  AND s.name = '{gc_pause_slice_name(0)}' AND s.dur > 0 "
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
            phase_slice_name(MARK_ALIVE, 1),
            phase_slice_name(FILL_INCREMENT, 1),
            phase_slice_name(DEDUCE_UNREACHABLE, 1),
            phase_slice_name(HANDLE_WEAKREFS, 1),
            phase_slice_name(FINALIZE_GARBAGE, 1),
            phase_slice_name(HANDLE_RESURRECTED, 1),
            phase_slice_name(CLEAR_WEAKREFS, 1),
            phase_slice_name(DELETE_GARBAGE, 1),
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
            output_format=FORMAT_PERFETTO,
            extra_args=["--normalize"],
        )

        assert result.returncode == 0, result.stderr
        with open_trace_processor(out) as tp:
            # One pid per file, so each minimum is a file's own zero. Without
            # --normalize both sit at the capture's raw timestamps.
            minimums = {
                pid: next(iter(tp.query(f"SELECT MIN(ts) AS min_ts FROM slice s {_process_filter(pid)}"))).min_ts
                for pid in (_PID_A, _PID_B)
            }
        assert minimums == {_PID_A: 0, _PID_B: 0}


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

        result = _run_combine(multi_pid_jsonl, out, output_format=FORMAT_PERFETTO)

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
