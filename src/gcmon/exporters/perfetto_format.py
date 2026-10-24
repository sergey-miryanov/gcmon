"""Track layout policy and the GC-to-Perfetto conversion pass (ADR-0001).

``draw_batch_on_process_rows`` maps ``TraceEvent`` objects from the shared
``trace_converter`` to the process rows' Perfetto packets; the ``_emit_*``
helpers place, parent and order each track.

The module is also the package's public face, re-exporting the enums,
builders, ``PerfettoTrackState``, ``fold_batch`` and
``finalize_perfetto_packets`` so an importer needs one name.
"""

from collections.abc import Sequence

from ..model.names import HEAP_SIZE
from ..model.phases import Counters
from ..model.trace_event import (
    Counter,
    Instant,
    InterpreterTrack,
    LossTrack,
    PauseTrack,
    ProcessTrack,
    Slice,
    TraceEvent,
    Track,
)
from .perfetto_builders import (
    _args_to_debug_annotations,
    _make_counter_event,
    _make_instant_packet,
    _make_slice_packets,
    build_trace,
    build_trace_packet,
    build_track_descriptor,
    build_track_event,
)
from .perfetto_process_lifetime import (
    _emit_process_descriptor,
    _emit_root_descriptor,
    _record_capture_totals,
    emit_retired_process_row,
    finalize_perfetto_packets,
    fold_batch,
)
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
from .perfetto_track_state import PerfettoTrackState

__all__ = [
    "ChildTracksOrdering",
    "CounterDescriptorField",
    "DebugAnnotationField",
    "PerfettoTrackState",
    "ProcessDescriptorField",
    "ProcessOrdering",
    "TraceField",
    "TracePacketField",
    "TrackDescriptorField",
    "TrackEventField",
    "TrackEventType",
    "build_trace",
    "build_trace_packet",
    "build_track_descriptor",
    "build_track_event",
    "draw_batch_on_process_rows",
    "emit_retired_process_row",
    "encode_batch_args",
    "finalize_perfetto_packets",
    "fold_batch",
]


_COUNTER_GROUP_NAME: str = "GC Metrics"

# The word "Python" is what sorts the group after `Process {pid}`: the
# Perfetto UI orders a process's rows by name within a kind (ADR-0027).
_INTERPRETER_LIST_NAME: str = "Python Interpreters"


# The fixed half of an interpreter group's name, named for the same
# reason as the process row's.
_INTERPRETER_GROUP_PREFIX: str = "Interpreter "


def _interpreter_group_name(iid: int) -> str:
    return f"{_INTERPRETER_GROUP_PREFIX}{iid}"


_PAUSE_TRACK_NAME: str = "GC Pauses"
_LOSS_TRACK_NAME: str = "GC Loss"

# A counter an interpreter owns that is nonetheless drawn a level up, on the
# interpreter's own group rather than inside its `GC Metrics` group
# (ADR-0004, ADR-0027).
#
# `rss` is not here: a `ProcessTrack` owns it, so parenting it to the process
# row is its identity rather than a policy.
_TOPLEVEL_COUNTER_METRICS: frozenset[str] = frozenset({HEAP_SIZE})

# What an interpreter group holds, top to bottom (ADR-0027). Each row ranks by
# its index here, so this tuple is the whole statement of the order.
_INTERPRETER_ROW_ORDER: tuple[str, ...] = (
    _PAUSE_TRACK_NAME,
    _LOSS_TRACK_NAME,
    HEAP_SIZE,
    _COUNTER_GROUP_NAME,
)
_INTERPRETER_ROW_RANKS: dict[str, int] = {name: rank for rank, name in enumerate(_INTERPRETER_ROW_ORDER)}

