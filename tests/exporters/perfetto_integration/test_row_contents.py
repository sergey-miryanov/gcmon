"""What a row carries once it is drawn.

The slice arguments, the counter tracks and the axes they share, the track
descriptors, the cmdline encoding and the RSS samples.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from perfetto.trace_processor import TraceProcessor

from gcmon.exporters import PerfettoExporter
from gcmon.exporters.perfetto_format import (
    _COUNTER_GROUP_NAME,
    _INTERPRETER_LIST_NAME,
    _INTERPRETER_ROW_ORDER,
    _interpreter_group_name,
)
from gcmon.exporters.perfetto_process_lifetime import (
    _PROCESS_LIFETIME_TRACK_NAME,
    _PROCESS_ROW_PREFIX,
    process_track_name,
)
from gcmon.exporters.trace_converter import counter_display_name
from gcmon.model.names import (
    ALIVE_SIZE,
    CANDIDATES,
    CLEAR_WEAKREFS,
    CLEAR_WEAKREFS_COUNT,
    COLLECTED,
    DEDUCE_UNREACHABLE,
    DELETE_GARBAGE,
    DELETED_GARBAGE_COUNT,
    DURATION,
    FILL_INCREMENT,
    FINALIZE_GARBAGE,
    FINALIZED_GARBAGE_COUNT,
    HANDLE_RESURRECTED,
    HANDLE_WEAKREFS,
    HEAP_SIZE,
    INCREMENT_SIZE,
    MARK_ALIVE,
    RSS,
    UNCOLLECTABLE,
    gc_pause_slice_name,
    phase_slice_name,
)
from tests.conftest import DEFAULT_PID
from tests.exporters.perfetto_integration.traces import (
    _ARG_PREFIX,
    _CANDIDATES,
    _COLLECTED,
    _COLLECTIONS,
    _DEFAULT_ROW_NAME,
    _EXPECTED_COUNTER_NAMES,
    _EXPECTED_PAUSE_ARGS,
    _FAKE_CMDLINE_JOINED,
    _HEAP_SIZE,
    _INSTANT_NAME,
    _PAUSE_NAME,
    _RSS_PID_1,
    _RSS_PID_2,
    _RSS_TS_1,
    _RSS_TS_3,
    _RSS_VAL_1,
    _RSS_VAL_3,
    _SECOND_ROW_NAME,
    _TS_START,
    _TS_STOP,
    _UNCOLLECTABLE,
    _on_interpreter,
    _process_filter,
    _process_filter_instant,
)
from tests.helpers import (
    create_mock_stats_item,
    open_trace_processor,
    proc,
)


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
                f"SELECT s.name FROM slice s {_process_filter(DEFAULT_PID)} AND s.name = '{gc_pause_slice_name(1)}'"
            )
        )

        assert len(rows) == 1, f"expected exactly one '{gc_pause_slice_name(1)}' slice, got {rows}"

    def test_full_fields_pause_encodes_all_optional_fields(
        self,
        trace_processor: TraceProcessor,
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
            r.name for r in trace_processor.query(f"SELECT DISTINCT s.name FROM slice s {_process_filter(DEFAULT_PID)}")
        }

        missing = set(expected_sub_slices) - slice_names
        assert not missing, f"missing sub-slices: {missing}"

    def test_deduce_unreachable_carries_candidates_and_the_pause_the_sizes(
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
                f"  AND s.name = '{phase_slice_name(DEDUCE_UNREACHABLE, 1)}' AND s.dur > 0 "
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
                f"  AND s.name = '{gc_pause_slice_name(1)}' AND s.dur > 0 "
                f"  {_on_interpreter(1)}"
                ")"
            )
        }
        for key in (
            INCREMENT_SIZE,
            ALIVE_SIZE,
            FINALIZED_GARBAGE_COUNT,
            DELETED_GARBAGE_COUNT,
            CLEAR_WEAKREFS_COUNT,
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
            assert counter_display_name(0, UNCOLLECTABLE) not in {n.strip() for n in names}, (
                f"uncollectable counter should be omitted when 0; got {names}"
            )
            assert counter_display_name(0, COLLECTED) in {n.strip() for n in names}
            assert counter_display_name(0, CANDIDATES) in {n.strip() for n in names}

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
            assert counter_display_name(gen, DURATION) in names, (
                f"no duration counter for generation {gen}; got {names}"
            )
        assert DURATION not in names, f"shared 'duration' counter should NOT be present; got {names}"

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
                f"SELECT id, name FROM counter_track WHERE name = '{counter_display_name(0, DURATION)}'",
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
            assert parents[0].name == _COUNTER_GROUP_NAME


class TestCounterYAxisShareKey:
    """``y_axis_share_key`` as the trace processor hands it to the UI.

    ``counter_track`` has no such column. The stdlib table the UI builds its
    TrackEvent rows from does, so that is where these read it. The wire-level
    tests in ``TestCounterTrackYAxisShareKey``
    (``test_perfetto_counter_tracks.py``) hold the bytes.
    """

    def _share_keys(self, tp: TraceProcessor) -> dict[str, set[str | None]]:
        """Every key a counter track of each name carries. One name covers a
        track per interpreter, hence a set."""
        list(tp.query("INCLUDE PERFETTO MODULE viz.summary.track_event"))
        keys: dict[str, set[str | None]] = {}
        for r in tp.query("SELECT name, y_axis_share_key FROM _track_event_tracks_ordered_groups WHERE is_counter = 1"):
            keys.setdefault(r.name, set()).add(r.y_axis_share_key)
        return keys

    def test_y_axis_share_key_shared_across_generations(
        self,
        trace_processor: TraceProcessor,
    ) -> None:
        """``G0 collected`` and ``G1 collected`` share one axis, keyed by the
        metric alone, and so does every other per-generation counter."""
        keys = self._share_keys(trace_processor)

        assert {name: keys[name] for name in keys if name != HEAP_SIZE} == {
            counter_display_name(gen, metric): {metric}
            for gen in (0, 1)
            for metric in (COLLECTED, UNCOLLECTABLE, CANDIDATES, DURATION)
        }

    def test_heap_size_y_axis_share_key_is_null(
        self,
        trace_processor: TraceProcessor,
    ) -> None:
        """A heap is sized in objects and shares an axis with nothing."""
        assert self._share_keys(trace_processor)[HEAP_SIZE] == {None}


def rows_drawn_under(tp: TraceProcessor, *path: str) -> list[str]:
    """The rows under the row *path* names, top to bottom. No path is the top
    level.

    `order_id` is the key the Perfetto UI sorts a TrackEvent row's children
    on, and the trace processor computes it: by `sibling_order_rank` under a
    parent whose `child_ordering` is explicit, by name under one with none.
    """
    list(tp.query("INCLUDE PERFETTO MODULE viz.summary.track_event"))
    rows = list(
        tp.query("SELECT min_track_id AS id, parent_id, name, order_id FROM _track_event_tracks_ordered_groups")
    )
    parent: int | None = None
    for name in path:
        [parent] = [r.id for r in rows if r.parent_id == parent and r.name == name]
    return [r.name for r in sorted((r for r in rows if r.parent_id == parent), key=lambda r: r.order_id)]


class TestTheOrderRowsAreDrawnIn:
    """Read off the trace processor, which is where the UI gets it. Neither
    order below is the alphabetical one, so a rank that stopped reaching the
    wire would show as rows sorted by name."""

    def test_an_interpreter_s_rows_follow_their_ranks(self, every_row_trace_processor: TraceProcessor) -> None:
        drawn = rows_drawn_under(every_row_trace_processor, _INTERPRETER_LIST_NAME, _interpreter_group_name(0))

        assert drawn == list(_INTERPRETER_ROW_ORDER)

    def test_the_counters_in_a_group_follow_their_ranks(self, every_row_trace_processor: TraceProcessor) -> None:
        drawn = rows_drawn_under(
            every_row_trace_processor, _INTERPRETER_LIST_NAME, _interpreter_group_name(0), _COUNTER_GROUP_NAME
        )

        assert drawn == [counter_display_name(0, metric) for metric in (COLLECTED, UNCOLLECTABLE, CANDIDATES, DURATION)]

    def test_the_top_level_rows_come_out_by_name(self, every_row_trace_processor: TraceProcessor) -> None:
        """No rank reaches these: a process track is OS-scoped and the trace
        processor discards one there (ADR-0003). The name is all that places
        the interpreter list under the process row (ADR-0027)."""
        drawn = rows_drawn_under(every_row_trace_processor)

        assert drawn == [_DEFAULT_ROW_NAME, _PROCESS_LIFETIME_TRACK_NAME, _INTERPRETER_LIST_NAME, RSS]


class TestTrackDescriptors:
    """The Perfetto exporter emits a process track descriptor with the
    expected ``Process <pid>`` name, and no thread descriptor at all.
    (Chrome JSON does not produce a separate process track; the test is
    therefore Perfetto-only.)"""

    def test_process_track_present(self, trace_processor: TraceProcessor) -> None:
        rows = sorted(
            r.name for r in trace_processor.query(f"SELECT name FROM track WHERE name LIKE '{_PROCESS_ROW_PREFIX}%'")
        )

        assert rows == sorted([_DEFAULT_ROW_NAME, _SECOND_ROW_NAME]), (
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
                f"WHERE p.name LIKE '{_PROCESS_ROW_PREFIX}%' ORDER BY p.name, th.tid"
            )
        )

        assert [(r.pname, r.tname, r.tid, r.ismain, r.untracked) for r in rows] == [
            (_DEFAULT_ROW_NAME, "", 1, 1, 1),
            (_SECOND_ROW_NAME, "", 2, 1, 1),
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
        assert self._description(trace_processor_with_cmdline, _DEFAULT_ROW_NAME) == _FAKE_CMDLINE_JOINED
        assert self._description(trace_processor_with_cmdline, _SECOND_ROW_NAME) == _FAKE_CMDLINE_JOINED

    def test_a_process_with_no_cmdline_gets_no_description(
        self,
        trace_processor: TraceProcessor,
    ) -> None:
        assert self._description(trace_processor, _DEFAULT_ROW_NAME) is None
        assert self._description(trace_processor, _SECOND_ROW_NAME) is None


class TestRssCounterTrackIntegration:
    """Integration tests verifying RSS counter tracks are populated in
    Perfetto traces and queryable through the trace processor."""

    @pytest.mark.parametrize(("expected_ts", "expected_val"), [(_RSS_TS_1, _RSS_VAL_1), (_RSS_TS_3, _RSS_VAL_3)])
    def test_rss_counter_values_match(
        self,
        trace_processor_with_rss: TraceProcessor,
        expected_ts: int,
        expected_val: int,
    ) -> None:
        """Values written via ``add_rss_sample`` must appear in the
        ``counter`` table with the correct timestamp and value."""
        rows = list(
            trace_processor_with_rss.query(
                f"SELECT c.value, c.ts FROM counter c "
                f"JOIN counter_track ct ON c.track_id = ct.id "
                f"WHERE ct.name = '{RSS}' AND c.ts = {expected_ts}"
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
        gc_metrics_rows = list(
            trace_processor_with_rss.query(f"SELECT name FROM track WHERE name = '{_COUNTER_GROUP_NAME}'")
        )

        assert not gc_metrics_rows, f"GC Metrics track should NOT appear in an RSS-only trace; got {gc_metrics_rows}"

    def test_rss_counter_tracks_per_pid(
        self,
        trace_processor_with_rss: TraceProcessor,
    ) -> None:
        """Each PID gets its own RSS counter track. Verify by counting
        distinct RSS counter track ids and total counter values."""
        # Two distinct RSS counter tracks (one per PID).
        rss_track_ids = list(
            trace_processor_with_rss.query(f"SELECT DISTINCT id FROM counter_track WHERE name = '{RSS}' ORDER BY id")
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
                f"WHERE ct.name = '{RSS}'"
            )
        )
        assert total_values[0].cnt == 3, (
            f"expected 3 RSS counter values total (2 for PID 1, 1 for PID 2), got {total_values[0].cnt}"
        )

        # Both expected PIDs appear in the process table.
        for pid in (_RSS_PID_1, _RSS_PID_2):
            proc_rows = list(
                trace_processor_with_rss.query(
                    f"SELECT name FROM process WHERE name = '{process_track_name(proc(pid))}'"
                )
            )
            assert len(proc_rows) == 1, f"expected process row for PID {pid}"

    def test_rss_counter_track_carries_no_unit(
        self,
        trace_processor_with_rss: TraceProcessor,
    ) -> None:
        """The unit column comes back as ``None`` or ``''``: gcmon sets no
        explicit unit."""
        rows = list(trace_processor_with_rss.query(f"SELECT unit FROM counter_track WHERE name = '{RSS}'"))

        assert rows
        assert {r.unit for r in rows} <= {None, ""}

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
            for expected in (counter_display_name(0, COLLECTED), counter_display_name(0, CANDIDATES), HEAP_SIZE):
                assert expected in counter_tracks, (
                    f"GC counter track {expected!r} missing after adding RSS; got {sorted(counter_tracks)}"
                )
            assert RSS in counter_tracks, (
                f"RSS counter track missing after adding RSS + GC events; got {sorted(counter_tracks)}"
            )


class TestTwoInterpretersHeapSizes:
    """Two interpreters in one process draw two `heap_size` rows.

    Both are named `heap_size`. What keeps them apart is the group each
    parents to (ADR-0027): sharing a name *and* a parent is what the trace
    processor merges, and the qualified name ADR-0024 wrote stood in for the
    parent before there was one.
    """

    @pytest.fixture(scope="class")
    @classmethod
    def two_interpreters(cls, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TraceProcessor]:
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
                f"WHERE ct.name = '{HEAP_SIZE}' ORDER BY ig.name"
            )
        )

        assert [r.iname for r in rows] == [_interpreter_group_name(0), _interpreter_group_name(1)]
        assert len({r.id for r in rows}) == 2, f"the two rows merged: {[dict(r.__dict__) for r in rows]}"

    def test_a_query_matching_the_bare_name_finds_it(self, two_interpreters: TraceProcessor) -> None:
        """What ADR-0024's qualifier broke and ADR-0027 gives back."""
        names = {r.name.strip() for r in two_interpreters.query("SELECT name FROM counter_track")}

        assert HEAP_SIZE in names

    def test_a_query_can_select_one_heap(self, two_interpreters: TraceProcessor) -> None:
        """One hop up the parent chain, which is what names the interpreter
        now that the track name does not."""
        rows = list(
            two_interpreters.query(
                "SELECT c.value AS value FROM counter c "
                "JOIN counter_track ct ON c.track_id = ct.id "
                "JOIN track ig ON ct.parent_id = ig.id "
                f"WHERE ct.name = '{HEAP_SIZE}' AND ig.name = '{_interpreter_group_name(1)}'"
            )
        )

        assert [r.value for r in rows] == [9_000]

    def test_each_row_parents_to_its_own_interpreter_group(self, two_interpreters: TraceProcessor) -> None:
        parents = [
            r.parent_id
            for r in two_interpreters.query(f"SELECT parent_id FROM counter_track WHERE name = '{HEAP_SIZE}'")
        ]

        assert len(parents) == 2
        assert len(set(parents)) == 2, f"both heap rows share a parent: {parents}"

    def test_rss_stays_bare(self, two_interpreters: TraceProcessor) -> None:
        """Its owner is the process, and a process holds one."""
        names = {r.name.strip() for r in two_interpreters.query("SELECT name FROM counter_track")}

        assert RSS in names

    def test_every_other_counter_name_is_what_it_was(self, two_interpreters: TraceProcessor) -> None:
        names = {r.name.strip() for r in two_interpreters.query("SELECT name FROM counter_track")}

        assert names == {
            counter_display_name(0, COLLECTED),
            counter_display_name(0, UNCOLLECTABLE),
            counter_display_name(0, CANDIDATES),
            counter_display_name(0, DURATION),
            HEAP_SIZE,
            RSS,
        }
