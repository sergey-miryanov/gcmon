"""The run rows: every pause in the run on its generation's row, drawn beside
the process rows from the same events."""

from __future__ import annotations

from pathlib import Path

from perfetto.trace_processor import TraceProcessor

from gcmon.exporters.encoder import Layout
from gcmon.exporters.perfetto_format import (
    _INTERPRETER_LIST_NAME,
    _LOSS_TRACK_NAME,
    _PAUSE_TRACK_NAME,
    _interpreter_group_name,
)
from gcmon.exporters.perfetto_process_lifetime import _PROCESS_LIFETIME_TRACK_NAME, process_track_name
from gcmon.exporters.perfetto_run_rows import (
    _ALL_PAUSES_GROUP_NAME,
    _MARKS_TRACK_NAME,
    _RSS_GROUP_NAME,
    _generation_row_name,
)
from gcmon.exporters.trace_converter import convert_item_to_trace_format
from gcmon.model.names import GENERATIONS, PID, PID_EPOCH, RSS
from gcmon.model.process import Process
from gcmon.model.trace_event import Slice
from tests.conftest import DEFAULT_PID
from tests.exporters.perfetto_integration.traces import (
    _GEN,
    _INSTANT_NAME,
    _KILL_GC_START,
    _KILL_GC_STOP,
    _KILL_LAST_GC_START,
    _KILL_LAST_GC_STOP,
    _KILL_LOSS_START,
    _KILL_LOSS_STOP,
    _KILL_MARK_TS,
    _MARKS,
    _OVERLAPPING_LOSSES,
    _OVERLAPPING_PAUSES,
    _PAUSE_NAME,
    _RSS_SAMPLES,
    _RSS_VAL_1,
    _SECOND_PID,
    _write_every_kind_trace,
    _write_killed_marks_and_rss_trace,
    _write_marks_and_rss_trace,
    _write_overlapping_losses_trace,
    flat_key,
    row_track_ids,
    rows_drawn_under,
)
from tests.helpers import open_trace_processor, proc

type _RowPath = tuple[str, ...]
type _Drawn = tuple[list[tuple[object, ...]], list[tuple[int, float]]]

_PID: str = f"EXTRACT_ARG(s.arg_set_id, '{flat_key(PID)}')"

_PID_EPOCH: str = f"EXTRACT_ARG(s.arg_set_id, '{flat_key(PID_EPOCH)}')"

# Whether a slice carries either, as the 0 or 1 the stub's `int` describes.
_NAMES_A_PROCESS: str = f"({_PID} IS NOT NULL) AS has_pid, ({_PID_EPOCH} IS NOT NULL) AS has_pid_epoch"


_GEN_ROW: tuple[str, str] = (_ALL_PAUSES_GROUP_NAME, _generation_row_name(_GEN))


def _rss_row_name(process: Process) -> str:
    """Not the process's own name, which its row and its span already hold."""
    return f"{process_track_name(process)} {RSS}"


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
        track_ids = row_track_ids(overlapping_pauses_trace_processor, *_GEN_ROW)

        assert len(track_ids.split(",")) == 3

    def test_each_pause_keeps_its_own_width(self, overlapping_pauses_trace_processor: TraceProcessor) -> None:
        tp = overlapping_pauses_trace_processor
        track_ids = row_track_ids(tp, *_GEN_ROW)

        drawn = tp.query(f"SELECT ts, dur FROM slice WHERE track_id IN ({track_ids}) AND depth = 0")

        assert sorted((r.ts, r.dur) for r in drawn) == sorted(
            (record.ts_start, record.ts_stop - record.ts_start) for _, record in _OVERLAPPING_PAUSES
        )

    def test_each_sub_phase_sits_inside_its_own_pause(self, overlapping_pauses_trace_processor: TraceProcessor) -> None:
        tp = overlapping_pauses_trace_processor
        track_ids = row_track_ids(tp, *_GEN_ROW)

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
        track_ids = row_track_ids(tp, *_GEN_ROW)

        drawn = tp.query(
            f"SELECT {_PID} AS pid, {_PID_EPOCH} AS pid_epoch FROM slice s "
            f"WHERE s.track_id IN ({track_ids}) AND s.depth = 0"
        )

        assert sorted((r.pid, r.pid_epoch) for r in drawn) == sorted(
            (process.pid, process.pid_epoch) for process, _ in _OVERLAPPING_PAUSES
        )

    def test_a_sub_phase_does_not(self, overlapping_pauses_trace_processor: TraceProcessor) -> None:
        tp = overlapping_pauses_trace_processor
        track_ids = row_track_ids(tp, *_GEN_ROW)

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


