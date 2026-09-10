"""Validate ``perfetto_proto`` field numbers against the real protobuf schema.

Every other Perfetto test asserts on bytes gcmon itself wrote, so a wrong
field number would agree with itself. These read the numbers out of the
``perfetto`` package's generated descriptors instead.
"""

from perfetto.protos.perfetto.trace.perfetto_trace_pb2 import (
    CounterDescriptor,
    DebugAnnotation,
    ProcessDescriptor,
    Trace,
    TracePacket,
    TrackDescriptor,
    TrackEvent,
)

from gcmon.exporters.perfetto_proto import (
    ChildTracksOrdering,
    CounterDescriptorField,
    DebugAnnotationField,
    ProcessDescriptorField,
    ProcessOrdering,
    TraceField,
    TracePacketField,
    TrackDescriptorField,
    TrackEventField,
    TrackEventType,
)


class TestPerfettoProtoConstants:
    def test_trace_field(self) -> None:
        desc = Trace.DESCRIPTOR
        assert desc is not None
        assert desc.fields_by_name["packet"].number == TraceField.PACKET

    def test_trace_packet_field(self) -> None:
        desc = TracePacket.DESCRIPTOR
        assert desc is not None
        assert desc.fields_by_name["timestamp"].number == TracePacketField.TIMESTAMP
        assert desc.fields_by_name["trusted_packet_sequence_id"].number == TracePacketField.SEQUENCE_ID
        assert desc.fields_by_name["track_event"].number == TracePacketField.TRACK_EVENT
        assert desc.fields_by_name["compressed_packets"].number == TracePacketField.COMPRESSED_PACKETS
        assert desc.fields_by_name["zstd_compressed_packets"].number == TracePacketField.ZSTD_COMPRESSED_PACKETS
        assert desc.fields_by_name["track_descriptor"].number == TracePacketField.TRACK_DESCRIPTOR

    def test_track_descriptor_field(self) -> None:
        desc = TrackDescriptor.DESCRIPTOR
        assert desc is not None
        f = desc.fields_by_name
        assert f["uuid"].number == TrackDescriptorField.UUID
        assert f["name"].number == TrackDescriptorField.NAME
        assert f["process"].number == TrackDescriptorField.PROCESS
        assert f["parent_uuid"].number == TrackDescriptorField.PARENT_UUID
        assert f["counter"].number == TrackDescriptorField.COUNTER
        assert f["child_ordering"].number == TrackDescriptorField.CHILD_ORDERING
        assert f["sibling_order_rank"].number == TrackDescriptorField.SIBLING_ORDER_RANK
        assert f["description"].number == TrackDescriptorField.DESCRIPTION
        assert f["process_ordering"].number == TrackDescriptorField.PROCESS_ORDERING

    def test_process_descriptor_field(self) -> None:
        desc = ProcessDescriptor.DESCRIPTOR
        assert desc is not None
        f = desc.fields_by_name
        assert f["pid"].number == ProcessDescriptorField.PID
        assert f["cmdline"].number == ProcessDescriptorField.CMDLINE
        assert f["process_name"].number == ProcessDescriptorField.PROCESS_NAME
        assert f["start_timestamp_ns"].number == ProcessDescriptorField.START_TIMESTAMP_NS

    def test_counter_descriptor_field(self) -> None:
        desc = CounterDescriptor.DESCRIPTOR
        assert desc is not None
        f = desc.fields_by_name
        assert f["type"].number == CounterDescriptorField.TYPE
        assert f["categories"].number == CounterDescriptorField.CATEGORIES
        assert f["unit"].number == CounterDescriptorField.UNIT
        assert f["unit_multiplier"].number == CounterDescriptorField.UNIT_MULTIPLIER
        assert f["is_incremental"].number == CounterDescriptorField.IS_INCREMENTAL
        assert f["unit_name"].number == CounterDescriptorField.UNIT_NAME
        assert f["y_axis_share_key"].number == CounterDescriptorField.Y_AXIS_SHARE_KEY

    def test_track_event_field(self) -> None:
        desc = TrackEvent.DESCRIPTOR
        assert desc is not None
        f = desc.fields_by_name
        assert f["type"].number == TrackEventField.TYPE
        assert f["track_uuid"].number == TrackEventField.TRACK_UUID
        assert f["debug_annotations"].number == TrackEventField.DEBUG_ANNOTATIONS
        assert f["categories"].number == TrackEventField.CATEGORIES
        assert f["name"].number == TrackEventField.NAME
        assert f["counter_value"].number == TrackEventField.COUNTER_VALUE
        assert f["double_counter_value"].number == TrackEventField.DOUBLE_COUNTER_VALUE
        assert f["timestamp_delta_us"].number == TrackEventField.TIMESTAMP_DELTA_US
        assert f["timestamp_absolute_us"].number == TrackEventField.TIMESTAMP_ABSOLUTE_US

    def test_debug_annotation_field(self) -> None:
        desc = DebugAnnotation.DESCRIPTOR
        assert desc is not None
        f = desc.fields_by_name
        assert f["name"].number == DebugAnnotationField.NAME
        assert f["bool_value"].number == DebugAnnotationField.BOOL_VALUE
        assert f["int_value"].number == DebugAnnotationField.INT_VALUE
        assert f["string_value"].number == DebugAnnotationField.STRING_VALUE
        assert f["dict_entries"].number == DebugAnnotationField.DICT_ENTRIES

    def test_dict_entries_sits_outside_the_value_oneof(self) -> None:
        """Protobuf keeps the scalar values one-at-a-time and stops there. An
        annotation setting both a group and a value encodes without complaint,
        so leaving the value fields unset is gcmon's job."""
        desc = DebugAnnotation.DESCRIPTOR
        assert desc is not None
        f = desc.fields_by_name
        assert f["string_value"].containing_oneof is not None
        assert f["dict_entries"].containing_oneof is None

    def test_type_constants(self) -> None:
        assert int(TrackEvent.Type.TYPE_SLICE_BEGIN) == int(TrackEventType.SLICE_BEGIN)
        assert int(TrackEvent.Type.TYPE_SLICE_END) == int(TrackEventType.SLICE_END)
        assert int(TrackEvent.Type.TYPE_INSTANT) == int(TrackEventType.INSTANT)
        assert int(TrackEvent.Type.TYPE_COUNTER) == int(TrackEventType.COUNTER)

    def test_child_tracks_ordering(self) -> None:
        v = TrackDescriptor.ChildTracksOrdering
        assert int(v.UNKNOWN) == ChildTracksOrdering.UNKNOWN
        assert int(v.LEXICOGRAPHIC) == ChildTracksOrdering.LEXICOGRAPHIC
        assert int(v.CHRONOLOGICAL) == ChildTracksOrdering.CHRONOLOGICAL
        assert int(v.EXPLICIT) == ChildTracksOrdering.EXPLICIT

    def test_process_ordering(self) -> None:
        v = TrackDescriptor.ProcessOrdering
        assert int(v.PROCESS_ORDERING_UNSPECIFIED) == ProcessOrdering.UNSPECIFIED
        assert int(v.PROCESS_ORDERING_EXPLICIT) == ProcessOrdering.EXPLICIT
