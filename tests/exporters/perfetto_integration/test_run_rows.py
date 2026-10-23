"""The run rows: every pause in the run on its generation's row, drawn beside
the process rows from the same events."""

from __future__ import annotations

from pathlib import Path

from perfetto.trace_processor import TraceProcessor

from gcmon.exporters.encoder import Layout
from gcmon.exporters.perfetto_format import _PAUSE_TRACK_NAME
from gcmon.exporters.perfetto_run_rows import _ALL_PAUSES_GROUP_NAME, _generation_row_name
from gcmon.exporters.trace_converter import convert_item_to_trace_format
from gcmon.model.names import GENERATIONS, PID, PID_EPOCH
from gcmon.model.trace_event import Slice
from tests.exporters.perfetto_integration.traces import (
    _GEN,
    _KILL_GC_START,
    _KILL_GC_STOP,
    _KILL_LAST_GC_START,
    _KILL_LAST_GC_STOP,
    _OVERLAPPING_PAUSES,
    _PAUSE_NAME,
    _write_every_kind_trace,
    flat_key,
    rows_drawn_under,
)
from tests.helpers import open_trace_processor

type _RowPath = tuple[str, ...]
type _Drawn = tuple[list[tuple[object, ...]], list[tuple[int, float]]]

_PID: str = f"EXTRACT_ARG(s.arg_set_id, '{flat_key(PID)}')"

_PID_EPOCH: str = f"EXTRACT_ARG(s.arg_set_id, '{flat_key(PID_EPOCH)}')"

# Whether a slice carries either, as the 0 or 1 the stub's `int` describes.
_NAMES_A_PROCESS: str = f"({_PID} IS NOT NULL) AS has_pid, ({_PID_EPOCH} IS NOT NULL) AS has_pid_epoch"


def _row_track_ids(tp: TraceProcessor, name: str) -> str:
    """The tracks the one row called *name* is drawn from, a lane each, as a
    list SQL's ``IN`` takes."""
    list(tp.query("INCLUDE PERFETTO MODULE viz.summary.track_event"))
    [row] = list(tp.query(f"SELECT track_ids FROM _track_event_tracks_ordered_groups WHERE name = '{name}'"))
    return str(row.track_ids)


def _every_row(path: Path) -> tuple[list[_RowPath], dict[_RowPath, _Drawn]]:
    """Every row the trace at *path* draws, in the order the UI lists them,
    and what is drawn on each: its slices with their args, and its counter
    values.

    A row is named by the names from the top level down to it, after the name
    of the process it sits in, or an empty one. Track ids and uuids are the
    trace's own, so none is kept.
    """
    with open_trace_processor(path) as tp:
        list(tp.query("INCLUDE PERFETTO MODULE viz.summary.track_event"))
        processes = {r.upid: r.name for r in tp.query("SELECT upid, name FROM process WHERE name IS NOT NULL")}
        rows = list(
            tp.query(
                "SELECT min_track_id AS id, upid, parent_id, name, track_ids, order_id "
                "FROM _track_event_tracks_ordered_groups"
            )
        )
        args: dict[int, list[tuple[object, object]]] = {}
        for arg in tp.query("SELECT arg_set_id, key, display_value FROM args"):
            args.setdefault(arg.arg_set_id, []).append((arg.key, arg.display_value))

        order: list[_RowPath] = []
        drawn: dict[_RowPath, _Drawn] = {}

        def walk(parent_id: int | None, parent_path: _RowPath) -> None:
            for row in sorted((r for r in rows if r.parent_id == parent_id), key=lambda r: r.order_id):
                row_path = (*parent_path, row.name) if parent_path else (processes.get(row.upid, ""), row.name)
                slices = tp.query(
                    f"SELECT name, ts, dur, depth, arg_set_id FROM slice WHERE track_id IN ({row.track_ids})"
                )
                counters = tp.query(f"SELECT ts, value FROM counter WHERE track_id IN ({row.track_ids})")
                order.append(row_path)
                drawn[row_path] = (
                    sorted((s.name, s.ts, s.dur, s.depth, tuple(sorted(args.get(s.arg_set_id, [])))) for s in slices),
                    sorted((c.ts, c.value) for c in counters),
                )
                walk(row.id, row_path)

        walk(None, ())
    assert len(drawn) == len(order), "two rows share a path, so one would hide the other"
    return order, drawn