class TestLossWindowsAtOnce:
    """Three interpreters' loss windows, two of them in one process, overlap
    on the one root ``GC Loss``, where one interpreter's touching polls still
    read as neighbours."""

    def test_share_one_root_row_a_track_per_interpreter(
        self, overlapping_losses_trace_processor: TraceProcessor
    ) -> None:
        track_ids = row_track_ids(overlapping_losses_trace_processor, _LOSS_TRACK_NAME)

        assert len(track_ids.split(",")) == 3

    def test_each_window_keeps_its_own_width_inside_none(
        self, overlapping_losses_trace_processor: TraceProcessor
    ) -> None:
        tp = overlapping_losses_trace_processor
        track_ids = row_track_ids(tp, _LOSS_TRACK_NAME)

        drawn = tp.query(f"SELECT ts, dur, depth FROM slice WHERE track_id IN ({track_ids})")

        assert sorted((r.ts, r.dur, r.depth) for r in drawn) == sorted(
            (loss.ts_start, loss.ts_stop - loss.ts_start, 0) for _, loss in _OVERLAPPING_LOSSES
        )

    def test_leave_no_end_unpaired(self, overlapping_losses_trace_processor: TraceProcessor) -> None:
        flagged = overlapping_losses_trace_processor.query(
            "SELECT name FROM stats WHERE severity IN ('error', 'data_loss') AND value > 0"
        )

        assert [r.name for r in flagged] == []

    def test_each_interpreter_keeps_its_own_loss_row(self, tmp_path: Path) -> None:
        _order, drawn = _every_row(_write_overlapping_losses_trace(tmp_path))
        expected: dict[_RowPath, list[tuple[object, ...]]] = {}
        for process, loss in _OVERLAPPING_LOSSES:
            row = (
                process_track_name(process),
                _INTERPRETER_LIST_NAME,
                _interpreter_group_name(loss.iid),
                _LOSS_TRACK_NAME,
            )
            expected.setdefault(row, []).append((loss.ts_start, loss.ts_stop - loss.ts_start, 0))

        assert {row: sorted(s[1:4] for s in drawn[row][0]) for row in expected} == {
            row: sorted(windows) for row, windows in expected.items()
        }


class TestWhoseALossWindowIs:
    def test_a_window_on_the_root_row_names_its_process(
        self, overlapping_losses_trace_processor: TraceProcessor
    ) -> None:
        tp = overlapping_losses_trace_processor
        track_ids = row_track_ids(tp, _LOSS_TRACK_NAME)

        drawn = tp.query(
            f"SELECT {_PID} AS pid, {_PID_EPOCH} AS pid_epoch FROM slice s WHERE s.track_id IN ({track_ids})"
        )

        assert sorted((r.pid, r.pid_epoch) for r in drawn) == sorted(
            (process.pid, process.pid_epoch) for process, _ in _OVERLAPPING_LOSSES
        )

    def test_nothing_on_an_interpreter_s_loss_row_does(
        self, overlapping_losses_trace_processor: TraceProcessor
    ) -> None:
        drawn = overlapping_losses_trace_processor.query(
            f"SELECT {_NAMES_A_PROCESS} FROM slice s JOIN track t ON s.track_id = t.id "
            f"WHERE t.name = '{_LOSS_TRACK_NAME}' AND t.parent_id IS NOT NULL"
        )

        assert {(r.has_pid, r.has_pid_epoch) for r in drawn} == {(0, 0)}


