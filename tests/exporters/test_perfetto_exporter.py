"""Tests for Perfetto binary protobuf exporter."""

import threading
from collections.abc import Set as AbstractSet
from pathlib import Path

from perfetto.protos.perfetto.trace.perfetto_trace_pb2 import (
    TracePacket,
    TrackEvent,
)

from gcmon.control.protocol import START_EVENT, STOP_EVENT
from gcmon.exporters import PerfettoExporter
from gcmon.exporters.perfetto_format import (
    TrackEventType,
)
from gcmon.exporters.perfetto_process_lifetime import _PROCESS_ROW_PREFIX, process_track_name
from gcmon.model.data import GCStatsInfo
from gcmon.model.names import (
    CLEAR_WEAKREFS,
    DEDUCE_UNREACHABLE,
    DELETE_GARBAGE,
    FILL_INCREMENT,
    FINALIZE_GARBAGE,
    GC_PAUSE_NAME,
    HANDLE_RESURRECTED,
    HANDLE_WEAKREFS,
    MARK_ALIVE,
    gc_pause_slice_name,
    phase_slice_name,
)
from gcmon.model.process import Process
from tests.conftest import DEFAULT_PID
from tests.data_helpers import create_instant_msg
from tests.exporters.conftest import ExporterFactory
from tests.exporters.perfetto_helpers import pause_item
from tests.helpers import create_mock_incremental_item, create_mock_stats_item, perfetto_packets, proc

# Pids that only ever show up as liveness observations: gcmon polled
# them successfully but they never collected, so they produce no events.
_QUIET_PID: int = 24680
_OTHER_QUIET_PID: int = 13579


def _read_trace_packets(path: Path) -> list[TracePacket]:
    return perfetto_packets(path.read_bytes())


def _get_track_event(packet: TracePacket) -> TrackEvent | None:
    if packet.HasField("track_event"):
        return packet.track_event
    return None


def _is_track_event(packet: TracePacket, event_type: int) -> bool:
    track_event = _get_track_event(packet)
    if track_event is not None:
        return bool(track_event.type == event_type)
    return False


def _count_event_type(packet_fields: list[TracePacket], event_type: int) -> int:
    count = 0
    for pf in packet_fields:
        track_event = _get_track_event(pf)
        if track_event is not None and track_event.type == event_type:
            count += 1
    return count


def _count_descriptors(packet_fields: list[TracePacket]) -> int:
    return sum(1 for pf in packet_fields if pf.HasField("track_descriptor"))