class TestAllGcPauses:
    def test_holds_a_row_per_generation_in_rank_order(self, three_generations_trace_processor: TraceProcessor) -> None:
        """``Gen0`` to ``Gen2`` sort the same by name, so the order the rows
        come out in cannot tell ranked rows from unranked ones: the ordering
        and the ranks they were described with are read as well."""
        tp = three_generations_trace_processor
        group = tp.query(
            "SELECT EXTRACT_ARG(source_arg_set_id, 'child_ordering') AS ordering FROM track "
            f"WHERE name = '{_ALL_PAUSES_GROUP_NAME}'"
        )
        ranks = tp.query(
            "SELECT DISTINCT t.name, EXTRACT_ARG(t.source_arg_set_id, 'sibling_order_rank') AS rank "
            f"FROM track t JOIN track g ON t.parent_id = g.id WHERE g.name = '{_ALL_PAUSES_GROUP_NAME}'"
        )

        assert rows_drawn_under(tp, _ALL_PAUSES_GROUP_NAME) == [_generation_row_name(gen) for gen in GENERATIONS]
        assert [str(r.ordering) for r in group] == ["explicit"]
        assert sorted((r.name, r.rank) for r in ranks) == [(_generation_row_name(gen), gen) for gen in GENERATIONS]


class TestInterpretersPausingAtOnce:
    """Three interpreters' gen-0 pauses overlap, two of them in one process, so
    their one row needs a lane each."""

    def test_share_one_row_a_lane_each(self, overlapping_pauses_trace_processor: TraceProcessor) -> None:
        track_ids = _row_track_ids(overlapping_pauses_trace_processor, _generation_row_name(_GEN))

        assert len(track_ids.split(",")) == 3

    def test_each_pause_keeps_its_own_width(self, overlapping_pauses_trace_processor: TraceProcessor) -> None:
        tp = overlapping_pauses_trace_processor
        track_ids = _row_track_ids(tp, _generation_row_name(_GEN))

        drawn = tp.query(f"SELECT ts, dur FROM slice WHERE track_id IN ({track_ids}) AND depth = 0")

        assert sorted((r.ts, r.dur) for r in drawn) == sorted(
            (record.ts_start, record.ts_stop - record.ts_start) for _, record in _OVERLAPPING_PAUSES
        )

    def test_each_sub_phase_sits_inside_its_own_pause(self, overlapping_pauses_trace_processor: TraceProcessor) -> None:
        tp = overlapping_pauses_trace_processor
        track_ids = _row_track_ids(tp, _generation_row_name(_GEN))

        drawn = tp.query(
            "SELECT s.ts, s.dur, p.ts AS parent_ts FROM slice s JOIN slice p ON s.parent_id = p.id "
            f"WHERE s.track_id IN ({track_ids})"
        )

        assert sorted((r.ts, r.dur, r.parent_ts) for r in drawn) == sorted(
            (event.ts_start, event.ts_stop - event.ts_start, record.ts_start)
            for process, record in _OVERLAPPING_PAUSES
            for event in convert_item_to_trace_format(process, record)
            if isinstance(event, Slice) and event.name != _PAUSE_NAME
        )

    def test_leave_no_end_unpaired(self, overlapping_pauses_trace_processor: TraceProcessor) -> None:
        """``misplaced_end_event`` is filed under ``data_loss``, which a check
        on ``error`` alone misses."""
        flagged = overlapping_pauses_trace_processor.query(
            "SELECT name FROM stats WHERE severity IN ('error', 'data_loss') AND value > 0"
        )

        assert [r.name for r in flagged] == []


