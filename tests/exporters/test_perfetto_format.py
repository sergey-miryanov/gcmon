"""Tests for converting trace events into Perfetto packets."""

from perfetto.protos.perfetto.trace.perfetto_trace_pb2 import (
    DebugAnnotation,
    TracePacket,
    TrackDescriptor,
    TrackEvent,
)

from gcmon.control.protocol import START_EVENT, STOP_EVENT
from gcmon.exporters.perfetto_format import (
    _COUNTER_GROUP_NAME,
    _COUNTER_RANKS,
    _INTERPRETER_GROUP_PREFIX,
    _INTERPRETER_LIST_NAME,
    _LOSS_TRACK_NAME,
    _PAUSE_TRACK_NAME,
    _UNLISTED_COUNTER_RANK,
    _interpreter_group_name,
    convert_trace_events_to_perfetto,
)
from gcmon.exporters.perfetto_process_lifetime import (
    _PROCESS_ROW_SLICE_NAME,
    finalize_perfetto_packets,
    process_track_name,
)
from gcmon.exporters.perfetto_proto import TrackEventType
from gcmon.exporters.perfetto_track_state import PerfettoTrackState
from gcmon.exporters.trace_converter import (
    convert_item_to_trace_format,
    convert_loss_to_trace_format,
    counter_display_name,
)
from gcmon.model.data import GCStatsInfo, LossMsg
from gcmon.model.names import (
    CANDIDATES,
    CLEAR_WEAKREFS,
    CLEAR_WEAKREFS_COUNT,
    CLIPPED,
    COLLECTED,
    COLLECTIONS,
    DEDUCE_UNREACHABLE,
    DELETE_GARBAGE,
    DELETED_GARBAGE_COUNT,
    DURATION,
    FILL_INCREMENT,
    FINALIZE_GARBAGE,
    FINALIZED_GARBAGE_COUNT,
    GC_LOSS_NAME,
    GENERATION,
    HANDLE_RESURRECTED,
    HANDLE_WEAKREFS,
    HEAP_SIZE,
    IID,
    LOST_COUNT,
    LOST_PAUSE,
    LOST_PAUSE_NS,
    MARK_ALIVE,
    OBSERVED_COUNT,
    PID,
    PID_EPOCH,
    REAL_END_TS,
    REAL_START_TS,
    RSS,
    UNCOLLECTABLE,
    gc_pause_slice_name,
    phase_slice_name,
)
from gcmon.model.trace_event import (
    Counter,
    Instant,
    TraceEvent,
    Track,
)
from tests.exporters.perfetto_helpers import (
    convert_item,
    convert_items,
    lifetime_slices,
    parse_track_descriptor,
    pause_item,
)
from tests.helpers import create_mock_loss_item, interpreter_track, proc, process_track

TARGET_PID: int = 100

# The ``Processes`` row a pid draws. A descriptor carries it and an
# assertion reads it back, so both go through one spelling.
TARGET_ROW_NAME: str = process_track_name(proc(TARGET_PID))


def full_subphase_item() -> GCStatsInfo:
    """A pause with every sub-phase set, each one 100ns long.

    The converter draws one slice per sub-phase off these, so a test that
    reads a sub-phase slice or its annotations starts here.
    """
    return pause_item(
        gen=1,
        ts_start=3_000,
        ts_stop=4_000,
        heap_size=2048,
        collections=10,
        collected=100,
        uncollectable=1,
        candidates=20,
        duration=0.01,
        increment_size=500,
        alive_size=300,
        ts_mark_alive_start=3_000,
        ts_mark_alive_stop=3_100,
        ts_fill_increment_start=3_100,
        ts_fill_increment_stop=3_200,
        ts_deduce_unreachable_start=3_200,
        ts_deduce_unreachable_stop=3_300,
        ts_handle_weakref_callbacks_start=3_300,
        ts_handle_weakref_callbacks_stop=3_400,
        ts_finalize_garbage_stop=3_500,
        finalized_garbage_count=42,
        ts_handle_resurrected_stop=3_600,
        ts_clear_weakrefs_stop=3_700,
        clear_weakrefs_count=7,
        ts_delete_garbage_start=3_800,
        ts_delete_garbage_stop=3_900,
        deleted_garbage_count=13,
    )


def pause_spans(packets: list[bytes]) -> list[tuple[int, int]]:
    """Where each gen-0 pause in *packets* begins and ends.

    An END names nothing, so it is matched to its BEGIN by the track they
    share, which carries nothing but pauses.
    """
    parsed = [TracePacket.FromString(raw) for raw in packets]
    begins = [p for p in parsed if p.track_event.name == gc_pause_slice_name(0)]
    rows = {p.track_event.track_uuid for p in begins}
    ends = [
        p for p in parsed if p.track_event.type == TrackEvent.Type.TYPE_SLICE_END and p.track_event.track_uuid in rows
    ]
    return [(begin.timestamp, end.timestamp) for begin, end in zip(begins, ends, strict=True)]


def counters_written(descriptors: list[bytes], packets: list[bytes]) -> list[str]:
    """The name of every counter track *packets* put a value on, sorted."""
    described = [td for td in map(parse_track_descriptor, descriptors) if td is not None]
    names = {td.uuid: td.name for td in described if td.HasField("counter")}
    events = [TracePacket.FromString(raw).track_event for raw in packets]
    return sorted({names[e.track_uuid] for e in events if e.type == TrackEvent.Type.TYPE_COUNTER})