# What a `GC Metrics` group holds, ranked the same way. Neither counter drawn
# elsewhere is here: `heap_size` ranks among its interpreter's own rows
# through `_INTERPRETER_ROW_RANKS`, and `rss` parents to the process track,
# which is OS-scoped, and the trace processor discards a rank there
# (ADR-0003).
_COUNTER_RANKS: dict[str, int] = {metric: rank for rank, metric in enumerate(Counters.counter_metrics)}

# A metric this module has never heard of draws below every one it has.
_UNLISTED_COUNTER_RANK: int = len(_COUNTER_RANKS)


def _emit_interpreter_group_descriptors(
    track: PauseTrack | InterpreterTrack | LossTrack,
    state: PerfettoTrackState,
    sequence_id: int,
) -> tuple[int, list[bytes]]:
    """Build the two groups that hold *track*'s rows, outer first, and
    return the inner one's uuid (ADR-0027).

    The list carries no rank: the process track above it is OS-scoped, and
    the trace processor discards one there (ADR-0003).
    """
    process = track.process
    packets: list[bytes] = []
    if not state.has_interpreter_list_track(process):
        list_desc = build_track_descriptor(
            state.get_or_create_interpreter_list_track_uuid(process),
            _INTERPRETER_LIST_NAME,
            parent_uuid=state.get_process_track_uuid(process),
            child_ordering=ChildTracksOrdering.EXPLICIT,
        )
        state.mark_interpreter_list_track(process)
        packets.append(build_trace_packet(sequence_id, track_descriptor=list_desc))
    if state.has_interpreter_group_track(process, track.iid):
        return state.get_or_create_interpreter_group_track_uuid(process, track.iid), packets
    group_uuid = state.get_or_create_interpreter_group_track_uuid(process, track.iid)
    state.mark_interpreter_group_track(process, track.iid)
    group_desc = build_track_descriptor(
        group_uuid,
        _interpreter_group_name(track.iid),
        parent_uuid=state.get_or_create_interpreter_list_track_uuid(process),
        child_ordering=ChildTracksOrdering.EXPLICIT,
        sibling_order_rank=track.iid,
    )
    packets.append(build_trace_packet(sequence_id, track_descriptor=group_desc))
    return group_uuid, packets


def _emit_pause_descriptor(
    track: PauseTrack,
    state: PerfettoTrackState,
    sequence_id: int,
) -> list[bytes]:
    """Build *track*'s GC Pauses track descriptor, once.

    A plain custom track under the interpreter's own group, never a thread
    (ADR-0027).
    """
    if state.has_track(track):
        return []
    state.mark_track(track)
    interpreter_uuid, packets = _emit_interpreter_group_descriptors(track, state, sequence_id)
    desc = build_track_descriptor(
        state.get_track_uuid(track),
        _PAUSE_TRACK_NAME,
        parent_uuid=interpreter_uuid,
        sibling_order_rank=_INTERPRETER_ROW_RANKS[_PAUSE_TRACK_NAME],
    )
    return [*packets, build_trace_packet(sequence_id, track_descriptor=desc)]


def _emit_loss_descriptor(
    track: LossTrack,
    state: PerfettoTrackState,
    sequence_id: int,
) -> list[bytes]:
    """Build *track*'s GC Loss track descriptor, once.

    Beside the interpreter's pause row inside its group, and named for what
    it holds rather than for the interpreter (ADR-0027).
    """
    if state.has_track(track):
        return []
    state.mark_track(track)
    interpreter_uuid, packets = _emit_interpreter_group_descriptors(track, state, sequence_id)
    desc = build_track_descriptor(
        state.get_track_uuid(track),
        _LOSS_TRACK_NAME,
        parent_uuid=interpreter_uuid,
        sibling_order_rank=_INTERPRETER_ROW_RANKS[_LOSS_TRACK_NAME],
    )
    return [*packets, build_trace_packet(sequence_id, track_descriptor=desc)]


