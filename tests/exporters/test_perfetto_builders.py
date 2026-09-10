"""Tests for the pure protobuf submessage builders.

These parse emitted bytes back with the real ``perfetto`` package, which
is what pins the wire format (ADR-0001).
"""

from perfetto.protos.perfetto.trace.perfetto_trace_pb2 import (
    DebugAnnotation,
    Trace,
    TracePacket,
    TrackDescriptor,
    TrackEvent,
)

from gcmon.exporters.perfetto_builders import (
    _build_debug_annotation_bool,
    build_trace,
    build_trace_packet,
    build_track_descriptor,
    build_track_event,
)
from gcmon.exporters.perfetto_proto import TrackEventType


class TestBuildTrackDescriptor:
    def test_process_descriptor(self) -> None:
        data = build_track_descriptor(uuid=100, name="Process 100", pid=100)
        descriptor = TrackDescriptor()
        descriptor.ParseFromString(data)
        assert descriptor.uuid == 100
        assert descriptor.name == "Process 100"
        assert not descriptor.HasField("thread")
        assert not descriptor.HasField("parent_uuid")
        assert not descriptor.HasField("counter")
        assert descriptor.HasField("process")
        assert descriptor.process.pid == 100
        assert descriptor.process.process_name == "Process 100"

    def test_process_descriptor_with_cmdline(self) -> None:
        data = build_track_descriptor(
            uuid=100,
            name="Process 100",
            pid=100,
            cmdline=["python", "-u", "script.py", "--arg1"],
            description="python -u script.py --arg1",
        )
        descriptor = TrackDescriptor()
        descriptor.ParseFromString(data)
        assert descriptor.description == "python -u script.py --arg1"
        assert descriptor.HasField("process")
        assert descriptor.process.pid == 100
        assert descriptor.process.process_name == "Process 100"
        assert len(descriptor.process.cmdline) == 4
        assert descriptor.process.cmdline[0] == "python"
        assert descriptor.process.cmdline[1] == "-u"
        assert descriptor.process.cmdline[2] == "script.py"
        assert descriptor.process.cmdline[3] == "--arg1"

    def test_process_descriptor_no_cmdline_when_none(self) -> None:
        data = build_track_descriptor(uuid=100, name="Process 100", pid=100)
        descriptor = TrackDescriptor()
        descriptor.ParseFromString(data)
        assert not descriptor.HasField("description")
        assert descriptor.HasField("process")
        assert len(descriptor.process.cmdline) == 0

    def test_process_descriptor_no_cmdline_when_empty(self) -> None:
        data = build_track_descriptor(uuid=100, name="Process 100", pid=100, cmdline=[])
        descriptor = TrackDescriptor()
        descriptor.ParseFromString(data)
        assert not descriptor.HasField("description")
        assert descriptor.HasField("process")
        assert len(descriptor.process.cmdline) == 0

    def test_counter_descriptor(self) -> None:
        data = build_track_descriptor(uuid=300, name="G0 collected", parent_uuid=200, is_counter=True)
        descriptor = TrackDescriptor()
        descriptor.ParseFromString(data)
        assert descriptor.uuid == 300
        assert descriptor.name == "G0 collected"
        assert descriptor.parent_uuid == 200
        assert descriptor.counter.SerializeToString() == b""

    def test_counter_descriptor_with_share_key(self) -> None:
        data = build_track_descriptor(
            uuid=300,
            name="G0 collected",
            parent_uuid=200,
            is_counter=True,
            y_axis_share_key="collected",
        )
        descriptor = TrackDescriptor()
        descriptor.ParseFromString(data)
        assert descriptor.uuid == 300
        assert descriptor.name == "G0 collected"
        assert descriptor.parent_uuid == 200
        assert descriptor.HasField("counter")
        assert descriptor.counter.y_axis_share_key == "collected"

    def test_process_descriptor_with_start_timestamp_ns(self) -> None:
        data = build_track_descriptor(
            uuid=100,
            name="Process 100",
            pid=100,
            start_timestamp_ns=1_700_000_000_123_456_789,
        )
        descriptor = TrackDescriptor()
        descriptor.ParseFromString(data)
        assert descriptor.HasField("process")
        assert descriptor.process.start_timestamp_ns == 1_700_000_000_123_456_789

    def test_process_descriptor_without_start_timestamp_ns(self) -> None:
        """No start_timestamp_ns is written when the kwarg is omitted
        (default ``None``). The field must be absent from the bytes."""
        data = build_track_descriptor(uuid=100, name="Process 100", pid=100)
        descriptor = TrackDescriptor()
        descriptor.ParseFromString(data)
        assert descriptor.HasField("process")
        assert not descriptor.process.HasField("start_timestamp_ns")