class TestWhoseAPauseIs:
    """Two processes' windows on ``Processes`` can share a pause on the run
    rows, so the pause names its process. On the process rows the row does."""

    def test_a_pause_on_the_run_rows_names_its_process(
        self, overlapping_pauses_trace_processor: TraceProcessor
    ) -> None:
        tp = overlapping_pauses_trace_processor
        track_ids = _row_track_ids(tp, _generation_row_name(_GEN))

        drawn = tp.query(
            f"SELECT {_PID} AS pid, {_PID_EPOCH} AS pid_epoch FROM slice s "
            f"WHERE s.track_id IN ({track_ids}) AND s.depth = 0"
        )

        assert sorted((r.pid, r.pid_epoch) for r in drawn) == sorted(
            (process.pid, process.pid_epoch) for process, _ in _OVERLAPPING_PAUSES
        )

    def test_a_sub_phase_does_not(self, overlapping_pauses_trace_processor: TraceProcessor) -> None:
        tp = overlapping_pauses_trace_processor
        track_ids = _row_track_ids(tp, _generation_row_name(_GEN))

        drawn = tp.query(f"SELECT {_NAMES_A_PROCESS} FROM slice s WHERE s.track_id IN ({track_ids}) AND s.depth > 0")

        assert {(r.has_pid, r.has_pid_epoch) for r in drawn} == {(0, 0)}

    def test_nothing_on_an_interpreter_s_pause_row_does(
        self, overlapping_pauses_trace_processor: TraceProcessor
    ) -> None:
        drawn = overlapping_pauses_trace_processor.query(
            f"SELECT {_NAMES_A_PROCESS} FROM slice s JOIN track t ON s.track_id = t.id "
            f"WHERE t.name = '{_PAUSE_TRACK_NAME}'"
        )

        assert {(r.has_pid, r.has_pid_epoch) for r in drawn} == {(0, 0)}


def test_a_run_killed_before_close_keeps_every_flushed_pause_on_both_sets_of_rows(
    killed_run_trace_processor: TraceProcessor,
) -> None:
    """Two processes' pauses and a third flushed after one of them retired,
    each on its own flush."""
    tp = killed_run_trace_processor
    on_run_rows = tp.query(
        f"SELECT ts, dur FROM slice WHERE track_id IN ({_row_track_ids(tp, _generation_row_name(_GEN))}) AND depth = 0"
    )
    on_process_rows = tp.query(
        "SELECT s.ts, s.dur FROM slice s JOIN track t ON s.track_id = t.id "
        f"WHERE t.name = '{_PAUSE_TRACK_NAME}' AND s.depth = 0"
    )

    expected = [
        (_KILL_GC_START, _KILL_GC_STOP - _KILL_GC_START),
        (_KILL_GC_START, _KILL_GC_STOP - _KILL_GC_START),
        (_KILL_LAST_GC_START, _KILL_LAST_GC_STOP - _KILL_LAST_GC_START),
    ]
    assert sorted((r.ts, r.dur) for r in on_run_rows) == expected
    assert sorted((r.ts, r.dur) for r in on_process_rows) == expected


def test_the_process_layout_draws_the_full_trace_less_its_run_rows(tmp_path: Path) -> None:
    """From the same events: every row in the order the UI lists it, with
    every slice and counter value on it."""
    full_order, full_drawn = _every_row(_write_every_kind_trace(tmp_path, Layout.FULL))
    process_order, process_drawn = _every_row(_write_every_kind_trace(tmp_path, Layout.PROCESS))

    run_rows = [row for row in full_order if row[1] == _ALL_PAUSES_GROUP_NAME]

    assert run_rows == [
        ("", _ALL_PAUSES_GROUP_NAME),
        *(("", _ALL_PAUSES_GROUP_NAME, _generation_row_name(gen)) for gen in GENERATIONS),
    ]
    assert process_order == [row for row in full_order if row not in run_rows]
    assert process_drawn == {row: drawn for row, drawn in full_drawn.items() if row not in run_rows}