class TestConvertItemToPerfettoPackets:
    def test_cmdline_emitted_once_per_process(self, state: PerfettoTrackState) -> None:
        target = proc(TARGET_PID)
        state.set_cmdline(target, ("python", "script.py"))
        item = pause_item()

        desc1, _ = convert_item(target, item, state, sequence_id=1)

        found_cmdline = False
        found_description = False
        for desc_bytes in desc1:
            packet = TracePacket()
            packet.ParseFromString(desc_bytes)
            if packet.HasField("track_descriptor"):
                td = packet.track_descriptor
                if td.description == "python script.py":
                    found_description = True
                if td.HasField("process") and len(td.process.cmdline) > 0:
                    assert len(td.process.cmdline) == 2
                    assert td.process.cmdline[0] == "python"
                    assert td.process.cmdline[1] == "script.py"
                    found_cmdline = True
        assert found_cmdline
        assert found_description, "description should be set when cmdline is present"

        desc2, _ = convert_item(
            target,
            pause_item(
                gen=1,
                ts_start=3_000,
                ts_stop=4_000,
                heap_size=2000,
                collections=2,
                collected=20,
                candidates=10,
                duration=0.002,
            ),
            state,
            sequence_id=1,
        )

        for desc_bytes in desc2:
            packet = TracePacket()
            packet.ParseFromString(desc_bytes)
            if packet.HasField("track_descriptor"):
                assert not packet.track_descriptor.HasField("process")

    def test_two_processes_on_one_pid_carry_their_own_cmdlines(self, state: PerfettoTrackState) -> None:
        """``ProcessDescriptor.cmdline`` reaches no SQL column
        (ADR-0010), so the descriptor is where a successor's command
        line can be checked at all. Before the split it had nowhere to
        go: one descriptor went out for the pid, carrying the first
        process's program.
        """
        first, second = proc(TARGET_PID, 1), proc(TARGET_PID, 2)
        state.set_cmdline(first, ("python", "first.py"))
        state.set_cmdline(second, ("python", "second.py"))

        cmdlines: dict[str, tuple[str, ...]] = {}
        for process, ts in ((first, 1_000), (second, 3_000)):
            descriptors, _ = convert_item(
                process,
                pause_item(ts_start=ts, ts_stop=ts + 1_000),
                state,
                sequence_id=1,
            )
            for desc_bytes in descriptors:
                td = parse_track_descriptor(desc_bytes)
                if td is not None and td.HasField("process"):
                    cmdlines[td.name] = tuple(td.process.cmdline)

        assert cmdlines == {
            TARGET_ROW_NAME: ("python", "first.py"),
            process_track_name(proc(TARGET_PID, 2)): ("python", "second.py"),
        }

    def test_pause_track_has_sibling_order_rank_zero(self, state: PerfettoTrackState) -> None:
        item = pause_item()

        descriptors, _ = convert_item(proc(TARGET_PID), item, state, sequence_id=1)

        interpreter_uuid = state.get_or_create_interpreter_group_track_uuid(proc(TARGET_PID), 0)
        pause_uuid = state.get_track_uuid(interpreter_track(TARGET_PID, 0))
        pause_found = False
        for desc_bytes in descriptors:
            packet = TracePacket()
            packet.ParseFromString(desc_bytes)
            if packet.HasField("track_descriptor"):
                td = packet.track_descriptor
                if td.uuid == pause_uuid:
                    assert td.parent_uuid == interpreter_uuid
                    assert td.HasField("sibling_order_rank"), "an unset rank reads as 0 too"
                    assert td.sibling_order_rank == 0
                    assert not td.HasField("child_ordering")
                    assert not td.HasField("thread")
                    pause_found = True
        assert pause_found

    def test_counter_tracks_parented_to_counter_group(self, state: PerfettoTrackState) -> None:
        item = pause_item()

        descriptors, _ = convert_item(proc(TARGET_PID), item, state, sequence_id=1)

        proc_uuid = state.get_process_track_uuid(proc(TARGET_PID))
        interpreter_uuid = state.get_or_create_interpreter_group_track_uuid(proc(TARGET_PID), 0)
        group_uuid = state.get_or_create_counter_group_track_uuid(interpreter_track(TARGET_PID, 0))
        assert group_uuid != proc_uuid
        group_seen = False
        per_metric_parent: dict[str, int] = {}
        for desc_bytes in descriptors:
            packet = TracePacket()
            packet.ParseFromString(desc_bytes)
            if packet.HasField("track_descriptor"):
                td = packet.track_descriptor
                uuid = td.uuid
                if td.HasField("counter"):
                    per_metric_parent[td.name] = td.parent_uuid
                elif uuid == group_uuid:
                    group_seen = True
                    assert td.parent_uuid == interpreter_uuid
                    assert td.child_ordering == 3
        assert group_seen, "GC Counters group track descriptor was not emitted"
        # heap_size is a top-level counter: parented to the interpreter's own
        # group, beside `GC Metrics` rather than inside it.
        assert per_metric_parent[HEAP_SIZE] == interpreter_uuid
        assert proc_uuid not in per_metric_parent.values()
        # Per-gen counters are parented to the GC Counters group.
        for name, parent_uuid in per_metric_parent.items():
            if name != HEAP_SIZE:
                assert parent_uuid == group_uuid, f"{name!r} should parent to group"

    def test_basic_item_emits_pause_slice(self, state: PerfettoTrackState) -> None:
        item = pause_item()

        _, packets = convert_item(proc(TARGET_PID), item, state, sequence_id=1)

        # The GC pause slice begin is not the first packet: the "Process
        # 100" slice begin on the shared "Processes" track precedes it.
        # Find the GC pause slice by name to disambiguate.
        assert len(packets) >= 2
        lifetime_uuid = state.get_or_create_process_lifetime_track_uuid()

        begin_packet = None
        for p in packets:
            packet = TracePacket()
            packet.ParseFromString(p)
            if (
                packet.track_event.type == TrackEvent.Type.TYPE_SLICE_BEGIN
                and packet.track_event.track_uuid != lifetime_uuid
                and packet.track_event.name == gc_pause_slice_name(0)
            ):
                begin_packet = p
                break
        assert begin_packet is not None
        first_packet = TracePacket()
        first_packet.ParseFromString(begin_packet)
        assert first_packet.timestamp == 1_000
        assert first_packet.HasField("track_event")
        assert first_packet.track_event.type == TrackEvent.Type.TYPE_SLICE_BEGIN
        assert first_packet.track_event.name == gc_pause_slice_name(0)

    def test_basic_item_emits_counter_events(self, state: PerfettoTrackState) -> None:
        item = pause_item(uncollectable=2)

        descriptors, packets = convert_item(proc(TARGET_PID), item, state, sequence_id=1)

        described = [td for td in map(parse_track_descriptor, descriptors) if td is not None]
        names = {td.uuid: td.name for td in described if td.HasField("counter")}
        events = [TracePacket.FromString(raw).track_event for raw in packets]
        written = {
            names[e.track_uuid]: (field, getattr(e, field))
            for e in events
            if (field := e.WhichOneof("counter_value_field")) is not None
        }
        # `duration` alone is a double (DOUBLE_COUNTER_VALUE, field 44).
        assert written == {
            counter_display_name(0, COLLECTED): ("counter_value", 10),
            counter_display_name(0, UNCOLLECTABLE): ("counter_value", 2),
            counter_display_name(0, CANDIDATES): ("counter_value", 5),
            counter_display_name(0, DURATION): ("double_counter_value", 0.001),
            HEAP_SIZE: ("counter_value", 1000),
        }

    def test_counter_descriptor_emitted_once(self, state: PerfettoTrackState) -> None:
        item = pause_item()

        desc1, _ = convert_item(proc(TARGET_PID), item, state, sequence_id=1)
        desc2, _ = convert_item(proc(TARGET_PID), item, state, sequence_id=1)

        assert len(desc1) > 0
        assert len(desc2) == 0

    def test_invalid_timestamps_produces_events(self, state: PerfettoTrackState) -> None:
        """The pause goes out as the record gives it. Nothing reorders an
        inverted one or drops it."""
        item = pause_item(ts_start=2_000, ts_stop=1_000)

        _descriptors, packets = convert_item(proc(TARGET_PID), item, state, sequence_id=1)

        assert pause_spans(packets) == [(2_000, 1_000)]

    def test_equal_timestamps_produces_events(self, state: PerfettoTrackState) -> None:
        item = pause_item(ts_stop=1_000, duration=0.0)

        _descriptors, packets = convert_item(proc(TARGET_PID), item, state, sequence_id=1)

        assert pause_spans(packets) == [(1_000, 1_000)]

    def test_incremental_item_emits_subphases(self, state: PerfettoTrackState) -> None:
        item = full_subphase_item()

        _, packets = convert_item(proc(TARGET_PID), item, state, sequence_id=1)

        slice_begins: list[str | None] = []
        for p in packets:
            packet = TracePacket()
            packet.ParseFromString(p)
            if packet.HasField("track_event") and packet.track_event.type == TrackEvent.Type.TYPE_SLICE_BEGIN:
                slice_begins.append(packet.track_event.name or None)
        assert gc_pause_slice_name(1) in slice_begins
        assert phase_slice_name(MARK_ALIVE, 1) in slice_begins
        assert phase_slice_name(FILL_INCREMENT, 1) in slice_begins
        assert phase_slice_name(DEDUCE_UNREACHABLE, 1) in slice_begins
        assert phase_slice_name(HANDLE_WEAKREFS, 1) in slice_begins
        assert phase_slice_name(FINALIZE_GARBAGE, 1) in slice_begins
        assert phase_slice_name(HANDLE_RESURRECTED, 1) in slice_begins
        assert phase_slice_name(CLEAR_WEAKREFS, 1) in slice_begins
        assert phase_slice_name(DELETE_GARBAGE, 1) in slice_begins

    def test_uncollectable_counter_omitted_when_zero(self, state: PerfettoTrackState) -> None:
        item = pause_item(collections=5, candidates=3)

        descriptors, packets = convert_item(proc(TARGET_PID), item, state, sequence_id=1)

        assert counters_written(descriptors, packets) == sorted(
            [HEAP_SIZE, *(counter_display_name(0, metric) for metric in (COLLECTED, CANDIDATES, DURATION))]
        )

    def test_uncollectable_counter_emitted_when_nonzero(self, state: PerfettoTrackState) -> None:
        item = pause_item(collections=5, uncollectable=2, candidates=3)

        descriptors, packets = convert_item(proc(TARGET_PID), item, state, sequence_id=1)

        assert counters_written(descriptors, packets) == sorted(
            [
                HEAP_SIZE,
                *(counter_display_name(0, metric) for metric in (COLLECTED, CANDIDATES, DURATION, UNCOLLECTABLE)),
            ]
        )

    def test_duration_counter_in_gc_metrics_group(self, state: PerfettoTrackState) -> None:
        item = pause_item(collections=5, uncollectable=2, candidates=3, duration=0.42)

        descriptors_packets, packets = convert_item(proc(TARGET_PID), item, state, sequence_id=1)

        # Find the per-gen `G0 duration` counter track UUID. The duration is
        # now split by generation (one `G{gen} duration` track per (pid, iid))
        # so a shared `duration` track is no longer emitted.
        duration_track_uuid: int | None = None
        for p in packets:
            packet = TracePacket()
            packet.ParseFromString(p)
            if not packet.HasField("track_event"):
                continue
            track_event = packet.track_event
            if track_event.type == TrackEvent.Type.TYPE_COUNTER and track_event.double_counter_value == 0.42:
                duration_track_uuid = track_event.track_uuid
                break
        assert duration_track_uuid is not None

        # Find the matching TrackDescriptor, and check it ranks where
        # `_COUNTER_ORDER` puts `duration` under a parent named "GC Metrics".
        descriptors: dict[int, tuple[int, int, str]] = {}
        for p in descriptors_packets:
            packet = TracePacket()
            packet.ParseFromString(p)
            if not packet.HasField("track_descriptor"):
                continue
            td = packet.track_descriptor
            descriptors[td.uuid] = (
                td.parent_uuid,
                td.sibling_order_rank,
                td.name,
            )
        assert duration_track_uuid in descriptors
        parent, rank, _ = descriptors[duration_track_uuid]
        assert rank == _COUNTER_RANKS[DURATION]
        assert parent != 0
        assert descriptors[parent][2] == _COUNTER_GROUP_NAME

    def _annotations_for_slice(
        self,
        packets: list[bytes],
        slice_name: str,
    ) -> list[tuple[str | None, int | None]]:
        for p in packets:
            packet = TracePacket()
            packet.ParseFromString(p)
            if not packet.HasField("track_event"):
                continue
            track_event = packet.track_event
            if track_event.type != TrackEvent.Type.TYPE_SLICE_BEGIN:
                continue
            if track_event.name != slice_name:
                continue
            out: list[tuple[str | None, int | None]] = []
            for ann in track_event.debug_annotations:
                out.append(
                    (
                        ann.name or None,
                        ann.int_value if ann.HasField("int_value") else None,
                    )
                )
            return out
        raise AssertionError(f"slice {slice_name!r} not found in packets")

    def test_finalize_garbage_substep_has_count_annotation(self, state: PerfettoTrackState) -> None:
        _, packets = convert_item(proc(TARGET_PID), full_subphase_item(), state, sequence_id=1)

        anns = self._annotations_for_slice(packets, phase_slice_name(FINALIZE_GARBAGE, 1))
        assert (FINALIZED_GARBAGE_COUNT, 42) in anns
        assert all(name not in (DELETED_GARBAGE_COUNT, CLEAR_WEAKREFS_COUNT) for name, _ in anns)

    def test_clear_weakrefs_substep_has_count_annotation(self, state: PerfettoTrackState) -> None:
        _, packets = convert_item(proc(TARGET_PID), full_subphase_item(), state, sequence_id=1)

        anns = self._annotations_for_slice(packets, phase_slice_name(CLEAR_WEAKREFS, 1))
        assert (CLEAR_WEAKREFS_COUNT, 7) in anns
        assert all(name not in (FINALIZED_GARBAGE_COUNT, DELETED_GARBAGE_COUNT) for name, _ in anns)

    def test_delete_garbage_substep_has_count_annotation(self, state: PerfettoTrackState) -> None:
        _, packets = convert_item(proc(TARGET_PID), full_subphase_item(), state, sequence_id=1)

        anns = self._annotations_for_slice(packets, phase_slice_name(DELETE_GARBAGE, 1))
        assert (DELETED_GARBAGE_COUNT, 13) in anns
        assert all(name not in (FINALIZED_GARBAGE_COUNT, CLEAR_WEAKREFS_COUNT) for name, _ in anns)

    def test_deduce_unreachable_substep_has_candidates_annotation(self, state: PerfettoTrackState) -> None:
        item = full_subphase_item()

        _, packets = convert_item(proc(TARGET_PID), item, state, sequence_id=1)

        anns = self._annotations_for_slice(packets, phase_slice_name(DEDUCE_UNREACHABLE, 1))
        assert (CANDIDATES, item.candidates) in anns
        assert (GENERATION, 1) in anns

    def test_zero_duration_subphase_skipped(self, state: PerfettoTrackState) -> None:
        item = pause_item(
            gen=1,
            ts_start=3_000,
            ts_stop=4_000,
            heap_size=2048,
            collections=10,
            collected=100,
            uncollectable=1,
            candidates=20,
            duration=0.01,
            increment_size=500,
            alive_size=300,
            ts_mark_alive_start=3_000,
            ts_mark_alive_stop=3_000,
            ts_fill_increment_start=3_100,
            ts_fill_increment_stop=3_200,
        )

        _, packets = convert_item(proc(TARGET_PID), item, state, sequence_id=1)

        slice_names: list[str | None] = []
        for p in packets:
            packet = TracePacket()
            packet.ParseFromString(p)
            if packet.HasField("track_event") and packet.track_event.type == TrackEvent.Type.TYPE_SLICE_BEGIN:
                slice_names.append(packet.track_event.name or None)
        assert phase_slice_name(MARK_ALIVE, 1) not in slice_names
        assert phase_slice_name(FILL_INCREMENT, 1) in slice_names

    def test_debug_annotation_name_wire_format(self, state: PerfettoTrackState) -> None:
        item = pause_item(collections=5, uncollectable=2, candidates=3)

        _, packets = convert_item(proc(TARGET_PID), item, state, sequence_id=1)

        # The process_track_name(proc(100)) slice begin on the shared "Processes" track
        # precedes the GC pause slice begin. Identify the GC pause slice
        # by its name.
        lifetime_uuid = state.get_or_create_process_lifetime_track_uuid()
        begin_packet = None
        for p in packets:
            packet = TracePacket()
            packet.ParseFromString(p)
            if (
                packet.track_event.type == TrackEvent.Type.TYPE_SLICE_BEGIN
                and packet.track_event.track_uuid != lifetime_uuid
                and packet.track_event.name == gc_pause_slice_name(0)
            ):
                begin_packet = packet
                break
        first_packet = begin_packet
        assert first_packet is not None
        anns = first_packet.track_event.debug_annotations
        assert len(anns) == 7
        for ann in anns:
            assert not ann.HasField("name_iid"), (
                "field 1 of DebugAnnotation is `name_iid` (uint64); the annotation name must not be written there"
            )
            assert ann.HasField("name")

    def test_debug_annotations_on_pause(self, state: PerfettoTrackState) -> None:
        item = pause_item(collections=5, uncollectable=2, candidates=3)

        _, packets = convert_item(proc(TARGET_PID), item, state, sequence_id=1)

        # Disambiguate by name (and exclude the spec-15 "Processes" track
        # slice begin) to find the GC pause slice.
        lifetime_uuid = state.get_or_create_process_lifetime_track_uuid()
        begin_packet = None
        for p in packets:
            packet = TracePacket()
            packet.ParseFromString(p)
            if (
                packet.track_event.type == TrackEvent.Type.TYPE_SLICE_BEGIN
                and packet.track_event.track_uuid != lifetime_uuid
                and packet.track_event.name == gc_pause_slice_name(0)
            ):
                begin_packet = p
                break
        assert begin_packet is not None
        first_packet = TracePacket()
        first_packet.ParseFromString(begin_packet)
        anns = first_packet.track_event.debug_annotations
        assert len(anns) == 7
        ann_values: list[tuple[str | None, int | None]] = []
        for ann in anns:
            name = ann.name or None
            val = ann.int_value if ann.HasField("int_value") else None
            ann_values.append((name, val))
        assert (GENERATION, 0) in ann_values
        assert (IID, 0) in ann_values
        assert (COLLECTIONS, 5) in ann_values
        assert (HEAP_SIZE, 1000) in ann_values
        assert (COLLECTED, 10) in ann_values
        assert (UNCOLLECTABLE, 2) in ann_values
        assert (CANDIDATES, 3) in ann_values


