"""A pid the operating system handed out more than once.

Every row belongs to a process rather than to a pid, so two processes sharing
one draw two of everything, and each ranks on its own first observation.
"""

from __future__ import annotations

from perfetto.trace_processor import TraceProcessor

from gcmon.exporters.perfetto_process_lifetime import (
    _PROCESS_LIFETIME_TRACK_NAME,
    _PROCESS_ROW_PREFIX,
    _PROCESS_ROW_SLICE_NAME,
    process_track_name,
)
from gcmon.exporters.trace_converter import counter_display_name
from gcmon.model.names import (
    CMDLINE,
    COLLECTED,
    GC_LOSS_NAME,
    LOST_COUNT,
    PID,
)
from tests.exporters.perfetto_integration.traces import (
    _DEFAULT_ROW_NAME,
    _FIRST_EPOCH,
    _HELD_EPOCHS,
    _HELD_PID,
    _LOSS_WINDOW_NS,
    _PAUSE_NAME,
    _REUSED_PID,
    _SECOND_EPOCH,
    _SECOND_ROW_NAME,
    _TS_START,
    _held_name,
    _held_start,
    flat_key,
)
from tests.helpers import proc


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
            for r in tp.query(
                f"SELECT upid, name, start_ts FROM process WHERE name GLOB '{process_track_name(proc(_REUSED_PID))}*'"
            )
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

        assert sorted(processes) == [_FIRST_EPOCH.track_name, _SECOND_EPOCH.track_name]
        assert {name: start for name, (_, start) in processes.items()} == {
            _FIRST_EPOCH.track_name: _FIRST_EPOCH.ts_start,
            _SECOND_EPOCH.track_name: _SECOND_EPOCH.ts_start,
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
            (_FIRST_EPOCH.track_name, _FIRST_EPOCH.ts_start),
            (_SECOND_EPOCH.track_name, _SECOND_EPOCH.ts_start),
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
                f"WHERE ct.name = '{counter_display_name(0, COLLECTED)}' ORDER BY p.name"
            )
        )

        assert [(r.pname, r.value) for r in rows] == [
            (_FIRST_EPOCH.track_name, float(_FIRST_EPOCH.collected)),
            (_SECOND_EPOCH.track_name, float(_SECOND_EPOCH.collected)),
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
                f"EXTRACT_ARG(s.arg_set_id, '{flat_key(LOST_COUNT)}') AS lost "
                "FROM slice s "
                "JOIN process_track pt ON s.track_id = pt.id "
                "JOIN process p ON pt.upid = p.upid "
                "JOIN track t ON t.id = s.track_id "
                f"WHERE t.name LIKE '{GC_LOSS_NAME}%' ORDER BY p.name"
            )
        )

        assert [(r.pname, r.ts, r.lost) for r in rows] == [
            (_FIRST_EPOCH.track_name, _FIRST_EPOCH.ts_stop, _FIRST_EPOCH.collected),
            (_SECOND_EPOCH.track_name, _SECOND_EPOCH.ts_stop, _SECOND_EPOCH.collected),
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
            (_FIRST_EPOCH.track_name, _FIRST_EPOCH.ts_start, _FIRST_EPOCH.ts_stop + _LOSS_WINDOW_NS),
            (_SECOND_EPOCH.track_name, _SECOND_EPOCH.ts_start, _SECOND_EPOCH.ts_stop + _LOSS_WINDOW_NS),
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
            (_FIRST_EPOCH.track_name, " ".join(_FIRST_EPOCH.cmdline)),
            (_SECOND_EPOCH.track_name, " ".join(_SECOND_EPOCH.cmdline)),
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
                f"AND a.flat_key = '{flat_key(CMDLINE)}'"
            )
        }

        assert on_the_span == {
            _FIRST_EPOCH.track_name: " ".join(_FIRST_EPOCH.cmdline),
            _SECOND_EPOCH.track_name: " ".join(_SECOND_EPOCH.cmdline),
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
                f"SELECT upid, name, start_ts FROM process WHERE name GLOB '{_PROCESS_ROW_PREFIX}*' ORDER BY start_ts"
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
                f"WHERE s.name = '{_PROCESS_ROW_SLICE_NAME}' GROUP BY p.upid ORDER BY p.name"
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
                f"EXTRACT_ARG(s.arg_set_id, '{flat_key(PID)}') AS os_pid "
                "FROM slice s "
                "JOIN process_track pt ON s.track_id = pt.id "
                "JOIN process p ON pt.upid = p.upid "
                f"WHERE s.name = '{_PROCESS_ROW_SLICE_NAME}' ORDER BY p.name"
            )
        )

        assert [r.os_pid for r in rows] == [_HELD_PID] * _HELD_EPOCHS
        row_pids = {r.pid for r in rows}
        assert len(row_pids) == _HELD_EPOCHS
        assert _HELD_PID not in row_pids


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
                f"WHERE name IN ('{_DEFAULT_ROW_NAME}', '{_SECOND_ROW_NAME}') ORDER BY name",
            )
        )

        assert [r.name for r in rows] == sorted([_DEFAULT_ROW_NAME, _SECOND_ROW_NAME]), (
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
                f"SELECT name FROM track WHERE name LIKE '{_PROCESS_ROW_PREFIX}%' ORDER BY name",
            )
        )

        assert [r.name for r in rows] == sorted(
            [
                _DEFAULT_ROW_NAME,
                _SECOND_ROW_NAME,
            ]
        ), f"expected process track rows for both pids; got {[r.name for r in rows]}"

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
            WHERE name IN ('{_DEFAULT_ROW_NAME}', '{_SECOND_ROW_NAME}')
            ORDER BY name
        """
            )
        )

        start_ts = {r.name: r.start_ts for r in rows}
        assert start_ts == {
            _DEFAULT_ROW_NAME: _TS_START - 1_000_000,
            _SECOND_ROW_NAME: _TS_START - 2_000_000,
        }, f"unexpected start_ts values: {start_ts}"
