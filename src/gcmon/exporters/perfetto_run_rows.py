"""The run rows: every pause in the run on its generation's row, and every
loss window on one ``GC Loss`` row, at the top level beside the process rows.

A track per interpreter and generation for its pauses, and per interpreter
for its loss windows, which the trace processor merges with the others sharing
its name and parent into one row, as many lanes tall as the most interpreters
pausing, or blind, at once.

A slice that begins inside an open one stays on its lane, so a sub-phase stays
inside its pause.
"""

from collections.abc import Sequence

from ..model.names import PAUSE, PID, PID_EPOCH
from ..model.trace_event import LossTrack, PauseTrack, Slice, TraceEvent
from .perfetto_builders import (
    _build_debug_annotation_int,
    _make_slice_packets,
    build_trace_packet,
    build_track_descriptor,
)
from .perfetto_format import _LOSS_TRACK_NAME
from .perfetto_proto import ChildTracksOrdering
from .perfetto_track_state import PerfettoTrackState

__all__ = ["draw_batch_on_run_rows"]


_ALL_PAUSES_GROUP_NAME: str = "All GC Pauses"


def _generation_row_name(gen: int) -> str:
    return f"Gen{gen}"


def _emit_all_pauses_group_descriptor(
    state: PerfettoTrackState,
    sequence_id: int,
) -> tuple[int, list[bytes]]:
    """Build the group every pause track parents to, once, and return its
    uuid."""
    if state.has_all_pauses_group():
        return state.get_or_create_all_pauses_group_uuid(), []
    group_uuid = state.get_or_create_all_pauses_group_uuid()
    desc = build_track_descriptor(group_uuid, _ALL_PAUSES_GROUP_NAME, child_ordering=ChildTracksOrdering.EXPLICIT)
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
    group_uuid, packets = _emit_all_pauses_group_descriptor(state, sequence_id)
    desc = build_track_descriptor(
        state.get_or_create_run_track_uuid(track),
        _generation_row_name(track.gen),
        parent_uuid=group_uuid,
        sibling_order_rank=track.gen,
    )
    return [*packets, build_trace_packet(sequence_id, track_descriptor=desc)]


def _emit_loss_track_descriptor(
    track: LossTrack,
    state: PerfettoTrackState,
    sequence_id: int,
) -> list[bytes]:
    """Build the descriptor of the track *track*'s windows draw on among the
    run rows, once."""
    if state.has_run_track(track):
        return []
    desc = build_track_descriptor(state.get_or_create_run_track_uuid(track), _LOSS_TRACK_NAME)
    return [build_trace_packet(sequence_id, track_descriptor=desc)]


def draw_batch_on_run_rows(
    events: Sequence[TraceEvent],
    annotations: Sequence[list[bytes]],
    state: PerfettoTrackState,
    sequence_id: int,
) -> tuple[list[bytes], list[bytes]]:
    """The run rows' share of a batch: every descriptor its events need and
    the packets drawing them.

    A pause and a loss window carry ``pid`` and ``pid_epoch``, which settle
    whose it is where two processes' spans on ``Processes`` both cover it.
    """
    descriptors: list[bytes] = []
    packets: list[bytes] = []
    for event, encoded in zip(events, annotations, strict=True):
        if not isinstance(event, Slice):
            continue
        track = event.track
        if isinstance(track, PauseTrack):
            descriptors.extend(_emit_generation_track_descriptor(track, state, sequence_id))
            names_its_process = event.cat.startswith(PAUSE.category)
        else:
            descriptors.extend(_emit_loss_track_descriptor(track, state, sequence_id))
            names_its_process = True
        track_uuid = state.get_or_create_run_track_uuid(track)
        if names_its_process:
            encoded = [
                _build_debug_annotation_int(PID, track.process.pid),
                _build_debug_annotation_int(PID_EPOCH, track.process.pid_epoch),
                *encoded,
            ]
        packets.extend(
            _make_slice_packets(sequence_id, track_uuid, event.name, event.cat, event.ts_start, event.ts_stop, encoded)
        )
    return descriptors, packets