class TestConvertInstantToPerfettoPacket:
    def test_emits_process_descriptor(self, state: PerfettoTrackState) -> None:
        events: list[TraceEvent] = [
            Instant(process_track(TARGET_PID), START_EVENT, ts=5_000),
        ]

        descriptors, _ = convert_trace_events_to_perfetto(events, state, sequence_id=1)

        # 1 root descriptor + 1 process descriptor. The "Processes" track
        # descriptor is emitted at closeout, not here.
        assert len(descriptors) == 2
        assert state.has_process_descriptor(proc(TARGET_PID))

    def test_emits_instant_event(self, state: PerfettoTrackState) -> None:
        events: list[TraceEvent] = [
            Instant(process_track(TARGET_PID), START_EVENT, ts=5_000),
        ]

        _, packets = convert_trace_events_to_perfetto(events, state, sequence_id=1)

        # One packet from the convert call: the user-provided instant on
        # the process track. This pid's whole observed span is a single
        # ts, so both its slices are zero-length -- and both are still
        # drawn, so finalize adds the track descriptor plus the
        # "Processes" pair and the "Lifetime" pair, all at ts 5_000.
        packets.extend(finalize_perfetto_packets(state, sequence_id=1))
        assert len(packets) == 6
        names: list[str | None] = []
        for p in packets:
            packet = TracePacket()
            packet.ParseFromString(p)
            if packet.HasField("track_event"):
                names.append(packet.track_event.name or None)
        assert names == [
            START_EVENT,
            TARGET_ROW_NAME,
            TARGET_ROW_NAME,
            _PROCESS_ROW_SLICE_NAME,
            None,
        ]
        instant_packet = None
        for p in packets:
            packet = TracePacket()
            packet.ParseFromString(p)
            if packet.track_event.name == START_EVENT:
                instant_packet = packet
                break
        assert instant_packet is not None
        assert instant_packet.timestamp == 5_000
        assert instant_packet.track_event.type == TrackEvent.Type.TYPE_INSTANT
        assert instant_packet.track_event.name == START_EVENT

    def test_reuses_process_descriptor(self, state: PerfettoTrackState) -> None:
        desc1, packets1 = convert_trace_events_to_perfetto(
            [Instant(process_track(TARGET_PID), START_EVENT, ts=5_000)],
            state,
            sequence_id=1,
        )
        desc2, packets2 = convert_trace_events_to_perfetto(
            [Instant(process_track(TARGET_PID), STOP_EVENT, ts=10_000)],
            state,
            sequence_id=1,
        )

        # First call: 2 descriptors (root + process) + 1 packet from the
        # convert (the instant). Second call: 0 descriptors (all are
        # idempotent) + 1 packet (the new instant event). Both pairs --
        # the "Processes" one behind its descriptor and the "Lifetime"
        # one on the pid's own row -- come from the single finalize,
        # spanning both calls' timestamps.
        assert len(desc1) == 2
        assert len(packets1) == 1
        assert len(desc2) == 0
        assert len(packets2) == 1

        closeout = finalize_perfetto_packets(state, sequence_id=1)
        assert len(closeout) == 5
        lifetime_uuid = state.get_or_create_process_lifetime_track_uuid()
        assert lifetime_slices(closeout, lifetime_uuid) == [
            (
                5_000,
                TrackEventType.SLICE_BEGIN,
                TARGET_ROW_NAME,
                {PID: 100, PID_EPOCH: 1, REAL_START_TS: 5_000, REAL_END_TS: 10_000, CLIPPED: False},
            ),
            (10_000, TrackEventType.SLICE_END, TARGET_ROW_NAME, {}),
        ]

    def test_instant_after_gc_event_no_duplicate_descriptor(self, state: PerfettoTrackState) -> None:
        gc_item = pause_item()
        gc_desc, _ = convert_item(proc(TARGET_PID), gc_item, state, sequence_id=1)

        inst_desc, _ = convert_trace_events_to_perfetto(
            [Instant(process_track(TARGET_PID), STOP_EVENT, ts=5_000)],
            state,
            sequence_id=1,
        )

        assert len(gc_desc) >= 2
        assert len(inst_desc) == 0

    def test_counter_track_takes_the_display_name_it_was_given(self, state: PerfettoTrackState) -> None:
        descriptors, _ = convert_trace_events_to_perfetto(
            [
                Counter(
                    interpreter_track(TARGET_PID, 0),
                    metric=HEAP_SIZE,
                    display_name=HEAP_SIZE,
                    ts=1_000,
                    value=1234,
                ),
            ],
            state,
            sequence_id=1,
        )

        track_names: list[str] = []
        for d in descriptors:
            packet = TracePacket()
            packet.ParseFromString(d)
            if packet.HasField("track_descriptor"):
                td = packet.track_descriptor
                if td.HasField("counter") and td.name:
                    track_names.append(td.name)
        assert HEAP_SIZE in track_names
        assert "heap_size heap_size" not in track_names

    def test_shared_heap_size_track_reused_across_generations(self, state: PerfettoTrackState) -> None:
        """Read off the wire: asking the state for the track twice returns one
        uuid whatever the two conversions did."""
        items = [
            (proc(TARGET_PID), pause_item(heap_size=1000)),
            (proc(TARGET_PID), pause_item(gen=1, ts_start=3_000, ts_stop=4_000, heap_size=2000)),
        ]

        descriptors, packets, _closeout = convert_items(items, state, sequence_id=1)

        described = [td for td in map(parse_track_descriptor, descriptors) if td is not None]
        [heap_size] = [td.uuid for td in described if td.name == HEAP_SIZE]
        events = [TracePacket.FromString(raw).track_event for raw in packets]
        assert [event.counter_value for event in events if event.track_uuid == heap_size] == [1000, 2000]