class TestMarks:
    """Two processes' marks, one at the same instant as another's."""

    def test_every_mark_sits_on_one_root_row(self, marks_and_rss_trace_processor: TraceProcessor) -> None:
        tp = marks_and_rss_trace_processor
        track_ids = row_track_ids(tp, _MARKS_TRACK_NAME)

        drawn = tp.query(f"SELECT name, ts, dur FROM slice WHERE track_id IN ({track_ids})")

        assert sorted((r.name, r.ts, r.dur) for r in drawn) == sorted((_INSTANT_NAME, ts, 0) for _, ts in _MARKS)

    def test_a_mark_on_the_root_row_names_its_process(self, marks_and_rss_trace_processor: TraceProcessor) -> None:
        tp = marks_and_rss_trace_processor
        track_ids = row_track_ids(tp, _MARKS_TRACK_NAME)

        drawn = tp.query(
            f"SELECT s.ts, {_PID} AS pid, {_PID_EPOCH} AS pid_epoch FROM slice s WHERE s.track_id IN ({track_ids})"
        )

        assert sorted((r.ts, r.pid, r.pid_epoch) for r in drawn) == sorted(
            (ts, process.pid, process.pid_epoch) for process, ts in _MARKS
        )

    def test_a_mark_still_sits_on_its_process_row_naming_nothing(
        self, marks_and_rss_trace_processor: TraceProcessor
    ) -> None:
        drawn = marks_and_rss_trace_processor.query(
            f"SELECT s.ts, p.name AS process, {_NAMES_A_PROCESS} FROM slice s "
            "JOIN process_track t ON s.track_id = t.id JOIN process p USING (upid) "
            f"WHERE s.name = '{_INSTANT_NAME}'"
        )

        assert sorted((r.ts, str(r.process), r.has_pid, r.has_pid_epoch) for r in drawn) == sorted(
            (ts, process_track_name(process), 0, 0) for process, ts in _MARKS
        )


class TestRss:
    """Two processes' samples, the second pid's process starting first and
    sampling second."""

    def test_holds_a_counter_per_process_in_rank_order(self, marks_and_rss_trace_processor: TraceProcessor) -> None:
        tp = marks_and_rss_trace_processor
        group = tp.query(
            "SELECT EXTRACT_ARG(source_arg_set_id, 'child_ordering') AS ordering FROM track "
            f"WHERE name = '{_RSS_GROUP_NAME}'"
        )

        assert rows_drawn_under(tp, _RSS_GROUP_NAME) == [
            _rss_row_name(proc(_SECOND_PID)),
            _rss_row_name(proc(DEFAULT_PID)),
        ]
        assert [str(r.ordering) for r in group] == ["explicit"]

    def test_each_counter_holds_what_its_process_row_s_rss_does(self, tmp_path: Path) -> None:
        _order, drawn = _every_row(_write_marks_and_rss_trace(tmp_path))
        expected: dict[str, list[tuple[int, float]]] = {}
        for process, ts, rss in _RSS_SAMPLES:
            expected.setdefault(process_track_name(process), []).append((ts, rss))

        assert {name: drawn[("", _RSS_GROUP_NAME, f"{name} {RSS}")][1] for name in expected} == expected
        assert {name: drawn[(name, RSS)][1] for name in expected} == expected


