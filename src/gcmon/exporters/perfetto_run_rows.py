"""The run rows: every pause in the run on its generation's row, every loss
window on one ``GC Loss`` row, every mark on one ``Marks`` row and each
process's RSS under ``RSS``, at the top level beside the process rows.

A track per interpreter and generation for its pauses, per interpreter for its
loss windows and per process for its marks, which the trace processor merges
with the others sharing its name and parent into one row, as many lanes tall
as the most interpreters pausing, or blind, or processes marking at once.

A slice that begins inside an open one stays on its lane, so a sub-phase stays
inside its pause.
"""

from collections.abc import Sequence

from ..model.names import PAUSE, PID, PID_EPOCH, RSS
from ..model.process import Process
from ..model.trace_event import Instant, LossTrack, PauseTrack, ProcessTrack, Slice, TraceEvent
from .perfetto_builders import (
    _build_debug_annotation_int,
    _make_counter_event,
    _make_instant_packet,
    _make_slice_packets,
    build_trace_packet,
    build_track_descriptor,
)
from .perfetto_format import _LOSS_TRACK_NAME
from .perfetto_process_lifetime import process_track_name
from .perfetto_proto import ChildTracksOrdering
from .perfetto_track_state import PerfettoTrackState

__all__ = ["draw_batch_on_run_rows"]


_ALL_PAUSES_GROUP_NAME: str = "All GC Pauses"

_MARKS_TRACK_NAME: str = "Marks"

_RSS_GROUP_NAME: str = "RSS"


def _generation_row_name(gen: int) -> str:
    return f"Gen{gen}"


def _emit_group_descriptor(
    name: str,
    state: PerfettoTrackState,
    sequence_id: int,
) -> tuple[int, list[bytes]]:
    """Build the group *name* at the top level, its children ranked, once,
    and return its uuid."""
    if state.has_run_group(name):
        return state.get_or_create_run_group_uuid(name), []
    group_uuid = state.get_or_create_run_group_uuid(name)
    state.mark_run_group(name)
    desc = build_track_descriptor(group_uuid, name, child_ordering=ChildTracksOrdering.EXPLICIT)
    return group_uuid, [build_trace_packet(sequence_id, track_descriptor=desc)]


def _emit_generation_track_descriptor(
    track: PauseTrack,
    state: PerfettoTrackState,
    sequence_id: int,
) -> list[bytes]:
    """Build the descriptor of the track *track*'s pauses draw on among the
    run rows, once, its group first."""
    if state.has_run_track(track):
        return []
    group_uuid, packets = _emit_group_descriptor(_ALL_PAUSES_GROUP_NAME, state, sequence_id)
    state.mark_run_track(track)
    desc = build_track_descriptor(
        state.get_or_create_run_track_uuid(track),
        _generation_row_name(track.gen),
        parent_uuid=group_uuid,
        sibling_order_rank=track.gen,
    )
    return [*packets, build_trace_packet(sequence_id, track_descriptor=desc)]


def _emit_root_track_descriptor(
    track: LossTrack | ProcessTrack,
    name: str,
    state: PerfettoTrackState,
    sequence_id: int,
) -> list[bytes]:
    """Build the descriptor of the track *track*'s events draw on among the
    run rows, once, at the top level."""
    if state.has_run_track(track):
        return []
    state.mark_run_track(track)
    desc = build_track_descriptor(state.get_or_create_run_track_uuid(track), name)
    return [build_trace_packet(sequence_id, track_descriptor=desc)]


def _emit_rss_counter_descriptor(
    track: ProcessTrack,
    state: PerfettoTrackState,
    sequence_id: int,
) -> list[bytes]:
    """Build the descriptor of the counter *track*'s RSS draws on among the
    run rows, once, its group first."""
    if state.has_run_counter_track(track):
        return []
    rank = state.get_process_track_rank(track.process)
    assert rank is not None, f"{track.process} is drawn before the shared part folded it in"
    group_uuid, packets = _emit_group_descriptor(_RSS_GROUP_NAME, state, sequence_id)
    state.mark_run_counter_track(track)
    desc = build_track_descriptor(
        state.get_or_create_run_counter_track_uuid(track),
        f"{process_track_name(track.process)} {RSS}",
        parent_uuid=group_uuid,
        is_counter=True,
        sibling_order_rank=rank,
    )
    return [*packets, build_trace_packet(sequence_id, track_descriptor=desc)]


def _naming_its_process(process: Process, encoded: list[bytes], state: PerfettoTrackState) -> list[bytes]:
    named = state.get_pid_annotations(process)
    if named is None:
        named = (
            _build_debug_annotation_int(PID, process.pid),
            _build_debug_annotation_int(PID_EPOCH, process.pid_epoch),
        )
        state.set_pid_annotations(process, named)
    return [*named, *encoded]


def draw_batch_on_run_rows(
    events: Sequence[TraceEvent],
    annotations: Sequence[list[bytes]],
    state: PerfettoTrackState,
    sequence_id: int,
) -> tuple[list[bytes], list[bytes]]:
    """The run rows' share of a batch: every descriptor its events need and
    the packets drawing them.

    A pause, a loss window and a mark carry ``pid`` and ``pid_epoch``, which
    settle whose it is where two processes' spans on ``Processes`` both cover
    it. An interpreter's counters are not drawn here.
    """
    descriptors: list[bytes] = []
    packets: list[bytes] = []
    for event, encoded in zip(events, annotations, strict=True):
        if isinstance(event, Instant):
            track = event.track
            descriptors.extend(_emit_root_track_descriptor(track, _MARKS_TRACK_NAME, state, sequence_id))
            packets.append(
                _make_instant_packet(
                    sequence_id,
                    state.get_or_create_run_track_uuid(track),
                    event.name,
                    event.ts,
                    _naming_its_process(track.process, encoded, state),
                )
            )
        elif isinstance(event, Slice):
            slice_track = event.track
            if isinstance(slice_track, PauseTrack):
                descriptors.extend(_emit_generation_track_descriptor(slice_track, state, sequence_id))
                names_its_process = event.cat.startswith(PAUSE.category)
            else:
                descriptors.extend(_emit_root_track_descriptor(slice_track, _LOSS_TRACK_NAME, state, sequence_id))
                names_its_process = True
            if names_its_process:
                encoded = _naming_its_process(slice_track.process, encoded, state)
            packets.extend(
                _make_slice_packets(
                    sequence_id,
                    state.get_or_create_run_track_uuid(slice_track),
                    event.name,
                    event.cat,
                    event.ts_start,
                    event.ts_stop,
                    encoded,
                )
            )
        elif isinstance(event.track, ProcessTrack) and event.metric == RSS:
            descriptors.extend(_emit_rss_counter_descriptor(event.track, state, sequence_id))
            packets.append(
                build_trace_packet(
                    sequence_id,
                    timestamp=event.ts,
                    track_event=_make_counter_event(
                        state.get_or_create_run_counter_track_uuid(event.track), event.value
                    ),
                )
            )
    return descriptors, packets