class TestPerfettoExporter:
    def test_init(self, perfetto_exporter: ExporterFactory) -> None:
        exporter, path = perfetto_exporter()
        assert isinstance(exporter, PerfettoExporter)
        assert exporter._flush_threshold == 100
        assert exporter._buffer == []
        assert exporter._output_path == path

    def test_init_with_flush_threshold(self, perfetto_exporter: ExporterFactory) -> None:
        exporter, path = perfetto_exporter(threshold=500)
        assert isinstance(exporter, PerfettoExporter)
        assert exporter._flush_threshold == 500
        assert exporter._buffer == []
        assert exporter._output_path == path

    def _verify_event_structure(self, path: Path, num_items: int) -> None:
        packets = _read_trace_packets(path)
        assert len(packets) > 0

        slice_begins = _count_event_type(packets, TrackEventType.SLICE_BEGIN)
        slice_ends = _count_event_type(packets, TrackEventType.SLICE_END)
        counters = _count_event_type(packets, TrackEventType.COUNTER)

        assert slice_begins >= num_items
        assert slice_ends >= num_items
        assert counters >= num_items * 4

    def test_flushes_at_threshold(self, mock_stats_item: GCStatsInfo, perfetto_exporter: ExporterFactory) -> None:
        """The file is there before ``close``, so the batch went out on its own."""
        exporter, path = perfetto_exporter(threshold=10)

        for _ in range(10):
            exporter.add_event(proc(DEFAULT_PID), mock_stats_item)

        assert path.exists()

    def test_every_batch_reaches_the_trace(
        self, mock_stats_item: GCStatsInfo, perfetto_exporter: ExporterFactory
    ) -> None:
        exporter, path = perfetto_exporter(threshold=5)
        for _ in range(15):
            exporter.add_event(proc(DEFAULT_PID), mock_stats_item)

        exporter.close()

        self._verify_event_structure(path, 15)

    def test_close_writes_file(self, mock_stats_item: GCStatsInfo, perfetto_exporter: ExporterFactory) -> None:
        exporter, path = perfetto_exporter()
        exporter.add_event(proc(DEFAULT_PID), mock_stats_item)
        exporter.close()
        assert path.exists()
        assert path.stat().st_size > 0

        packets = _read_trace_packets(path)
        assert len(packets) > 0

        # Verify pause slice
        hit = False
        for packet in packets:
            track_event = _get_track_event(packet)
            if track_event and track_event.type == TrackEvent.Type.TYPE_SLICE_BEGIN:
                name = track_event.name
                if name == gc_pause_slice_name(0):
                    hit = True
                    break
        assert hit, f"{gc_pause_slice_name(0)} not found"

        # Verify descriptors present
        assert _count_descriptors(packets) >= 2

    def test_close_writes_all_events(self, mock_stats_item: GCStatsInfo, perfetto_exporter: ExporterFactory) -> None:
        exporter, path = perfetto_exporter(threshold=5)
        for _ in range(15):
            exporter.add_event(proc(DEFAULT_PID), mock_stats_item)
        exporter.close()
        self._verify_event_structure(path, 15)

    def test_timestamp_conversion(self, mock_stats_item: GCStatsInfo, perfetto_exporter: ExporterFactory) -> None:
        exporter, path = perfetto_exporter()
        exporter.add_event(proc(DEFAULT_PID), mock_stats_item)
        exporter.close()

        packets = _read_trace_packets(path)
        pause_ts = None
        for packet in packets:
            track_event = _get_track_event(packet)
            if track_event and track_event.type == TrackEvent.Type.TYPE_SLICE_BEGIN:
                pause_ts = packet.timestamp
                break
        assert pause_ts == 1_500_000_000

    def test_multiple_close_calls(self, mock_stats_item: GCStatsInfo, perfetto_exporter: ExporterFactory) -> None:
        exporter, path = perfetto_exporter()
        exporter.add_event(proc(DEFAULT_PID), mock_stats_item)
        exporter.close()
        exporter.close()

        packets = _read_trace_packets(path)
        # 1 GC pause slice begin + 1 Processes-track lifetime begin
        # + 1 Lifetime bar on the pid's own row.
        assert _count_event_type(packets, TrackEventType.SLICE_BEGIN) == 3

    def test_different_generation_events(self, perfetto_exporter: ExporterFactory) -> None:
        exporter, path = perfetto_exporter()
        for gen in range(3):
            item = create_mock_stats_item(gen=gen)
            exporter.add_event(proc(DEFAULT_PID), item)
        exporter.close()

        packets = _read_trace_packets(path)
        names = set()
        for packet in packets:
            track_event = _get_track_event(packet)
            if track_event and track_event.type == TrackEvent.Type.TYPE_SLICE_BEGIN:
                name = track_event.name
                if name and GC_PAUSE_NAME in name:
                    names.add(name)
        assert names == {gc_pause_slice_name(0), gc_pause_slice_name(1), gc_pause_slice_name(2)}

    def test_add_instant_event_writes_instant_event(self, perfetto_exporter: ExporterFactory) -> None:
        exporter, path = perfetto_exporter()
        instant = create_instant_msg(name=START_EVENT, ts=1_500_000_000)
        exporter.add_instant_event(proc(DEFAULT_PID), instant)
        exporter.close()

        packets = _read_trace_packets(path)
        names: list[str] = []
        for packet in packets:
            track_event = _get_track_event(packet)
            if track_event and track_event.type == TrackEvent.Type.TYPE_INSTANT:
                name = track_event.name
                if name:
                    names.append(name)
        # Only what the caller sent: the process row is kept rendered by
        # the "Lifetime" slice, which is not an instant.
        assert names == [START_EVENT]

    def test_multiple_add_instant_event(self, perfetto_exporter: ExporterFactory) -> None:
        exporter, path = perfetto_exporter()
        for ev_name in (START_EVENT, STOP_EVENT):
            exporter.add_instant_event(proc(DEFAULT_PID), create_instant_msg(name=ev_name, ts=1_500_000_000))
        exporter.close()

        packets = _read_trace_packets(path)
        names: list[str] = []
        for packet in packets:
            track_event = _get_track_event(packet)
            if track_event and track_event.type == TrackEvent.Type.TYPE_INSTANT:
                event_name = track_event.name
                if event_name:
                    names.append(event_name)
        assert names == [START_EVENT, STOP_EVENT]

    def test_events_have_valid_timestamps(
        self, mock_stats_item: GCStatsInfo, perfetto_exporter: ExporterFactory
    ) -> None:
        exporter, path = perfetto_exporter()
        exporter.add_event(proc(DEFAULT_PID), mock_stats_item)
        exporter.close()

        packets = _read_trace_packets(path)
        for packet in packets:
            ts = packet.timestamp
            if ts:
                assert ts >= 1_500_000

    def test_close_with_no_events(self, perfetto_exporter: ExporterFactory) -> None:
        exporter, path = perfetto_exporter()
        exporter.close()
        assert not path.exists() or path.stat().st_size == 0

    def test_descriptors_written_before_events(self, perfetto_exporter: ExporterFactory) -> None:
        exporter, path = perfetto_exporter()
        exporter.add_event(proc(DEFAULT_PID), create_mock_stats_item())
        exporter.close()

        packets = _read_trace_packets(path)
        assert packets[0].HasField("track_descriptor")

    def test_multiple_processes(self, perfetto_exporter: ExporterFactory) -> None:
        exporter, path = perfetto_exporter()
        item = create_mock_stats_item()
        exporter.add_event(proc(100), item)
        exporter.add_event(proc(200), item)
        exporter.close()

        packets = _read_trace_packets(path)
        descriptors = sum(1 for p in packets if p.HasField("track_descriptor"))
        assert descriptors >= 4

    def test_incremental_item_emits_subphases(self, perfetto_exporter: ExporterFactory) -> None:
        exporter, path = perfetto_exporter()
        item = create_mock_incremental_item()
        exporter.add_event(proc(DEFAULT_PID), item)
        exporter.close()

        packets = _read_trace_packets(path)
        begin_names = set()
        for packet in packets:
            track_event = _get_track_event(packet)
            if track_event and track_event.type == TrackEvent.Type.TYPE_SLICE_BEGIN:
                name = track_event.name
                if name:
                    begin_names.add(name)
        expected = {
            gc_pause_slice_name(0),
            phase_slice_name(MARK_ALIVE, 0),
            phase_slice_name(FILL_INCREMENT, 0),
            phase_slice_name(DEDUCE_UNREACHABLE, 0),
            phase_slice_name(HANDLE_WEAKREFS, 0),
            phase_slice_name(FINALIZE_GARBAGE, 0),
            phase_slice_name(HANDLE_RESURRECTED, 0),
            phase_slice_name(CLEAR_WEAKREFS, 0),
            phase_slice_name(DELETE_GARBAGE, 0),
        }
        assert expected.issubset(begin_names)

    def test_counter_events_per_metric(self, perfetto_exporter: ExporterFactory) -> None:
        exporter, path = perfetto_exporter()
        exporter.add_event(proc(DEFAULT_PID), create_mock_stats_item())
        exporter.close()

        packets = _read_trace_packets(path)
        counter_tracks = set()
        for packet in packets:
            track_event = _get_track_event(packet)
            if track_event and track_event.type == TrackEvent.Type.TYPE_COUNTER:
                uuid = track_event.track_uuid
                if uuid:
                    counter_tracks.add(uuid)
        # collected, uncollectable, candidates, heap_size, duration.
        assert len(counter_tracks) == 5

    def test_cmdline_comes_from_the_process(self, tmp_path: Path) -> None:
        exporter = PerfettoExporter(output_path=tmp_path / "test.pb")
        item = pause_item()
        exporter.add_process_cmdline(proc(DEFAULT_PID), ("python", "-u", "my_script.py"))
        exporter.add_event(proc(DEFAULT_PID), item)
        exporter.close()

        trace_data = (tmp_path / "test.pb").read_bytes()
        assert len(trace_data) > 0

        packets = _read_trace_packets(tmp_path / "test.pb")
        found_cmdline = False
        found_description = False
        for packet in packets:
            if packet.HasField("track_descriptor"):
                td = packet.track_descriptor
                if td.description == "python -u my_script.py":
                    found_description = True
                if td.HasField("process"):
                    descriptor = td.process
                    if descriptor.cmdline:
                        assert descriptor.cmdline[0] == "python"
                        assert descriptor.cmdline[1] == "-u"
                        assert descriptor.cmdline[2] == "my_script.py"
                        found_cmdline = True
        assert found_cmdline, "cmdline not found in trace"
        assert found_description, "track description should be set when cmdline is present"

    def test_a_process_with_no_cmdline_gets_no_description(self, tmp_path: Path) -> None:
        exporter = PerfettoExporter(output_path=tmp_path / "test.pb")
        item = pause_item()
        exporter.add_event(proc(DEFAULT_PID), item)
        exporter.close()

        trace_data = (tmp_path / "test.pb").read_bytes()
        assert len(trace_data) > 0

        packets = _read_trace_packets(tmp_path / "test.pb")
        for packet in packets:
            if packet.HasField("track_descriptor"):
                td = packet.track_descriptor
                assert not td.HasField("description"), "description should be absent when the process has no cmdline"
                if td.HasField("process"):
                    descriptor = td.process
                    assert len(descriptor.cmdline) == 0, "cmdline should be absent when the process has none"

    def test_slice_begin_end_matched(self, perfetto_exporter: ExporterFactory) -> None:
        exporter, path = perfetto_exporter()
        for _ in range(5):
            exporter.add_event(proc(DEFAULT_PID), create_mock_stats_item())
        exporter.close()

        packets = _read_trace_packets(path)
        begins = _count_event_type(packets, TrackEventType.SLICE_BEGIN)
        ends = _count_event_type(packets, TrackEventType.SLICE_END)
        assert begins == ends


