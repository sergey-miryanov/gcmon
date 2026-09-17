"""What the monitor reported live, and what a run cut short still draws."""

from __future__ import annotations

from perfetto.trace_processor import TraceProcessor

from gcmon.exporters.perfetto_format import _PAUSE_TRACK_NAME
from gcmon.exporters.perfetto_process_lifetime import (
    _PROCESS_LIFETIME_TRACK_NAME,
    _PROCESS_ROW_SLICE_NAME,
)
from gcmon.model.names import GC_PAUSE_NAME
from tests.conftest import DEFAULT_PID
from tests.exporters.perfetto_integration.traces import (
    _DEFAULT_ROW_NAME,
    _FAKE_CMDLINE_JOINED,
    _KILL_GC_START,
    _KILL_TICK,
    _LIVE_BUSY_CMDLINE,
    _LIVE_GC_START,
    _LIVE_QUIET_CMDLINE,
    _LIVE_TICKS,
    _SECOND_PID,
    _SECOND_ROW_NAME,
    _misplaced_end_events,
    _process_row_filter,
)


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
                f"WHERE t.name = '{_PROCESS_LIFETIME_TRACK_NAME}' AND s.name = '{_SECOND_ROW_NAME}'"
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
                f"WHERE t.name = '{_PROCESS_LIFETIME_TRACK_NAME}' AND s.name = '{_DEFAULT_ROW_NAME}' "
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
                f"WHERE p.name = '{_SECOND_ROW_NAME}'"
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
            _DEFAULT_ROW_NAME: " ".join(_LIVE_BUSY_CMDLINE),
            _SECOND_ROW_NAME: " ".join(_LIVE_QUIET_CMDLINE),
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
            _DEFAULT_ROW_NAME: 1,
            _SECOND_ROW_NAME: 1,
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
        """The bar keeps the row rendered, and the row keeps its command
        line."""
        rows = list(
            killed_run_trace_processor.query(
                f"SELECT a.string_value AS description FROM args a "
                f"JOIN process_track pt ON a.arg_set_id = pt.source_arg_set_id "
                f"JOIN process p ON p.upid = pt.upid "
                f"WHERE a.key = 'description' AND p.name = '{_SECOND_ROW_NAME}'"
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
                f"WHERE pt.name = '{_PAUSE_TRACK_NAME}' AND s.name LIKE '{GC_PAUSE_NAME}%'"
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
            _DEFAULT_ROW_NAME: 1,
            _SECOND_ROW_NAME: 1,
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
            _DEFAULT_ROW_NAME: span,
            _SECOND_ROW_NAME: span,
        }