def _emit_counter_group_descriptor(
    track: InterpreterTrack,
    state: PerfettoTrackState,
    sequence_id: int,
) -> tuple[int, list[bytes]]:
    """Build *track*'s GC Metrics grouping track descriptor.

    Parented to the interpreter's own group rather than to the process track,
    which is what keeps one process's copies from merging (ADR-0027). It
    carries no ``process`` or ``thread`` field: the trace processor honors
    ordering on a plain custom track and not on an OS-scoped one (ADR-0003).
    """
    interpreter_uuid, packets = _emit_interpreter_group_descriptors(track, state, sequence_id)
    if state.has_counter_group_track(track):
        return state.get_or_create_counter_group_track_uuid(track), packets
    group_uuid = state.get_or_create_counter_group_track_uuid(track)
    state.mark_counter_group_track(track)
    desc = build_track_descriptor(
        group_uuid,
        _COUNTER_GROUP_NAME,
        parent_uuid=interpreter_uuid,
        child_ordering=ChildTracksOrdering.EXPLICIT,
        sibling_order_rank=_INTERPRETER_ROW_RANKS[_COUNTER_GROUP_NAME],
    )
    return group_uuid, [*packets, build_trace_packet(sequence_id, track_descriptor=desc)]


def _emit_counter_track_descriptor(
    track: ProcessTrack | InterpreterTrack,
    metric: str,
    display_name: str,
    state: PerfettoTrackState,
    sequence_id: int,
) -> tuple[int, list[bytes]]:
    """Build a counter track descriptor if not already emitted.

    A counter the process owns parents to the process track and carries no
    rank, which the trace processor discards there (ADR-0003). One an
    interpreter owns whose metric is in ``_TOPLEVEL_COUNTER_METRICS`` draws
    on that interpreter's group, beside its pause and loss rows rather than
    inside its GC Metrics group. Every other counter parents to the GC
    Metrics group, where the trace processor and the UI honor its
    ``_COUNTER_RANKS`` entry.

    *display_name* is the track name on the wire and identifies the track
    within *track*; *metric* is what the rank and the shared y axis are keyed
    on, so ``G0 collected`` and ``G1 collected`` share a scale.
    """
    if isinstance(track, ProcessTrack):
        if state.has_counter_track(track, display_name):
            return state.get_or_create_counter_track_uuid(track, display_name), []
        ctr_uuid = state.get_or_create_counter_track_uuid(track, display_name)
        state.mark_counter_track(track, display_name)
        desc = build_track_descriptor(
            ctr_uuid,
            display_name,
            parent_uuid=state.get_process_track_uuid(track.process),
            is_counter=True,
        )
        return ctr_uuid, [build_trace_packet(sequence_id, track_descriptor=desc)]
    if metric in _TOPLEVEL_COUNTER_METRICS:
        interpreter_uuid, packets = _emit_interpreter_group_descriptors(track, state, sequence_id)
        if state.has_counter_track(track, display_name):
            return state.get_or_create_counter_track_uuid(track, display_name), packets
        ctr_uuid = state.get_or_create_counter_track_uuid(track, display_name)
        state.mark_counter_track(track, display_name)
        desc = build_track_descriptor(
            ctr_uuid,
            display_name,
            parent_uuid=interpreter_uuid,
            is_counter=True,
            sibling_order_rank=_INTERPRETER_ROW_RANKS[HEAP_SIZE],
        )
        return ctr_uuid, [*packets, build_trace_packet(sequence_id, track_descriptor=desc)]
    group_uuid, group_packets = _emit_counter_group_descriptor(track, state, sequence_id)
    if state.has_counter_track(track, display_name):
        ctr_uuid = state.get_or_create_counter_track_uuid(track, display_name)
        return ctr_uuid, group_packets
    ctr_uuid = state.get_or_create_counter_track_uuid(track, display_name)
    state.mark_counter_track(track, display_name)
    desc = build_track_descriptor(
        ctr_uuid,
        display_name,
        parent_uuid=group_uuid,
        is_counter=True,
        sibling_order_rank=_COUNTER_RANKS.get(metric, _UNLISTED_COUNTER_RANK),
        y_axis_share_key=metric,
    )
    return ctr_uuid, [*group_packets, build_trace_packet(sequence_id, track_descriptor=desc)]