class TestAnInstantCanCarryArgs:
    """An `Instant` carries debug annotations the way a `Slice` does.

    Nothing fills the field yet: a `TInstantMsg` is a type, a name and a `ts`,
    so neither the live path nor `combine` has anything to put there. The
    mark grammar is what will, and it arrives with the caller that needs it.
    Reached by constructing an `Instant` directly, which is the only way to
    reach it and what the encoder's other unit tests already do.
    """

    def _instant_packet(self, event: Instant) -> TrackEvent:
        state = PerfettoTrackState()
        _, packets = convert_trace_events_to_perfetto([event], state, sequence_id=1)
        for p in packets:
            packet = TracePacket()
            packet.ParseFromString(p)
            if packet.track_event.type == TrackEvent.Type.TYPE_INSTANT and packet.track_event.name == event.name:
                return packet.track_event
        raise AssertionError(f"no instant packet named {event.name!r}")

    def test_the_args_reach_the_packet_as_debug_annotations(self) -> None:
        track_event = self._instant_packet(
            Instant(process_track(TARGET_PID), "benchmark", 1_000, {"benchmark": "json_loads", "loops": 3})
        )

        annotations = {
            a.name: (a.string_value or None, a.int_value if a.HasField("int_value") else None)
            for a in track_event.debug_annotations
        }
        assert annotations == {"benchmark": ("json_loads", None), "loops": (None, 3)}

    def test_an_instant_with_no_args_writes_no_annotations_field(self) -> None:
        """The bytes an instant produces today, so the field costs a trace
        that does not use it nothing."""
        bare = self._instant_packet(Instant(process_track(TARGET_PID), "mark", 1_000))

        assert len(bare.debug_annotations) == 0
        assert (
            bare.SerializeToString()
            == self._instant_packet(Instant(process_track(TARGET_PID), "mark", 1_000, {})).SerializeToString()
        )

    def test_a_nested_group_flattens_the_way_it_does_on_a_slice(self) -> None:
        track_event = self._instant_packet(
            Instant(process_track(TARGET_PID), "benchmark", 1_000, {"timing": {"warmup": 5, "unit": "ms"}})
        )

        (group,) = track_event.debug_annotations
        assert group.name == "timing"
        assert {
            e.name: (e.string_value or None, e.int_value if e.HasField("int_value") else None)
            for e in group.dict_entries
        } == {
            "warmup": (None, 5),
            "unit": ("ms", None),
        }


