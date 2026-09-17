"""The ``Processes`` row a run draws, read back out of the trace.

One bar per process over the interval gcmon saw it, and the cases that decide
where a bar begins and ends: spans that cross, spans of zero length, and a run
flushed more than once.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from perfetto.trace_processor import TraceProcessor

from gcmon.exporters import PerfettoExporter
from gcmon.exporters.perfetto_process_lifetime import (
    _PROCESS_LIFETIME_TRACK_NAME,
    _PROCESS_ROW_SLICE_NAME,
    process_track_name,
)
from gcmon.exporters.trace_converter import duration_text
from gcmon.model.names import (
    CMDLINE,
    LOST_COUNT,
    LOST_PAUSE,
    LOST_PAUSE_NS,
    PID,
    PID_EPOCH,
    SAMPLED_COUNT,
)
from tests.conftest import DEFAULT_PID
from tests.data_helpers import create_instant_msg
from tests.exporters.perfetto_integration.traces import (
    _ARG_PREFIX,
    _CROSS_A_START,
    _CROSS_A_STOP,
    _CROSS_B_START,
    _CROSS_B_STOP,
    _DEFAULT_ROW_NAME,
    _FAKE_CMDLINE_JOINED,
    _FIRST_EPOCH,
    _INSTANT_NAME,
    _LOST_PAUSE_NS,
    _SECOND_EPOCH,
    _SECOND_PID,
    _SECOND_ROW_NAME,
    _THIRD_ROW_NAME,
    _TS_START,
    _ZERO_CLIPPED_START,
    _ZERO_CLIPPED_STOP,
    _ZERO_CROSSER_START,
    _ZERO_CROSSER_STOP,
    _ZERO_INSTANT_TS,
    _misplaced_end_events,
    _process_row_filter,
)
from tests.helpers import (
    create_mock_stats_item,
    open_trace_processor,
    proc,
)


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
            _DEFAULT_ROW_NAME: (default_start, 10_000_000),
            _SECOND_ROW_NAME: (_TS_START - 2_000_000, 7_000_000),
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
                f"AND s.name = '{_SECOND_ROW_NAME}'"
            )
        )
        assert [r.dur for r in shared] == [999_999], "expected the shared row to draw the clipped span"
        assert self._lifetimes(trace_processor)[_SECOND_ROW_NAME][1] == 7_000_000

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
            (process_track_name(proc(pid)), f"{_ARG_PREFIX}.{key}")
            for pid in (DEFAULT_PID, _SECOND_PID)
            for key in (
                CMDLINE,
                PID,
                PID_EPOCH,
                "interpreters",
                SAMPLED_COUNT,
                LOST_COUNT,
                LOST_PAUSE,
                LOST_PAUSE_NS,
            )
        }
        # Read each annotation out of the column its type puts it in, so a
        # `pid_epoch` written as a string reads back as a missing int.
        assert {r.name: r.string_value for r in rows if r.flat_key.endswith(CMDLINE)} == {
            _DEFAULT_ROW_NAME: _FAKE_CMDLINE_JOINED,
            _SECOND_ROW_NAME: _FAKE_CMDLINE_JOINED,
        }
        assert {r.name: r.int_value for r in rows if r.flat_key.endswith(PID_EPOCH)} == {
            _DEFAULT_ROW_NAME: 1,
            _SECOND_ROW_NAME: 1,
        }
        assert {r.name: r.int_value for r in rows if r.flat_key.endswith(".pid")} == {
            _DEFAULT_ROW_NAME: DEFAULT_PID,
            _SECOND_ROW_NAME: _SECOND_PID,
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
            _DEFAULT_ROW_NAME: 3,
            _SECOND_ROW_NAME: 1,
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
        assert counts[(_DEFAULT_ROW_NAME, SAMPLED_COUNT)] == 3
        assert counts[(_SECOND_ROW_NAME, SAMPLED_COUNT)] == 1
        assert counts[(_DEFAULT_ROW_NAME, LOST_COUNT)] == 0
        assert counts[(_DEFAULT_ROW_NAME, LOST_PAUSE_NS)] == 0
        text = {(r.name, r.flat_key.rsplit(".", 1)[1]): r.string_value for r in rows}
        assert text[(_DEFAULT_ROW_NAME, LOST_PAUSE)] == "0ns"

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
        assert counts[(_FIRST_EPOCH.track_name, SAMPLED_COUNT)] == 1
        assert counts[(_SECOND_EPOCH.track_name, SAMPLED_COUNT)] == 1
        assert counts[(_FIRST_EPOCH.track_name, LOST_COUNT)] == _FIRST_EPOCH.collected
        assert counts[(_SECOND_EPOCH.track_name, LOST_COUNT)] == _SECOND_EPOCH.collected
        assert counts[(_FIRST_EPOCH.track_name, LOST_PAUSE_NS)] == _LOST_PAUSE_NS
        text = {(r.name, r.flat_key.rsplit(".", 1)[1]): r.string_value for r in rows}
        assert text[(_FIRST_EPOCH.track_name, LOST_PAUSE)] == duration_text(_LOST_PAUSE_NS)

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
                f"WHERE p.name = '{_DEFAULT_ROW_NAME}' "
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
            _DEFAULT_ROW_NAME,
            _SECOND_ROW_NAME,
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
            _DEFAULT_ROW_NAME: _FAKE_CMDLINE_JOINED,
            _SECOND_ROW_NAME: _FAKE_CMDLINE_JOINED,
        }

    def test_allocates_no_track(self, trace_processor: TraceProcessor) -> None:
        """The bar reuses the process track uuid. A uuid of its own would draw
        a second row, with no descriptor behind it to name the process."""
        rows = list(
            trace_processor.query(
                f"SELECT s.name AS name, s.track_id AS track_id FROM slice s "
                f"JOIN process_track pt ON s.track_id = pt.id "
                f"JOIN process p ON pt.upid = p.upid "
                f"WHERE p.name = '{_DEFAULT_ROW_NAME}' "
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
        assert lifetimes[_THIRD_ROW_NAME] == (_ZERO_INSTANT_TS, 0)
        assert lifetimes[_DEFAULT_ROW_NAME] == (
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
            _DEFAULT_ROW_NAME,
            _SECOND_ROW_NAME,
        ], f"expected exactly one dur-bearing Process <pid> slice per pid, got {[(r.name, r.dur) for r in rows]}"
        for r in rows:
            assert r.dur > 0, f"slice {r.name!r} has dur={r.dur}, expected > 0"
        spans = {r.name: (r.ts, r.ts + r.dur) for r in rows}
        default_start = _TS_START - 1_000_000
        assert spans == {
            _DEFAULT_ROW_NAME: (default_start, _TS_START + 9_000_000),
            _SECOND_ROW_NAME: (_TS_START - 2_000_000, default_start - 1),
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
            (_DEFAULT_ROW_NAME, "debug.real_start_ts"): _TS_START - 1_000_000,
            (_DEFAULT_ROW_NAME, "debug.real_end_ts"): _TS_START + 9_000_000,
            (_SECOND_ROW_NAME, "debug.real_start_ts"): _TS_START - 2_000_000,
            (_SECOND_ROW_NAME, "debug.real_end_ts"): _TS_START + 5_000_000,
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
        assert rows
        for r in rows:
            assert pat.match(r.name), (
                f"slice name {r.name!r} on the {_PROCESS_LIFETIME_TRACK_NAME!r} track "
                f"must match 'Process <pid>' or 'Process <pid>#N'"
            )

    @pytest.mark.parametrize(
        ("pid", "first_event"),
        [(DEFAULT_PID, _TS_START - 1_000_000), (_SECOND_PID, _TS_START - 2_000_000)],
    )
    def test_a_slice_begins_at_its_process_s_first_event(
        self,
        trace_processor: TraceProcessor,
        pid: int,
        first_event: int,
    ) -> None:
        """The instant each process opens on, which the trace writes ahead of
        that process's first record."""
        rows = list(
            trace_processor.query(
                f"SELECT s.ts FROM slice s "
                f"JOIN track t ON s.track_id = t.id "
                f"WHERE t.name = '{_PROCESS_LIFETIME_TRACK_NAME}' "
                f"AND s.name = '{process_track_name(proc(pid))}'"
            )
        )

        assert [r.ts for r in rows] == [first_event]

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
                    f"  AND s.name = '{process_track_name(proc(pid))}'"
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
            _DEFAULT_ROW_NAME: (_CROSS_A_START, _CROSS_B_START - 1),
            # Untouched: this is the span that used to be truncated.
            _SECOND_ROW_NAME: (_CROSS_B_START, _CROSS_B_STOP),
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
            (_DEFAULT_ROW_NAME, "debug.real_start_ts"): _CROSS_A_START,
            (_DEFAULT_ROW_NAME, "debug.real_end_ts"): _CROSS_A_STOP,
            (_SECOND_ROW_NAME, "debug.real_start_ts"): _CROSS_B_START,
            (_SECOND_ROW_NAME, "debug.real_end_ts"): _CROSS_B_STOP,
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
            _DEFAULT_ROW_NAME: 1,
            _SECOND_ROW_NAME: 0,
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
            _THIRD_ROW_NAME: (_ZERO_INSTANT_TS, 0),
            _DEFAULT_ROW_NAME: (_ZERO_CLIPPED_START, 0),
            _SECOND_ROW_NAME: (_ZERO_CROSSER_START, _ZERO_CROSSER_STOP - _ZERO_CROSSER_START),
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
            (_DEFAULT_ROW_NAME, "debug.real_start_ts"): _ZERO_CLIPPED_START,
            (_DEFAULT_ROW_NAME, "debug.real_end_ts"): _ZERO_CLIPPED_STOP,
            (_SECOND_ROW_NAME, "debug.real_start_ts"): _ZERO_CROSSER_START,
            (_SECOND_ROW_NAME, "debug.real_end_ts"): _ZERO_CROSSER_STOP,
            (_THIRD_ROW_NAME, "debug.real_start_ts"): _ZERO_INSTANT_TS,
            (_THIRD_ROW_NAME, "debug.real_end_ts"): _ZERO_INSTANT_TS,
        }


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
                    f"AND s.name = '{process_track_name(proc(pid))}'"
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