def test_a_run_killed_before_close_keeps_every_flushed_pause_on_both_sets_of_rows(
    killed_run_trace_processor: TraceProcessor,
) -> None:
    """Two processes' pauses and a third flushed after one of them retired,
    each on its own flush."""
    tp = killed_run_trace_processor
    on_run_rows = tp.query(
        f"SELECT ts, dur FROM slice WHERE track_id IN ({row_track_ids(tp, *_GEN_ROW)}) AND depth = 0"
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


def test_a_run_killed_before_close_keeps_its_flushed_loss_window_on_both_sets_of_rows(
    killed_run_trace_processor: TraceProcessor,
) -> None:
    tp = killed_run_trace_processor
    on_run_rows = tp.query(f"SELECT ts, dur FROM slice WHERE track_id IN ({row_track_ids(tp, _LOSS_TRACK_NAME)})")
    on_process_rows = tp.query(
        f"SELECT s.ts, s.dur FROM slice s JOIN process_track t ON s.track_id = t.id WHERE t.name = '{_LOSS_TRACK_NAME}'"
    )

    expected = [(_KILL_LOSS_START, _KILL_LOSS_STOP - _KILL_LOSS_START)]
    assert [(r.ts, r.dur) for r in on_run_rows] == expected
    assert [(r.ts, r.dur) for r in on_process_rows] == expected


def test_a_run_killed_before_close_keeps_its_flushed_mark_and_sample_on_both_sets_of_rows(tmp_path: Path) -> None:
    with open_trace_processor(_write_killed_marks_and_rss_trace(tmp_path)) as tp:
        marks_on_run_rows = tp.query(f"SELECT ts FROM slice WHERE track_id IN ({row_track_ids(tp, _MARKS_TRACK_NAME)})")
        marks_on_process_rows = tp.query(
            f"SELECT s.ts FROM slice s JOIN process_track t ON s.track_id = t.id WHERE s.name = '{_INSTANT_NAME}'"
        )
        rss_row = row_track_ids(tp, _RSS_GROUP_NAME, _rss_row_name(proc(DEFAULT_PID)))
        samples_on_run_rows = tp.query(f"SELECT ts, value FROM counter WHERE track_id IN ({rss_row})")
        samples_on_process_rows = tp.query(
            f"SELECT c.ts, c.value FROM counter c JOIN counter_track t ON c.track_id = t.id WHERE t.name = '{RSS}'"
        )

        assert [r.ts for r in marks_on_run_rows] == [_KILL_MARK_TS]
        assert [r.ts for r in marks_on_process_rows] == [_KILL_MARK_TS]
        assert [(r.ts, r.value) for r in samples_on_run_rows] == [(_KILL_MARK_TS, _RSS_VAL_1)]
        assert [(r.ts, r.value) for r in samples_on_process_rows] == [(_KILL_MARK_TS, _RSS_VAL_1)]


def test_the_run_rows_come_out_at_the_top_level_by_name(tmp_path: Path) -> None:
    order, _drawn = _every_row(_write_every_kind_trace(tmp_path, Layout.FULL))

    assert [row[1] for row in order if row[0] == "" and len(row) == 2] == [
        _ALL_PAUSES_GROUP_NAME,
        _LOSS_TRACK_NAME,
        _MARKS_TRACK_NAME,
        _PROCESS_LIFETIME_TRACK_NAME,
        _RSS_GROUP_NAME,
    ]


def test_the_process_layout_draws_the_full_trace_less_its_run_rows(tmp_path: Path) -> None:
    """From the same events: every row in the order the UI lists it, with
    every slice and counter value on it."""
    full_order, full_drawn = _every_row(_write_every_kind_trace(tmp_path, Layout.FULL))
    process_order, process_drawn = _every_row(_write_every_kind_trace(tmp_path, Layout.PROCESS))

    run_rows = [row for row in full_order if row[0] == "" and row[1] != _PROCESS_LIFETIME_TRACK_NAME]

    assert run_rows == [
        ("", _ALL_PAUSES_GROUP_NAME),
        *(("", _ALL_PAUSES_GROUP_NAME, _generation_row_name(gen)) for gen in GENERATIONS),
        ("", _LOSS_TRACK_NAME),
        ("", _MARKS_TRACK_NAME),
        ("", _RSS_GROUP_NAME),
        ("", _RSS_GROUP_NAME, _rss_row_name(proc(DEFAULT_PID))),
        ("", _RSS_GROUP_NAME, _rss_row_name(proc(_SECOND_PID, 2))),
    ]
    assert process_order == [row for row in full_order if row not in run_rows]
    assert process_drawn == {row: drawn for row, drawn in full_drawn.items() if row not in run_rows}