class TestATrackIsDescribedOffTheEventsOnIt:
    """A track's descriptor goes out because an event named that track.

    No producer sends metadata first, so none can forget to: the descriptors
    a batch needs are derived from the tracks its events name, ahead of the
    packets that name them.
    """

    def _pid_events(self, pid: int, iid: int = 0) -> list[TraceEvent]:
        item = pause_item(iid=iid)
        return convert_item_to_trace_format(proc(pid), item)

    def _named(self, descriptors: list[bytes]) -> list[str]:
        parsed = [parse_track_descriptor(d) for d in descriptors]
        return [td.name for td in parsed if td is not None and td.name]

    def test_a_gc_record_describes_the_process_and_the_interpreter(self, state: PerfettoTrackState) -> None:
        descriptors, _ = convert_trace_events_to_perfetto(self._pid_events(100), state, sequence_id=1)

        names = self._named(descriptors)
        assert TARGET_ROW_NAME in names
        assert _PAUSE_TRACK_NAME in names
        assert (
            names.index(TARGET_ROW_NAME) < names.index(_interpreter_group_name(0)) < names.index(_PAUSE_TRACK_NAME)
        ), f"each parent must precede its child; got {names}"

    def test_an_rss_only_pid_gets_a_process_row_and_no_thread_row(self, state: PerfettoTrackState) -> None:
        descriptors, _ = convert_trace_events_to_perfetto(
            [Counter(process_track(TARGET_PID), RSS, RSS, 1_000, 4096)],
            state,
            sequence_id=1,
        )

        assert TARGET_ROW_NAME in self._named(descriptors)
        parsed = [parse_track_descriptor(d) for d in descriptors]
        assert not any(td.HasField("thread") for td in parsed if td is not None)

    def test_a_mark_only_pid_gets_a_process_row_and_no_thread_row(self, state: PerfettoTrackState) -> None:
        descriptors, _ = convert_trace_events_to_perfetto(
            [Instant(process_track(TARGET_PID), "mark", ts=1_000)],
            state,
            sequence_id=1,
        )

        assert TARGET_ROW_NAME in self._named(descriptors)
        parsed = [parse_track_descriptor(d) for d in descriptors]
        assert not any(td.HasField("thread") for td in parsed if td is not None)

    def test_every_track_is_described_exactly_once_across_batches(self, state: PerfettoTrackState) -> None:
        first, _ = convert_trace_events_to_perfetto(self._pid_events(100), state, sequence_id=1)
        second, _ = convert_trace_events_to_perfetto(self._pid_events(100), state, sequence_id=1)

        assert TARGET_ROW_NAME in self._named(first)
        assert _PAUSE_TRACK_NAME in self._named(first)
        assert self._named(second) == []

    def test_a_second_interpreter_is_described_when_it_first_collects(self, state: PerfettoTrackState) -> None:
        """Its own row and its own counters, in a batch the process was
        already described in."""
        convert_trace_events_to_perfetto(self._pid_events(100, iid=0), state, sequence_id=1)

        later, _ = convert_trace_events_to_perfetto(self._pid_events(100, iid=1), state, sequence_id=1)

        names = self._named(later)
        assert names[:2] == [_interpreter_group_name(1), _PAUSE_TRACK_NAME], (
            f"the group it parents to comes first; got {names}"
        )
        assert TARGET_ROW_NAME not in names