class TestRssRoundTrip:
    def test_add_rss_sample_emits_counter_event(self, perfetto_exporter: ExporterFactory) -> None:
        exporter, path = perfetto_exporter()
        exporter.add_rss_sample(proc(100), 4096, 1_000_000)
        exporter.close()

        packets = _read_trace_packets(path)
        counter_packets: list[tuple[int, int, int]] = []
        for packet in packets:
            if not packet.HasField("track_event"):
                continue
            track_event = packet.track_event
            if track_event.type != TrackEvent.Type.TYPE_COUNTER:
                continue
            counter_packets.append((track_event.track_uuid, track_event.counter_value, packet.timestamp))

        assert len(counter_packets) == 1, f"expected 1 counter event, got {len(counter_packets)}"
        _, value, ts = counter_packets[0]
        assert value == 4096
        assert ts == 1_000_000

    def test_rss_uses_separate_counter_track(self, perfetto_exporter: ExporterFactory) -> None:
        """RSS counter uses a different track UUID than GC counters."""
        exporter, path = perfetto_exporter()
        exporter.add_event(proc(DEFAULT_PID), create_mock_stats_item())
        exporter.add_rss_sample(proc(DEFAULT_PID), 4096, 2_000_000)
        exporter.close()

        packets = _read_trace_packets(path)
        counter_uuids: set[int] = set()
        for packet in packets:
            if not packet.HasField("track_event"):
                continue
            track_event = packet.track_event
            if track_event.type != TrackEvent.Type.TYPE_COUNTER:
                continue
            counter_uuids.add(track_event.track_uuid)

        # At least: collected, uncollectable, candidates, heap_size,
        # duration (5 GC counters) + 1 RSS counter = 6 distinct UUIDs.
        assert len(counter_uuids) >= 6


