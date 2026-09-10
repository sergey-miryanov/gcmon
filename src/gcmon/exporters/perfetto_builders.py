"""Pure builders for Perfetto protobuf submessages and packets.

Plain values in, wire-format bytes out. Nothing here touches
``PerfettoTrackState``, allocates a uuid, or decides layout policy, which
is what keeps it directly testable against the wire format (ADR-0001).
"""

from collections.abc import Mapping, Sequence

from .perfetto_proto import (
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
from .protobuf_encoder import (
    encode_bytes_field,
    encode_double_field,
    encode_string_field,
    encode_varint_field,
)

__all__ = [
    "build_trace",
    "build_trace_packet",
    "build_track_descriptor",
    "build_track_event",
]


def build_track_descriptor(
    uuid: int,
    name: str,
    pid: int | None = None,
    parent_uuid: int | None = None,
    is_counter: bool = False,
    child_ordering: ChildTracksOrdering | None = None,
    sibling_order_rank: int | None = None,
    cmdline: Sequence[str] | None = None,
    description: str | None = None,
    process_ordering: ProcessOrdering | None = None,
    start_timestamp_ns: int | None = None,
    y_axis_share_key: str | None = None,
) -> bytes:
    """Build a ``TrackDescriptor`` submessage as wire-format bytes.

    Parameters
    ----------
    uuid
        Track UUID; the special value ``0`` is reserved for the root
        track descriptor that carries the ``process_ordering`` hint.
    name
        Human-readable track name. Falsy values (empty string) suppress
        the ``name`` field, used for the root descriptor and for any
        other track where the name would be redundant.
    pid, cmdline, start_timestamp_ns
        Populate the ``ProcessDescriptor`` sub-message, the only
        OS-association gcmon writes (ADR-0027).
    parent_uuid
        Optional parent track UUID; sets ``TrackDescriptor.parent_uuid``.
    is_counter
        When ``True``, emits an empty ``CounterDescriptor`` sub-message
        (or a populated one if ``y_axis_share_key`` is set) at
        ``TrackDescriptor.counter`` (field 8).
    child_ordering, sibling_order_rank
        Grouped-track ordering hints consumed by trace processor.
    description
        Human-readable description; surfaced in the Perfetto UI as a
        tooltip on the track's help icon.
    process_ordering
        A root-descriptor-only hint that tells the UI to honor
        ``sibling_order_rank`` on process tracks. Only set it on the root
        descriptor (``uuid = 0``).
    y_axis_share_key
        Optional string used to group counter tracks with the same
        parent on a shared Y-axis in the Perfetto UI. Only effective
        when ``is_counter=True`` and the value is a non-empty string;
        an empty string is treated as "no key set" (REQ-6 in
        ``specs/17 - counter-y-axis-sharing.md``). Ignored entirely
        when ``is_counter=False``. See
        https://perfetto.dev/docs/reference/synthetic-track-event#sharing-y-axis-between-counters
    """
    result = encode_varint_field(TrackDescriptorField.UUID, uuid)
    if name:
        result += encode_string_field(TrackDescriptorField.NAME, name)
    if pid is not None:
        process_desc = encode_varint_field(ProcessDescriptorField.PID, pid)
        if cmdline:
            for arg in cmdline:
                process_desc += encode_string_field(ProcessDescriptorField.CMDLINE, arg)
        process_desc += encode_string_field(ProcessDescriptorField.PROCESS_NAME, name)
        if start_timestamp_ns is not None:
            process_desc += encode_varint_field(
                ProcessDescriptorField.START_TIMESTAMP_NS,
                start_timestamp_ns,
            )
        result += encode_bytes_field(TrackDescriptorField.PROCESS, process_desc)
    if parent_uuid is not None:
        result += encode_varint_field(TrackDescriptorField.PARENT_UUID, parent_uuid)
    if process_ordering is not None:
        result += encode_varint_field(TrackDescriptorField.PROCESS_ORDERING, process_ordering)
    if is_counter:
        if y_axis_share_key:
            counter_desc = encode_string_field(
                CounterDescriptorField.Y_AXIS_SHARE_KEY,
                y_axis_share_key,
            )
            result += encode_bytes_field(TrackDescriptorField.COUNTER, counter_desc)
        else:
            result += encode_bytes_field(TrackDescriptorField.COUNTER, b"")
    if child_ordering is not None:
        result += encode_varint_field(TrackDescriptorField.CHILD_ORDERING, child_ordering)
    if sibling_order_rank is not None:
        result += encode_varint_field(TrackDescriptorField.SIBLING_ORDER_RANK, sibling_order_rank)
    if description is not None:
        result += encode_string_field(TrackDescriptorField.DESCRIPTION, description)
    return result


def build_trace_packet(
    sequence_id: int,
    timestamp: int | None = None,
    track_event: bytes | None = None,
    track_descriptor: bytes | None = None,
) -> bytes:
    result = b""
    if timestamp is not None:
        result += encode_varint_field(TracePacketField.TIMESTAMP, timestamp)
    result += encode_varint_field(TracePacketField.SEQUENCE_ID, sequence_id)
    if track_event is not None:
        result += encode_bytes_field(TracePacketField.TRACK_EVENT, track_event)
    if track_descriptor is not None:
        result += encode_bytes_field(TracePacketField.TRACK_DESCRIPTOR, track_descriptor)
    return result


def _build_debug_annotation_bool(name: str, value: bool) -> bytes:
    """A flag the UI and SQL both read as ``true`` / ``false``.

    Always written, whichever way it reads: a consumer asks for the value
    and never for the field's presence.
    """
    result = encode_string_field(DebugAnnotationField.NAME, name)
    result += encode_varint_field(DebugAnnotationField.BOOL_VALUE, int(value))
    return result


def _build_debug_annotation_int(name: str, value: int) -> bytes:
    result = encode_string_field(DebugAnnotationField.NAME, name)
    result += encode_varint_field(DebugAnnotationField.INT_VALUE, value)
    return result


def _build_debug_annotation_string(name: str, value: str) -> bytes:
    result = encode_string_field(DebugAnnotationField.NAME, name)
    result += encode_string_field(DebugAnnotationField.STRING_VALUE, value)
    return result


def _build_debug_annotation_dict(name: str, entries: Mapping[str, int | str]) -> bytes:
    """A named group of annotations, rendered as one expandable node.

    The trace processor flattens it back out for SQL, joining the names with a
    dot: a ``lost_count`` under ``gen0`` is queried as
    ``args.debug.gen0.lost_count``.
    """
    result = encode_string_field(DebugAnnotationField.NAME, name)
    for key, value in entries.items():
        entry = (
            _build_debug_annotation_string(key, value)
            if isinstance(value, str)
            else _build_debug_annotation_int(key, value)
        )
        result += encode_bytes_field(DebugAnnotationField.DICT_ENTRIES, entry)
    return result


def build_track_event(
    type: TrackEventType,
    track_uuid: int,
    name: str | None = None,
    categories: list[str] | None = None,
    counter_value: int | None = None,
    double_counter_value: float | None = None,
    debug_annotations: list[bytes] | None = None,
) -> bytes:
    result = encode_varint_field(TrackEventField.TYPE, type)
    result += encode_varint_field(TrackEventField.TRACK_UUID, track_uuid)
    if categories:
        for cat in categories:
            result += encode_string_field(TrackEventField.CATEGORIES, cat)
    if name is not None:
        result += encode_string_field(TrackEventField.NAME, name)
    if counter_value is not None:
        result += encode_varint_field(TrackEventField.COUNTER_VALUE, counter_value)
    if double_counter_value is not None:
        result += encode_double_field(
            TrackEventField.DOUBLE_COUNTER_VALUE,
            double_counter_value,
        )
    if debug_annotations:
        for ann in debug_annotations:
            result += encode_bytes_field(TrackEventField.DEBUG_ANNOTATIONS, ann)
    return result


def build_trace(packets: list[bytes]) -> bytes:
    result = b""
    for packet in packets:
        result += encode_bytes_field(TraceField.PACKET, packet)
    return result


def _make_slice_begin(
    track_uuid: int,
    name: str,
    categories: list[str],
    annotations: list[bytes],
) -> bytes:
    return build_track_event(
        type=TrackEventType.SLICE_BEGIN,
        track_uuid=track_uuid,
        name=name,
        categories=categories,
        debug_annotations=annotations,
    )


def _make_slice_end(track_uuid: int) -> bytes:
    return build_track_event(
        type=TrackEventType.SLICE_END,
        track_uuid=track_uuid,
    )


def _make_counter_event(track_uuid: int, value: int | float) -> bytes:
    if isinstance(value, float):
        return build_track_event(
            type=TrackEventType.COUNTER,
            track_uuid=track_uuid,
            double_counter_value=value,
        )
    return build_track_event(
        type=TrackEventType.COUNTER,
        track_uuid=track_uuid,
        counter_value=value,
    )


def _args_to_debug_annotations(args: Mapping[str, int | str | dict[str, int | str]]) -> list[bytes]:
    return [_build_debug_annotation(k, v) for k, v in args.items()]


def _build_debug_annotation(name: str, value: int | str | Mapping[str, int | str]) -> bytes:
    if isinstance(value, str):
        return _build_debug_annotation_string(name, value)
    if isinstance(value, Mapping):
        return _build_debug_annotation_dict(name, value)
    return _build_debug_annotation_int(name, value)