class TestTheInterpreterGroupsAreDerived:
    """The two grouping rows that hold every interpreter's rows (ADR-0027).

    Read off the wire rather than through the trace processor: a group
    holding no events and no described children is not a row the trace
    processor builds, so until something moves inside them these
    descriptors are the only place they exist.
    """

    def _events(self, pid: int, iid: int = 0) -> list[TraceEvent]:
        item = pause_item(iid=iid)
        return convert_item_to_trace_format(proc(pid), item)

    def _by_name(self, descriptors: list[bytes]) -> dict[str, TrackDescriptor]:
        parsed = [parse_track_descriptor(d) for d in descriptors]
        return {td.name: td for td in parsed if td is not None and td.name}

    def test_a_record_derives_the_list_and_the_group_that_holds_it(self, state: PerfettoTrackState) -> None:
        descriptors, _ = convert_trace_events_to_perfetto(self._events(100), state, sequence_id=1)

        described = self._by_name(descriptors)

        listing = described[_INTERPRETER_LIST_NAME]
        assert listing.parent_uuid == state.get_process_track_uuid(proc(TARGET_PID))
        assert listing.child_ordering == 3
        # No rank: the process track is OS-scoped and discards one (ADR-0003).
        assert not listing.HasField("sibling_order_rank")

        group = described[_interpreter_group_name(0)]
        assert group.parent_uuid == listing.uuid
        assert group.child_ordering == 3
        assert group.HasField("sibling_order_rank"), "an unset rank reads as 0 too"
        assert group.sibling_order_rank == 0

    def test_the_list_precedes_the_group_it_holds(self, state: PerfettoTrackState) -> None:
        descriptors, _ = convert_trace_events_to_perfetto(self._events(100), state, sequence_id=1)

        names = [td.name for td in (parse_track_descriptor(d) for d in descriptors) if td is not None and td.name]
        assert (
            names.index(TARGET_ROW_NAME) < names.index(_INTERPRETER_LIST_NAME) < names.index(_interpreter_group_name(0))
        ), f"each parent must precede its child; got {names}"

    def test_the_list_s_name_sorts_after_the_process_row_s(self, state: PerfettoTrackState) -> None:
        """A rename has to keep the list under the row it belongs to.

        No rank reaches a process's top-level rows, since the process track
        is OS-scoped (ADR-0003), and the trace processor orders them by name,
        capitals first. The name is the whole of it (ADR-0027).
        """
        descriptors, _ = convert_trace_events_to_perfetto(self._events(100), state, sequence_id=1)

        parsed = [td for td in (parse_track_descriptor(d) for d in descriptors) if td is not None]
        process_uuid = state.get_process_track_uuid(proc(TARGET_PID))
        process_name = next(td.name for td in parsed if td.uuid == process_uuid)
        listing_name = next(td.name for td in parsed if td.parent_uuid == process_uuid)
        assert process_name < listing_name, f"{listing_name!r} draws above {process_name!r} in the UI"

    def test_a_second_interpreter_shares_the_list_and_gets_its_own_group(self, state: PerfettoTrackState) -> None:
        convert_trace_events_to_perfetto(self._events(100, iid=0), state, sequence_id=1)

        later, _ = convert_trace_events_to_perfetto(self._events(100, iid=1), state, sequence_id=1)

        described = self._by_name(later)

        assert _INTERPRETER_LIST_NAME not in described, "the list is described once per process"
        group = described[_interpreter_group_name(1)]
        assert group.parent_uuid == state.get_or_create_interpreter_list_track_uuid(proc(TARGET_PID))
        assert group.sibling_order_rank == 1

    def test_a_loss_row_reaches_the_same_group_as_the_collections(self, state: PerfettoTrackState) -> None:
        """One group per interpreter, not one per row it owns."""
        convert_trace_events_to_perfetto(self._events(100, iid=0), state, sequence_id=1)

        later, _ = convert_trace_events_to_perfetto(
            convert_loss_to_trace_format(proc(TARGET_PID), create_mock_loss_item(iid=0)),
            state,
            sequence_id=1,
        )

        described = self._by_name(later)
        assert _LOSS_TRACK_NAME in described, "the loss row still describes itself"
        assert _interpreter_group_name(0) not in described, "interpreter 0's group is already described"
        assert _INTERPRETER_LIST_NAME not in described

    def test_a_process_that_only_reported_rss_derives_neither(self, state: PerfettoTrackState) -> None:
        """A row exists because an event named it (ADR-0024), and nothing
        an interpreter owns was named here."""
        descriptors, _ = convert_trace_events_to_perfetto(
            [Counter(process_track(TARGET_PID), RSS, RSS, 1_000, 4096)],
            state,
            sequence_id=1,
        )

        described = self._by_name(descriptors)
        assert _INTERPRETER_LIST_NAME not in described
        assert not any(name.startswith(_INTERPRETER_GROUP_PREFIX) for name in described)


