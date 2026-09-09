"""Track layout policy and the GC-to-Perfetto conversion pass.

``convert_trace_events_to_perfetto`` maps ``TraceEvent`` objects from the
shared ``trace_converter`` to Perfetto packets; the ``_emit_*`` helpers
place, parent and order each track.

The module is also the package's public face, re-exporting the enums,
builders, ``PerfettoTrackState`` and ``finalize_perfetto_packets`` so an
importer needs one name.
"""

from collections.abc import Sequence

from ..model.trace_event import (
    Counter,
    Instant,
    InterpreterTrack,
    LossTrack,
    ProcessTrack,
    Slice,
    TraceEvent,
    Track,
)
from .perfetto_builders import (
    _args_to_debug_annotations,
    _make_counter_event,
    _make_slice_begin,
    _make_slice_end,
    build_trace,
    build_trace_packet,
    build_track_descriptor,
    build_track_event,
)
from .perfetto_process_lifetime import (
    _emit_process_descriptor,
    _emit_root_descriptor,
    _record_capture_totals,
    _record_process_lifetime,
    emit_retired_process_row,
    finalize_perfetto_packets,
)
from .perfetto_proto import (
    ChildTracksOrdering,
    CounterDescriptorField,
    DebugAnnotationField,
    ProcessDescriptorField,
    ProcessOrdering,
    ThreadDescriptorField,
    ThreadOrdering,
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
    "ThreadDescriptorField",
    "ThreadOrdering",
    "TraceField",
    "TracePacketField",
    "TrackDescriptorField",
    "TrackEventField",
    "TrackEventType",
    "build_trace",
    "build_trace_packet",
    "build_track_descriptor",
    "build_track_event",
    "convert_trace_events_to_perfetto",
    "emit_retired_process_row",
    "finalize_perfetto_packets",
]


_COUNTER_RANKS: dict[str, int] = {
    "heap_size": 0,
    "rss": 1,
    "collected": 2,
    "uncollectable": 3,
    "candidates": 4,
    "duration": 5,
    "increment_size": 6,
    "alive_size": 7,
    "finalized_garbage_count": 8,
    "deleted_garbage_count": 9,
    "clear_weakrefs_count": 10,
}

# A counter an interpreter owns that is nonetheless drawn a level up, beside
# the process's own counters rather than inside its `GC Metrics` group
# (ADR-0004). The process track is OS-scoped, so the trace processor drops
# `sibling_order_rank` for these and their position in the UI is a heuristic.
#
# `rss` is not here: a `ProcessTrack` owns it, so parenting it to the process
# row is its identity rather than a policy.
_TOPLEVEL_COUNTER_METRICS: frozenset[str] = frozenset({"heap_size"})

_COUNTER_GROUP_NAME: str = "GC Metrics"

_LOSS_TRACK_NAME: str = "GC Loss"
# Below the interpreter's own thread track, which ranks 0.
_LOSS_TRACK_RANK: int = 1


def _emit_thread_descriptor(
    track: InterpreterTrack,
    state: PerfettoTrackState,
    sequence_id: int,
) -> list[bytes]:
    """Build *track*'s thread track descriptor if not already emitted."""
    if state.has_track(track):
        return []
    state.mark_track(track)
    iid = track.iid
    # The row's pid, not the operating system's, so this thread lands under
    # its own process (ADR-0011). The `tid` beside it is the interpreter id
    # for every interpreter: it names a synthetic interpreter, not an
    # operating-system thread, so interpreter 0 takes no main-thread
    # treatment.
    row_pid = state.get_row_pid(track.process)
    desc = build_track_descriptor(
        state.get_track_uuid(track),
        f"Thread {iid}",
        pid=row_pid,
        tid=iid,
        parent_uuid=state.get_process_track_uuid(track.process),
        sibling_order_rank=0,
        thread_name=f"Thread {iid}",
    )
    return [build_trace_packet(sequence_id, track_descriptor=desc)]


def _emit_loss_descriptor(
    track: LossTrack,
    state: PerfettoTrackState,
    sequence_id: int,
) -> list[bytes]:
    """Build *track*'s GC Loss track descriptor, once.

    A plain custom track rather than a thread: a ``LossTrack`` names an
    interpreter but no OS thread, and a ``thread`` sub-message would describe
    one that does not exist.
    """
    if state.has_track(track):
        return []
    state.mark_track(track)
    desc = build_track_descriptor(
        state.get_track_uuid(track),
        f"{_LOSS_TRACK_NAME} {track.iid}",
        parent_uuid=state.get_process_track_uuid(track.process),
        sibling_order_rank=_LOSS_TRACK_RANK,
    )
    return [build_trace_packet(sequence_id, track_descriptor=desc)]


def _emit_counter_group_descriptor(
    track: Track,
    state: PerfettoTrackState,
    sequence_id: int,
) -> tuple[int, list[bytes]]:
    """Build *track*'s GC Metrics grouping track descriptor.

    It carries no ``process`` or ``thread`` field: the trace processor honors
    ordering on a plain custom track and not on an OS-scoped one (ADR-0003).
    """
    if state.has_counter_group_track(track):
        return state.get_or_create_counter_group_track_uuid(track), []
    group_uuid = state.get_or_create_counter_group_track_uuid(track)
    desc = build_track_descriptor(
        group_uuid,
        _COUNTER_GROUP_NAME,
        parent_uuid=state.get_process_track_uuid(track.process),
        child_ordering=ChildTracksOrdering.EXPLICIT,
        sibling_order_rank=0,
    )
    return group_uuid, [build_trace_packet(sequence_id, track_descriptor=desc)]