class TestBuildCounterDescriptor:
    """Wire-level tests for ``build_track_descriptor``'s
    ``y_axis_share_key`` kwarg and the resulting ``CounterDescriptor``
    submessage payload at ``TrackDescriptor.counter`` (field 8)."""

    def test_y_axis_share_key_emitted_at_field_8(self) -> None:
        data = build_track_descriptor(
            uuid=300,
            name="G0 collected",
            parent_uuid=200,
            is_counter=True,
            y_axis_share_key="collected",
        )
        descriptor = TrackDescriptor()
        descriptor.ParseFromString(data)
        assert descriptor.HasField("counter")
        assert descriptor.counter.y_axis_share_key == "collected"

    def test_no_y_axis_share_key_emits_empty_submessage(self) -> None:
        data = build_track_descriptor(
            uuid=300,
            name="G0 collected",
            parent_uuid=200,
            is_counter=True,
        )
        descriptor = TrackDescriptor()
        descriptor.ParseFromString(data)
        assert descriptor.counter.SerializeToString() == b""

    def test_y_axis_share_key_ignored_for_non_counter_track(self) -> None:
        data = build_track_descriptor(
            uuid=300,
            name="Track With Key",
            parent_uuid=200,
            is_counter=False,
            y_axis_share_key="ignored",
        )
        descriptor = TrackDescriptor()
        descriptor.ParseFromString(data)
        assert not descriptor.HasField("counter")

    def test_only_share_key_field_is_set_no_other_counter_fields(self) -> None:
        data = build_track_descriptor(
            uuid=300,
            name="G0 duration",
            parent_uuid=200,
            is_counter=True,
            y_axis_share_key="duration",
        )
        descriptor = TrackDescriptor()
        descriptor.ParseFromString(data)
        assert descriptor.HasField("counter")
        assert not descriptor.counter.HasField("type")
        assert len(descriptor.counter.categories) == 0
        assert not descriptor.counter.HasField("unit")
        assert not descriptor.counter.HasField("unit_multiplier")
        assert not descriptor.counter.HasField("is_incremental")
        assert not descriptor.counter.HasField("unit_name")
        assert descriptor.counter.y_axis_share_key == "duration"

    def test_y_axis_share_key_empty_string_treated_as_none(self) -> None:
        data = build_track_descriptor(
            uuid=300,
            name="G0 collected",
            parent_uuid=200,
            is_counter=True,
            y_axis_share_key="",
        )
        descriptor = TrackDescriptor()
        descriptor.ParseFromString(data)
        assert descriptor.counter.SerializeToString() == b""


class TestBuildTracePacket:
    def test_empty_packet(self) -> None:
        data = build_trace_packet(1)
        packet = TracePacket()
        packet.ParseFromString(data)
        assert packet.trusted_packet_sequence_id == 1

    def test_with_timestamp(self) -> None:
        data = build_trace_packet(1, timestamp=1_500_000_000)
        packet = TracePacket()
        packet.ParseFromString(data)
        assert packet.trusted_packet_sequence_id == 1
        assert packet.timestamp == 1_500_000_000

    def test_with_track_event(self) -> None:
        event = b"\x08\x01"
        data = build_trace_packet(1, track_event=event)
        packet = TracePacket()
        packet.ParseFromString(data)
        assert packet.trusted_packet_sequence_id == 1
        assert packet.track_event.SerializeToString() == event

    def test_with_track_descriptor(self) -> None:
        desc = b"\x0a\x05hello"
        data = build_trace_packet(1, track_descriptor=desc)
        packet = TracePacket()
        packet.ParseFromString(data)
        assert packet.trusted_packet_sequence_id == 1
        assert packet.track_descriptor.SerializeToString() == desc

    def test_with_all_fields(self) -> None:
        event = b"\x08\x01"
        data = build_trace_packet(42, timestamp=1000, track_event=event)
        packet = TracePacket()
        packet.ParseFromString(data)
        assert packet.trusted_packet_sequence_id == 42
        assert packet.timestamp == 1000
        assert packet.track_event.SerializeToString() == event