class TestTheRowsInsideAnInterpreterGroupAreRanked:
    """The rows a group holds draw in the order ADR-0027 sets.

    The order is spelled out below rather than read off
    ``_INTERPRETER_ROW_ORDER``, which would pass whatever that tuple said.
    Reordering the rows is a decision the record carries, so it costs an
    edit here too.
    """

    def _events(self, pid: int = 100, iid: int = 0) -> list[TraceEvent]:
        """One of every row an interpreter owns: a pause slice, a loss
        span, the `heap_size` counter and a counter inside GC Metrics."""
        item = pause_item(iid=iid)
        return [
            *convert_item_to_trace_format(proc(pid), item),
            *convert_loss_to_trace_format(proc(pid), create_mock_loss_item(iid=iid)),
        ]

    def test_they_draw_pauses_loss_heap_size_then_the_metrics_group(self, state: PerfettoTrackState) -> None:
        descriptors, _ = convert_trace_events_to_perfetto(self._events(), state, sequence_id=1)

        parsed = [td for td in (parse_track_descriptor(d) for d in descriptors) if td is not None]
        group_uuid = state.get_or_create_interpreter_group_track_uuid(proc(TARGET_PID), 0)
        rows = sorted((td for td in parsed if td.parent_uuid == group_uuid), key=lambda td: td.sibling_order_rank)
        assert [td.name for td in rows] == [_PAUSE_TRACK_NAME, _LOSS_TRACK_NAME, HEAP_SIZE, _COUNTER_GROUP_NAME]
        assert [td.sibling_order_rank for td in rows] == [0, 1, 2, 3], "ranks must be distinct for the order to hold"


class TestACounterTakesItsRankFromItsMetric:
    def _counter(self, track: Track, metric: str) -> list[TrackDescriptor]:
        state = PerfettoTrackState()
        descriptors, _ = convert_trace_events_to_perfetto(
            [Counter(track, metric, f"G0 {metric}", 1_000, 42)],
            state,
            sequence_id=1,
        )
        parsed = [parse_track_descriptor(d) for d in descriptors]
        return [td for td in parsed if td is not None and td.name == f"G0 {metric}"]

    def test_a_metric_nobody_listed_draws_below_every_listed_one(self) -> None:
        """A counter gcmon grows later should appear at the bottom of the
        group rather than above the ones somebody placed."""
        found = self._counter(interpreter_track(TARGET_PID, 0), "gizmo_count")

        assert found[0].sibling_order_rank == _UNLISTED_COUNTER_RANK
        assert found[0].sibling_order_rank > max(_COUNTER_RANKS.values())

    def test_a_counter_on_the_process_track_carries_no_rank(self) -> None:
        """The process track is OS-scoped, so the trace processor throws a
        child's rank away (ADR-0003). Writing one says otherwise."""
        found = self._counter(process_track(TARGET_PID), RSS)

        assert not found[0].HasField("sibling_order_rank")