def _lifetime_spans(path: Path) -> dict[str, tuple[int, int]]:
    """Return ``{slice name: (begin ts, end ts)}`` for the ``Processes``
    track, read back off disk.

    The track is the only one carrying named BEGIN/END pairs, and the
    encoder emits each pair adjacently, so matching by name is enough.
    """
    begins: dict[str, int] = {}
    spans: dict[str, tuple[int, int]] = {}
    for packet in _read_trace_packets(path):
        if not packet.HasField("track_event"):
            continue
        track_event = packet.track_event
        if not track_event.name.startswith(_PROCESS_ROW_PREFIX):
            continue
        if track_event.type == TrackEventType.SLICE_BEGIN:
            begins[track_event.name] = packet.timestamp
        elif track_event.type == TrackEventType.SLICE_END:
            spans[track_event.name] = (begins[track_event.name], packet.timestamp)
    return spans


class TestProcessLivenessRoundTrip:
    """``add_process_liveness`` folds into the same span accumulator GC
    events feed, so a ``Processes`` slice covers what gcmon observed
    rather than what it saw collect. See ADR-0011."""

    def test_liveness_only_pid_gets_a_slice(self, perfetto_exporter: ExporterFactory) -> None:
        """A pid gcmon polled OK for a run that never collected produces
        no events, so under the old rule it reached no track at all."""
        exporter, path = perfetto_exporter()
        exporter.add_event(proc(DEFAULT_PID), create_mock_stats_item())
        for ts in (1_400_000_000, 1_600_000_000, 1_800_000_000):
            exporter.add_process_liveness({proc(_QUIET_PID)}, ts)
        exporter.close()

        assert _lifetime_spans(path)[process_track_name(proc(_QUIET_PID))] == (1_400_000_000, 1_800_000_000)

    def test_liveness_widens_an_event_derived_span(self, perfetto_exporter: ExporterFactory) -> None:
        """Liveness folds in alongside events rather than replacing
        them: ``get_gc_stats`` returns collections that already
        happened, so a freshly discovered child's first event can carry
        a timestamp from before gcmon ever saw it."""
        exporter, path = perfetto_exporter()
        exporter.add_event(proc(DEFAULT_PID), create_mock_stats_item(ts_start=1_500_000_000, ts_stop=1_505_000_000))
        exporter.add_process_liveness({proc(DEFAULT_PID)}, 1_900_000_000)
        exporter.close()

        # The event start is still the start -- it precedes the first
        # observation -- and the last observation is now the end.
        assert _lifetime_spans(path)[process_track_name(proc(DEFAULT_PID))] == (1_500_000_000, 1_900_000_000)

    def test_whole_live_set_lands_in_one_call(self, perfetto_exporter: ExporterFactory) -> None:
        exporter, path = perfetto_exporter()
        exporter.add_event(proc(DEFAULT_PID), create_mock_stats_item())
        exporter.add_process_liveness({proc(_QUIET_PID), proc(_OTHER_QUIET_PID)}, 1_400_000_000)
        exporter.add_process_liveness({proc(_QUIET_PID), proc(_OTHER_QUIET_PID)}, 1_800_000_000)
        exporter.close()

        spans = _lifetime_spans(path)
        assert spans[process_track_name(proc(_QUIET_PID))] == (1_400_000_000, 1_800_000_000)
        assert spans[process_track_name(proc(_OTHER_QUIET_PID))] == (1_400_000_000, 1_800_000_000)

    def test_a_trace_of_nothing_but_liveness_is_still_written(self, perfetto_exporter: ExporterFactory) -> None:
        """A run in which nothing ever collected produces no events, so
        nothing reaches the file before ``close()``. The encoder used to
        skip its closeout in that case, dropping the whole track and the
        file with it."""
        exporter, path = perfetto_exporter()
        exporter.add_process_liveness({proc(_QUIET_PID), proc(_OTHER_QUIET_PID)}, 1_400_000_000)
        exporter.add_process_liveness({proc(_QUIET_PID), proc(_OTHER_QUIET_PID)}, 1_800_000_000)
        exporter.close()

        assert path.exists(), "a trace with spans and no events must still be written"
        assert _lifetime_spans(path) == {
            process_track_name(proc(_QUIET_PID)): (1_400_000_000, 1_800_000_000),
            process_track_name(proc(_OTHER_QUIET_PID)): (1_400_000_000, 1_800_000_000),
        }

    def test_a_trace_of_truly_nothing_writes_no_file(self, perfetto_exporter: ExporterFactory) -> None:
        """The other side of the guard: with no events *and* no liveness
        there is nothing to finalize, so no file appears."""
        exporter, path = perfetto_exporter()
        exporter.close()
        assert not path.exists()

    def test_liveness_holds_the_io_lock_for_the_duration(self, tmp_path: Path) -> None:
        """Asserted by contention, not by inspection: a second thread
        taking ``_io_lock`` the ordinary way -- through a flush -- must
        not get in while the liveness call is inside the encoder."""
        exporter = PerfettoExporter(output_path=tmp_path / "trace.pb", flush_threshold=1)
        inside = threading.Event()
        release = threading.Event()
        real_record = exporter._encoder.record_process_liveness

        def _blocking_record(processes: AbstractSet[Process], ts_ns: int) -> None:
            inside.set()
            assert release.wait(timeout=5.0)
            real_record(processes, ts_ns)

        exporter._encoder.record_process_liveness = _blocking_record  # type: ignore[method-assign]

        flushed = threading.Event()

        def _flush() -> None:
            # flush_threshold=1, so this reaches the encoder under
            # _io_lock rather than only buffering.
            exporter.add_instant_event(proc(DEFAULT_PID), create_instant_msg(name="contend", ts=1_500_000_000))
            flushed.set()

        liveness = threading.Thread(target=exporter.add_process_liveness, args=({proc(_QUIET_PID)}, 1_400_000_000))
        writer = threading.Thread(target=_flush)
        liveness.start()
        assert inside.wait(timeout=5.0)
        writer.start()

        assert not flushed.wait(timeout=0.2), "the flush got the io lock while liveness was still holding it"
        release.set()
        liveness.join(timeout=5.0)
        writer.join(timeout=5.0)
        assert flushed.is_set(), "the flush never completed after liveness released the lock"
        assert not liveness.is_alive() and not writer.is_alive()

        exporter.close()