class TestBuildTrackEvent:
    def test_slice_begin(self) -> None:
        data = build_track_event(type=TrackEventType.SLICE_BEGIN, track_uuid=100, name="test")
        track_event = TrackEvent()
        track_event.ParseFromString(data)
        assert track_event.type == TrackEvent.Type.TYPE_SLICE_BEGIN
        assert track_event.track_uuid == 100
        assert track_event.name == "test"

    def test_slice_end(self) -> None:
        data = build_track_event(type=TrackEventType.SLICE_END, track_uuid=100)
        track_event = TrackEvent()
        track_event.ParseFromString(data)
        assert track_event.type == TrackEvent.Type.TYPE_SLICE_END
        assert track_event.track_uuid == 100
        assert not track_event.HasField("name")

    def test_instant(self) -> None:
        data = build_track_event(type=TrackEventType.INSTANT, track_uuid=100, name="marker")
        track_event = TrackEvent()
        track_event.ParseFromString(data)
        assert track_event.type == TrackEvent.Type.TYPE_INSTANT
        assert track_event.track_uuid == 100
        assert track_event.name == "marker"

    def test_counter(self) -> None:
        data = build_track_event(type=TrackEventType.COUNTER, track_uuid=100, counter_value=42)
        track_event = TrackEvent()
        track_event.ParseFromString(data)
        assert track_event.type == TrackEvent.Type.TYPE_COUNTER
        assert track_event.track_uuid == 100
        assert track_event.counter_value == 42

    def test_with_categories(self) -> None:
        data = build_track_event(
            type=TrackEventType.SLICE_BEGIN,
            track_uuid=100,
            name="test",
            categories=["cat1", "cat2"],
        )
        track_event = TrackEvent()
        track_event.ParseFromString(data)
        assert len(track_event.categories) == 2
        assert track_event.categories[0] == "cat1"
        assert track_event.categories[1] == "cat2"

    def test_with_debug_annotations(self) -> None:
        ann1 = b"\x52\x03key\x20\x2a"
        ann2 = b"\x52\x05other\x20\x64"
        data = build_track_event(
            type=TrackEventType.SLICE_BEGIN,
            track_uuid=100,
            name="test",
            debug_annotations=[ann1, ann2],
        )
        track_event = TrackEvent()
        track_event.ParseFromString(data)
        assert len(track_event.debug_annotations) == 2
        assert track_event.debug_annotations[0].name == "key"
        assert track_event.debug_annotations[0].int_value == 42
        assert track_event.debug_annotations[1].name == "other"
        assert track_event.debug_annotations[1].int_value == 100


class TestBuildTrace:
    def test_empty_trace(self) -> None:
        data = build_trace([])
        assert data == b""

    def test_single_packet(self) -> None:
        packet = b"\x40\x01"
        data = build_trace([packet])
        trace = Trace()
        trace.ParseFromString(data)
        assert len(trace.packet) == 1
        assert trace.packet[0].SerializeToString() == packet

    def test_multiple_packets(self) -> None:
        p1 = b"\x40\x01"
        p2 = b"\x40\x02"
        data = build_trace([p1, p2])
        trace = Trace()
        trace.ParseFromString(data)
        assert len(trace.packet) == 2
        assert trace.packet[0].SerializeToString() == p1
        assert trace.packet[1].SerializeToString() == p2


class TestBuildDebugAnnotationBool:
    """``bool_value`` rather than ``int_value``, so the UI and SQL both
    read ``true`` where an int annotation would read ``1``."""

    def _parse(self, value: bool) -> DebugAnnotation:
        annotation = DebugAnnotation()
        annotation.ParseFromString(_build_debug_annotation_bool("clipped", value))
        return annotation

    def test_true(self) -> None:
        annotation = self._parse(True)
        assert annotation.name == "clipped"
        assert annotation.bool_value is True

    def test_false_is_written_rather_than_omitted(self) -> None:
        """A consumer reads the value, never the presence of the field."""
        annotation = self._parse(False)
        assert annotation.WhichOneof("value") == "bool_value"
        assert annotation.bool_value is False