def _emit_counter_track_descriptor(
    track: Track,
    metric: str,
    display_name: str,
    state: PerfettoTrackState,
    sequence_id: int,
) -> tuple[int, list[bytes]]:
    """Build a counter track descriptor if not already emitted.

    A counter the process owns, and one an interpreter owns whose metric is in
    ``_TOPLEVEL_COUNTER_METRICS``, hangs off the process track and renders at
    the top level. Every other counter hangs off its owner's GC Metrics group,
    where the trace processor and the UI honor its ``_COUNTER_RANKS`` entry.

    *display_name* is the track name on the wire and identifies the track
    within *track*; *metric* is what the rank and the shared y axis are keyed
    on, so ``G0 collected`` and ``G1 collected`` share a scale.
    """
    if isinstance(track, ProcessTrack) or metric in _TOPLEVEL_COUNTER_METRICS:
        if state.has_counter_track(track, display_name):
            return state.get_or_create_counter_track_uuid(track, display_name), []
        ctr_uuid = state.get_or_create_counter_track_uuid(track, display_name)
        desc = build_track_descriptor(
            ctr_uuid,
            display_name,
            parent_uuid=state.get_process_track_uuid(track.process),
            is_counter=True,
            sibling_order_rank=_COUNTER_RANKS.get(metric, 0),
        )
        return ctr_uuid, [build_trace_packet(sequence_id, track_descriptor=desc)]
    group_uuid, group_packets = _emit_counter_group_descriptor(track, state, sequence_id)
    if state.has_counter_track(track, display_name):
        ctr_uuid = state.get_or_create_counter_track_uuid(track, display_name)
        return ctr_uuid, group_packets
    ctr_uuid = state.get_or_create_counter_track_uuid(track, display_name)
    desc = build_track_descriptor(
        ctr_uuid,
        display_name,
        parent_uuid=group_uuid,
        is_counter=True,
        sibling_order_rank=_COUNTER_RANKS.get(metric, 0),
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
    process descriptor *is* its descriptor.
    """
    process = track.process
    descriptors = _emit_process_descriptor(
        process,
        state,
        sequence_id,
        sibling_order_rank=state.get_process_track_rank(process),
        start_timestamp_ns=state.get_process_lifetime_start_ts(process),
    )
    if isinstance(track, InterpreterTrack):
        descriptors.extend(_emit_thread_descriptor(track, state, sequence_id))
    elif isinstance(track, LossTrack):
        descriptors.extend(_emit_loss_descriptor(track, state, sequence_id))
    return descriptors


def convert_trace_events_to_perfetto(
    events: Sequence[TraceEvent],
    state: PerfettoTrackState,
    sequence_id: int,
) -> tuple[list[bytes], list[bytes]]:
    """Convert a list of ``TraceEvent`` objects to Perfetto protobuf packets.

    A track's descriptor goes out because an event named that track, ahead of
    the packet that named it. No producer sends metadata first, and none can
    forget to.

    Returns ``(descriptors, packets)``, two lists of encoded ``TracePacket``
    bytes ready for ``build_trace``. The first call on a given *state* also
    emits the root descriptor.

    Each process descriptor carries a ``sibling_order_rank`` and a
    ``process.start_timestamp_ns``, both taken from that process's first
    event. *state* accumulates those across batches, and the pre-pass below
    folds this batch in before the main loop, so a process described in this
    batch is ranked against the events of it. Both fields are always present:
    a descriptor goes out only for a process that named a track, which is a
    process the pre-pass has folded in.

    ``Processes``-track slices go out at trace close instead, from
    ``finalize_perfetto_packets``.
    """
    descriptors: list[bytes] = []
    packets: list[bytes] = []

    if events:
        for event in events:
            _record_process_lifetime(event, state)
            _record_capture_totals(event, state)
        descriptors.extend(_emit_root_descriptor(state, sequence_id))

    # Ranked after the pre-pass folded the batch in, so the processes this
    # batch describes are ordered against each other by first observation
    # rather than by the order their events happen to sit in.
    state.rank_processes(event.track.process for event in events)

    for event in events:
        process = event.track.process
        descriptors.extend(_emit_track_descriptors(event.track, state, sequence_id))

        if isinstance(event, Slice):
            track_uuid = state.get_track_uuid(event.track)
            # An adjacent pair, not interleaved into stack order: the trace
            # processor sorts by timestamp and builds the nesting itself.
            # BEGIN first, so a zero-length slice reads as ``dur = 0`` rather
            # than ``-1``. See ADR-0011 for the pattern. An END here carries no
            # name and closes the top of the stack, so it does not need the
            # naming ADR-0011 relies on.
            packets.append(
                build_trace_packet(
                    sequence_id,
                    timestamp=event.ts_start,
                    track_event=_make_slice_begin(
                        track_uuid,
                        event.name,
                        [event.cat],
                        _args_to_debug_annotations(event.args),
                    ),
                )
            )
            packets.append(
                build_trace_packet(
                    sequence_id,
                    timestamp=event.ts_stop,
                    track_event=_make_slice_end(track_uuid),
                )
            )

        elif isinstance(event, Instant):
            proc_uuid = state.get_process_track_uuid(process)
            packets.append(
                build_trace_packet(
                    sequence_id,
                    timestamp=event.ts,
                    track_event=build_track_event(
                        type=TrackEventType.INSTANT,
                        track_uuid=proc_uuid,
                        name=event.name,
                        debug_annotations=_args_to_debug_annotations(event.args),
                    ),
                )
            )

        elif isinstance(event, Counter):
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