class TestLossTrackDescriptor:
    """Nothing but the slices describes the loss track.

    A ``LossTrack`` is not an ``InterpreterTrack``, so a batch holding loss
    and no collection describes only the loss row. The descriptor has to come
    off the slices themselves, or they land on a uuid nothing ever named.
    """

    def _convert(self, msgs: list[LossMsg], state: PerfettoTrackState, pid: int = 100) -> list[bytes]:
        events: list[TraceEvent] = []
        for msg in msgs:
            events.extend(convert_loss_to_trace_format(proc(pid), msg))
        descriptors, _ = convert_trace_events_to_perfetto(events, state, 1)
        return descriptors

    def _msg(self, iid: int = 0) -> LossMsg:
        return create_mock_loss_item(iid=iid, gen=0, ts_start=1_000, ts_stop=2_000, lost_count=1, lost_pause_ns=200)

    def _loss_descriptors(self, descriptors: list[bytes]) -> list[TrackDescriptor]:
        parsed = [parse_track_descriptor(d) for d in descriptors]
        return [td for td in parsed if td is not None and td.name.startswith(_LOSS_TRACK_NAME)]

    def test_the_track_is_described(self, state: PerfettoTrackState) -> None:

        found = self._loss_descriptors(self._convert([self._msg(iid=0)], state))

        assert [td.name for td in found] == [_LOSS_TRACK_NAME]

    def test_each_interpreter_gets_one_inside_its_own_group(self, state: PerfettoTrackState) -> None:
        """Two rows, identically named. What tells them apart is the group
        each parents to, which is what carries the iid (ADR-0027)."""

        found = self._loss_descriptors(self._convert([self._msg(iid=0), self._msg(iid=1)], state))

        assert [td.name for td in found] == [_LOSS_TRACK_NAME, _LOSS_TRACK_NAME]
        assert [td.parent_uuid for td in found] == [
            state.get_or_create_interpreter_group_track_uuid(proc(TARGET_PID), 0),
            state.get_or_create_interpreter_group_track_uuid(proc(TARGET_PID), 1),
        ]

    def test_it_parents_to_the_interpreter_group(self, state: PerfettoTrackState) -> None:

        found = self._loss_descriptors(self._convert([self._msg()], state))

        assert found[0].parent_uuid == state.get_or_create_interpreter_group_track_uuid(proc(TARGET_PID), 0)

    def test_it_is_a_plain_custom_track(self, state: PerfettoTrackState) -> None:
        """A ``thread`` sub-message would describe an OS thread that does not
        exist, and Perfetto ignores ordering hints on OS-scoped tracks."""

        found = self._loss_descriptors(self._convert([self._msg()], state))

        assert not found[0].HasField("thread")
        assert not found[0].HasField("process")
        assert found[0].sibling_order_rank == 1

    def test_it_is_described_once(self, state: PerfettoTrackState) -> None:

        self._convert([self._msg()], state)

        again = self._loss_descriptors(self._convert([self._msg()], state))

        assert again == []

    def test_a_gc_slice_does_not_trigger_it(self, state: PerfettoTrackState) -> None:
        item = pause_item()

        descriptors, _ = convert_item(proc(TARGET_PID), item, state)

        assert self._loss_descriptors(descriptors) == []


class TestTheMissingCollectionsAnnotation:
    """A `GC Loss` slice's args, off the wire rather than out of the dict.

    Two things nothing else can settle. ``lost_collections`` is a range and
    has to arrive as ``string_value``: written into an integer field it comes
    back as a number that is not one of the counters it names. And each
    generation's counts are a *group*, which reaches the wire as
    ``dict_entries`` on an annotation carrying no value of its own.
    """

    def _slice(self, msg: LossMsg) -> TrackEvent:
        events: list[TraceEvent] = [*convert_loss_to_trace_format(proc(TARGET_PID), msg)]
        _, packets = convert_trace_events_to_perfetto(events, PerfettoTrackState(), 1)

        for raw in packets:
            packet = TracePacket()
            packet.ParseFromString(raw)
            if packet.HasField("track_event") and packet.track_event.name.startswith(f"{GC_LOSS_NAME}("):
                return packet.track_event
        raise AssertionError("no GC Loss slice in the packets")

    def _value(self, annotation: DebugAnnotation) -> str | int:
        if annotation.HasField("string_value"):
            return str(annotation.string_value)
        return int(annotation.int_value)

    def _top(self, msg: LossMsg) -> dict[str, str | int]:
        return {a.name: self._value(a) for a in self._slice(msg).debug_annotations if not a.dict_entries}

    def _group(self, msg: LossMsg, gen: int) -> dict[str, str | int]:
        for annotation in self._slice(msg).debug_annotations:
            if annotation.name == f"gen{gen}":
                return {entry.name: self._value(entry) for entry in annotation.dict_entries}
        raise AssertionError(f"no gen{gen} group on the slice")

    def _msg(self, lost_from: int, lost_count: int) -> LossMsg:
        return create_mock_loss_item(
            iid=0,
            gen=1,
            ts_start=1_000,
            ts_stop=2_000,
            observed_count=3,
            lost_count=lost_count,
            lost_pause_ns=200,
            lost_from=lost_from,
        )

    def test_a_range_reaches_the_wire_as_a_string(self) -> None:
        assert self._group(self._msg(lost_from=413, lost_count=19), 1)["lost_collections"] == "413..431"

    def test_one_collection_reaches_it_as_its_own_number(self) -> None:
        assert self._group(self._msg(lost_from=11, lost_count=1), 1)["lost_collections"] == "11"

    def test_the_counts_beside_it_stay_integers(self) -> None:
        group = self._group(self._msg(lost_from=11, lost_count=1), 1)

        assert group[LOST_COUNT] == 1
        assert group[LOST_PAUSE_NS] == 200

    def test_the_pause_total_reaches_the_wire_both_ways(self) -> None:
        """The nanoseconds are what SQL sums; the text beside them is what a
        reader takes off the slice. Losing either one leaves the other doing a
        job it is bad at."""
        group = self._group(self._msg(lost_from=11, lost_count=1), 1)

        assert group[LOST_PAUSE] == "200ns"
        assert group[LOST_PAUSE_NS] == 200

    def test_a_group_carries_entries_and_no_value_of_its_own(self) -> None:
        """``dict_entries`` sits outside the proto's ``value`` oneof, so an
        annotation that grouped *and* carried a value would be making two
        claims in one field. The trace processor flattens the entries back out
        under the group's name, which is what ``args.debug.gen1.lost_count``
        resolves to in SQL."""
        group = next(
            a for a in self._slice(self._msg(lost_from=11, lost_count=1)).debug_annotations if a.name == "gen1"
        )

        assert not group.HasField("string_value")
        assert not group.HasField("int_value")
        assert {entry.name for entry in group.dict_entries} >= {OBSERVED_COUNT, LOST_COUNT}

    def test_the_totals_stay_at_the_top_level(self) -> None:
        """Ungrouped, so the figure a reader wants first is the one they see
        first rather than one node down."""
        top = self._top(self._msg(lost_from=11, lost_count=1))

        assert top[LOST_COUNT] == 1
        assert top["seen"] == "75.0% (3 of 4)"