def _emit_track_descriptors(
    track: Track,
    state: PerfettoTrackState,
    sequence_id: int,
) -> list[bytes]:
    """Every descriptor *track* needs that has not gone out yet, parent
    first.

    The process descriptor whichever kind of track this is, then the track's
    own where it has one of its own to write. A ``ProcessTrack`` has none: the
    process descriptor *is* its descriptor. An ``InterpreterTrack`` has none
    either: each of its counters gets one of its own.
    """
    process = track.process
    descriptors = _emit_process_descriptor(
        process,
        state,
        sequence_id,
        sibling_order_rank=state.get_process_track_rank(process),
        start_timestamp_ns=state.get_process_lifetime_start_ts(process),
    )
    if isinstance(track, PauseTrack):
        descriptors.extend(_emit_pause_descriptor(track, state, sequence_id))
    elif isinstance(track, LossTrack):
        descriptors.extend(_emit_loss_descriptor(track, state, sequence_id))
    return descriptors


def draw_batch_on_process_rows(
    events: Sequence[TraceEvent],
    annotations: Sequence[list[bytes]],
    state: PerfettoTrackState,
    sequence_id: int,
) -> tuple[list[bytes], list[bytes]]:
    """The process rows' share of a batch: every descriptor its events need
    and the packets drawing them.

    A track's descriptor goes out because an event named that track, ahead of
    the packet that named it. No producer sends metadata first, and none can
    forget to.

    Each process descriptor carries a ``sibling_order_rank`` and a
    ``process.start_timestamp_ns``, both taken from that process's first
    event. The shared part (``fold_batch``) has folded this batch in already,
    so a process described in this batch is ranked against the events of it.

    *annotations* are ``encode_batch_args(events)``, the same lists the run
    rows draw the batch with.

    Returns ``(descriptors, packets)``, two lists of encoded ``TracePacket``
    bytes ready for ``build_trace``. The first call on a given *state* also
    emits the root descriptor. A span on the ``Processes`` row goes out with
    its process's own row, at the flush that retires it or at trace close
    (``emit_retired_process_row``, ``finalize_perfetto_packets``).
    """
    descriptors: list[bytes] = []
    packets: list[bytes] = []

    if events:
        descriptors.extend(_emit_root_descriptor(state, sequence_id))

    for event, encoded in zip(events, annotations, strict=True):
        _record_capture_totals(event, state)
        process = event.track.process
        descriptors.extend(_emit_track_descriptors(event.track, state, sequence_id))

        if isinstance(event, Slice):
            packets.extend(
                _make_slice_packets(
                    sequence_id,
                    state.get_track_uuid(event.track),
                    event.name,
                    event.cat,
                    event.ts_start,
                    event.ts_stop,
                    encoded,
                )
            )

        elif isinstance(event, Instant):
            packets.append(
                _make_instant_packet(sequence_id, state.get_process_track_uuid(process), event.name, event.ts, encoded)
            )

        else:
            assert isinstance(event, Counter)
            ctr_uuid, desc_bytes = _emit_counter_track_descriptor(
                event.track,
                event.metric,
                event.display_name,
                state,
                sequence_id,
            )
            descriptors.extend(desc_bytes)
            packets.append(
                build_trace_packet(
                    sequence_id,
                    timestamp=event.ts,
                    track_event=_make_counter_event(ctr_uuid, event.value),
                )
            )

    return descriptors, packets


def encode_batch_args(events: Sequence[TraceEvent]) -> list[list[bytes]]:
    """Every event's args as debug annotations, one list per event, encoded
    once for every set of rows that draws the batch."""
    return [[] if isinstance(event, Counter) else _args_to_debug_annotations(event.args) for event in events]
