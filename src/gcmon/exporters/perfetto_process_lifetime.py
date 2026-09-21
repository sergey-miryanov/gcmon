"""Everything that draws a process: its row, its name, and its two spans.

The root descriptor that makes process order explicit, one process
descriptor per process, one BEGIN/END pair per process on a track of its own
that the shared ``Processes`` row merges in, and one over the same interval
on the process's own row. See ADR-0011 and ADR-0028, and ADR-0001 for where
this sits among the encoder's layers.
"""

from ..model.names import (
    CMDLINE,
    LOST_COUNT,
    LOST_PAUSE,
    LOST_PAUSE_NS,
    PAUSE,
    PID,
    PID_EPOCH,
    SAMPLED_COUNT,
)
from ..model.process import Process
from ..model.trace_event import LossTrack, Slice, TraceEvent
from .perfetto_builders import (
    _build_debug_annotation_int,
    _build_debug_annotation_string,
    build_trace_packet,
    build_track_descriptor,
    build_track_event,
)
from .perfetto_proto import (
    ChildTracksOrdering,
    ProcessOrdering,
    TrackEventType,
)
from .perfetto_track_state import PerfettoTrackState, ProcessSpan
from .trace_converter import duration_text

__all__ = ["emit_retired_process_row", "finalize_perfetto_packets", "process_track_name"]


# Name every process's lifetime track carries. The trace processor merges
# root-level tracks sharing a name, so these draw as one row.
_PROCESS_LIFETIME_TRACK_NAME: str = "Processes"

# Name of the slice each process's own row carries over the interval gcmon
# observed it (ADR-0010).
_PROCESS_ROW_SLICE_NAME: str = "Lifetime"


# The fixed half of a process row's name. A reader groups on it and a
# test slices it back off, so it is named rather than repeated.
_PROCESS_ROW_PREFIX: str = "Process "


def process_track_name(process: Process) -> str:
    """What *process* is called (ADR-0011)."""
    return f"{_PROCESS_ROW_PREFIX}{process}"


def _emit_root_descriptor(
    state: PerfettoTrackState,
    sequence_id: int,
) -> list[bytes]:
    """Build the root ``TrackDescriptor`` (``uuid = 0``), once per trace.

    It carries no ``name`` and no ``process``, ``thread`` or ``counter``
    sub-message, so the trace processor draws no row for it (ADR-0011).
    """
    if state.has_root_descriptor():
        return []
    state.mark_root_descriptor_emitted()
    desc = build_track_descriptor(
        uuid=0,
        name="",
        process_ordering=ProcessOrdering.EXPLICIT,
    )
    return [build_trace_packet(sequence_id, track_descriptor=desc)]


def _emit_process_descriptor(
    process: Process,
    state: PerfettoTrackState,
    sequence_id: int,
    sibling_order_rank: int | None = None,
    start_timestamp_ns: int | None = None,
) -> list[bytes]:
    """Build a process track descriptor if not already emitted for *process*.

    *sibling_order_rank* orders this process against the other process
    tracks, which the UI honors only when the root descriptor carries
    ``process_ordering = PROCESS_ORDERING_EXPLICIT``; see
    ``_emit_root_descriptor``.

    *start_timestamp_ns* goes on the ``process`` sub-message. It is
    *process*'s first event in nanoseconds, the same timestamp the rank comes
    from.
    """
    if state.has_process_descriptor(process):
        return []
    state.mark_process_descriptor(process)
    proc_uuid = state.get_process_track_uuid(process)
    cmdline = state.get_cmdline(process)
    desc = build_track_descriptor(
        proc_uuid,
        process_track_name(process),
        pid=state.get_row_pid(process),
        child_ordering=ChildTracksOrdering.EXPLICIT,
        sibling_order_rank=sibling_order_rank,
        cmdline=cmdline,
        description=" ".join(cmdline) if cmdline else None,
        start_timestamp_ns=start_timestamp_ns,
    )
    return [build_trace_packet(sequence_id, track_descriptor=desc)]


def _emit_process_lifetime_track_descriptor(
    process: Process,
    state: PerfettoTrackState,
    sequence_id: int,
) -> bytes:
    """Build the descriptor for the track *process* draws its span on.

    It carries the name and nothing else, and every process's carries the
    same one, which is what merges them into one row (ADR-0011).
    """
    assert not state.has_process_lifetime_track(process), (
        f"the Processes track descriptor for {process} has already gone out"
    )
    state.mark_process_lifetime_track(process)
    track_uuid = state.get_process_lifetime_track_uuid(process)
    desc = build_track_descriptor(track_uuid, _PROCESS_LIFETIME_TRACK_NAME)
    return build_trace_packet(sequence_id, track_descriptor=desc)


def _cmdline_annotation(process: Process, state: PerfettoTrackState) -> list[bytes]:
    """The ``cmdline`` annotation both of *process*'s spans carry, or
    nothing where gcmon read no command line for it (ADR-0010)."""
    cmdline = state.get_cmdline(process)
    if not cmdline:
        return []
    return [_build_debug_annotation_string(CMDLINE, " ".join(cmdline))]


def _emit_process_lifetime_slice(
    span: ProcessSpan,
    state: PerfettoTrackState,
    sequence_id: int,
) -> list[bytes]:
    """Emit the ``TYPE_SLICE_BEGIN`` / ``TYPE_SLICE_END`` pair drawing *span*
    on its process's own track, BEGIN first: the trace processor breaks
    timestamp ties by position in the sequence, so a zero-length span with
    its END first reads as ``dur = -1`` (ADR-0011).

    The END carries no name. Its track holds this one span, so there is
    nothing else on it to close."""
    track_uuid = state.get_process_lifetime_track_uuid(span.process)
    debug_annotations = [
        *_cmdline_annotation(span.process, state),
        _build_debug_annotation_int(PID, span.process.pid),
        _build_debug_annotation_int(PID_EPOCH, span.process.pid_epoch),
    ]
    return [
        build_trace_packet(
            sequence_id,
            timestamp=span.start_ts,
            track_event=build_track_event(
                event_type=TrackEventType.SLICE_BEGIN,
                track_uuid=track_uuid,
                name=process_track_name(span.process),
                debug_annotations=debug_annotations,
            ),
        ),
        build_trace_packet(
            sequence_id,
            timestamp=span.end_ts,
            track_event=build_track_event(
                event_type=TrackEventType.SLICE_END,
                track_uuid=track_uuid,
            ),
        ),
    ]


def _emit_process_row_lifetime_slice(
    span: ProcessSpan,
    state: PerfettoTrackState,
    sequence_id: int,
) -> list[bytes]:
    """Emit the ``Lifetime`` pair on *span*'s own process track, which is
    what keeps that track non-empty so the Perfetto UI renders its
    ``description`` (ADR-0010).

    BEGIN first, so a process observed at a single instant reads as
    ``dur = 0`` rather than ``-1``.

    The caller describes *span*'s process first, so the track uuid this
    names is one a packet has described, and checks that the bar has not
    already gone out: this is what records that it has, and a second bar on
    one row would draw the process twice.
    """
    assert state.has_process_descriptor(span.process), (
        "a Lifetime bar needs a described row to draw on; describe the process first"
    )
    assert not state.has_process_row_drawn(span.process), (
        f"{span.process} has already drawn its Lifetime bar; the caller tests that first"
    )
    state.mark_process_row_drawn(span.process)
    track_uuid = state.get_process_track_uuid(span.process)
    lost_pause_ns = state.get_lost_pause_ns(span.process)
    debug_annotations = [
        *_cmdline_annotation(span.process, state),
        _build_debug_annotation_int(PID, span.process.pid),
        _build_debug_annotation_int(PID_EPOCH, span.process.pid_epoch),
        _build_debug_annotation_int("interpreters", state.get_interpreter_count(span.process)),
        _build_debug_annotation_int(SAMPLED_COUNT, state.get_sampled_count(span.process)),
        _build_debug_annotation_int(LOST_COUNT, state.get_lost_count(span.process)),
        _build_debug_annotation_string(LOST_PAUSE, duration_text(lost_pause_ns)),
        _build_debug_annotation_int(LOST_PAUSE_NS, lost_pause_ns),
    ]
    return [
        build_trace_packet(
            sequence_id,
            timestamp=span.start_ts,
            track_event=build_track_event(
                event_type=TrackEventType.SLICE_BEGIN,
                track_uuid=track_uuid,
                name=_PROCESS_ROW_SLICE_NAME,
                debug_annotations=debug_annotations,
            ),
        ),
        build_trace_packet(
            sequence_id,
            timestamp=span.end_ts,
            track_event=build_track_event(
                event_type=TrackEventType.SLICE_END,
                track_uuid=track_uuid,
            ),
        ),
    ]


def emit_retired_process_row(
    process: Process,
    state: PerfettoTrackState,
    sequence_id: int,
) -> list[bytes]:
    """Everything *process* draws, before the end of the run: the root
    descriptor, its process descriptor, its span on the shared ``Processes``
    row and its ``Lifetime`` bar.

    gcmon has let go of the pid, so *process*'s span is final: a record read
    afterwards is filed under whatever holds the pid now (ADR-0025), and
    liveness and RSS both work off the tick's live set. No span is measured
    against another, so a final one needs nothing else in hand (ADR-0011).
    What a run killed mid-flight loses shrinks to the processes still running.
    The Perfetto UI hides a row holding no events, so a bar that never reached
    the file takes its whole row with it, its interpreters' rows and all.

    Returns nothing for a process gcmon never observed, for one already drawn,
    and for a trace whose closeout has gone out.
    """
    if state.has_process_lifetime_emitted() or state.has_process_row_drawn(process):
        return []
    span = state.get_process_lifetime(process)
    if span is None:
        return []
    state.rank_processes([process])
    packets = [
        *_emit_root_descriptor(state, sequence_id),
        *_emit_process_descriptor(
            process,
            state,
            sequence_id,
            sibling_order_rank=state.get_process_track_rank(process),
            start_timestamp_ns=span.start_ts,
        ),
        _emit_process_lifetime_track_descriptor(process, state, sequence_id),
    ]
    packets.extend(_emit_process_lifetime_slice(span, state, sequence_id))
    packets.extend(_emit_process_row_lifetime_slice(span, state, sequence_id))
    return packets


def _record_process_lifetime(
    event: TraceEvent,
    state: PerfettoTrackState,
) -> None:
    """Fold *event* into its pid's recorded ``Processes``-track span.

    Every event widens the span in both directions, counters included: a
    timestamped event is evidence the process existed at that instant,
    whatever kind it is. A slice is evidence at both its ends, so it is
    folded in twice. Emits nothing: spans become packets at close.
    """
    process = event.track.process
    if isinstance(event, Slice):
        state.update_process_lifetime(process, event.ts_start)
        state.update_process_lifetime(process, event.ts_stop)
    else:
        state.update_process_lifetime(process, event.ts)


def _record_capture_totals(
    event: TraceEvent,
    state: PerfettoTrackState,
) -> None:
    """Fold *event* into its process's ``sampled_count`` and loss totals.

    Two of the events a batch carries stand for exactly one thing gcmon
    read: the ``GC Pause`` slice is one record, and a slice on a
    ``LossTrack`` is one poll interval. Every other event is a phase or a
    counter belonging to a record already counted here, and folding those
    in would count phases.

    Counted in the convert pass rather than at ``add_event``, which is where
    the records arrive but which holds no encoder lock and would need a
    second acquisition on the hot path. This runs where the caller already
    holds it, and it is also the one pass a capture read back from JSONL
    goes through, so ``gcmon combine`` fills the same annotations a live run
    does. Emits nothing: the totals become annotations when the bar is
    drawn.
    """
    if not isinstance(event, Slice):
        return
    if isinstance(event.track, LossTrack):
        # Read off the args `convert_loss_to_trace_format` just built, which
        # is where the per-interval sums already are. It writes both
        # unconditionally and as ints, so anything else is a bug rather than
        # a shape to tolerate.
        lost_count = event.args[LOST_COUNT]
        lost_pause_ns = event.args[LOST_PAUSE_NS]
        assert isinstance(lost_count, int) and isinstance(lost_pause_ns, int), (
            f"a GC Loss slice must carry integer totals, got {lost_count!r} and {lost_pause_ns!r}"
        )
        state.record_loss(event.track.process, lost_count, lost_pause_ns)
    elif event.cat.startswith(PAUSE.category):
        state.record_sampled(event.track.process)


def finalize_perfetto_packets(
    state: PerfettoTrackState,
    sequence_id: int,
) -> list[bytes]:
    """Emit every descriptor and span packet the end of the trace owes:
    the root descriptor, then per process still held a descriptor for its
    row where it has none, a descriptor for the track it draws its span on,
    that span, and its ``Lifetime`` bar on its own row. Call this once, at
    the end of the trace (typically the encoder's ``close()``).

    A process gcmon retired during the run drew both of its slices then
    (``emit_retired_process_row``), so this pass skips it.

    Every process still held gets both, including one the monitor loop
    only ever reported as live. Describing that one here is what puts it
    on the timeline at all: no event ever named its track, so no convert
    pass described it, and it reached the file as a slice on the shared
    row and nothing else. Its descriptor is as complete as any other,
    since gcmon reads a command line for every process it creates.

    No span is dropped: a pid observed at a single instant still gets a
    zero-duration slice. The spans go out in ascending
    ``(start_ts, process)``, which is the order the ranking hands out
    ranks in and is what keeps these bytes independent of the order the
    events arrived in. See ADR-0011.

    Safe to call with no spans, and safe to call twice; both return an
    empty list. The ``_process_lifetime_emitted`` flag on *state* guards
    the second call, and it guards every track it describes: a descriptor
    is not idempotent on its own.
    """
    if state.has_process_lifetime_emitted():
        return []
    recorded = state.get_process_lifetimes()
    if not recorded:
        return []
    state.mark_process_lifetime_emitted()
    spans = sorted(
        (span for span in recorded if not state.has_process_row_drawn(span.process)),
        key=lambda span: (span.start_ts, span.process),
    )

    state.rank_processes(span.process for span in spans)
    descriptors = _emit_root_descriptor(state, sequence_id)
    for span in spans:
        descriptors.extend(
            _emit_process_descriptor(
                span.process,
                state,
                sequence_id,
                sibling_order_rank=state.get_process_track_rank(span.process),
                start_timestamp_ns=span.start_ts,
            )
        )
        descriptors.append(_emit_process_lifetime_track_descriptor(span.process, state, sequence_id))

    packets: list[bytes] = []
    for span in spans:
        packets.extend(_emit_process_lifetime_slice(span, state, sequence_id))
        packets.extend(_emit_process_row_lifetime_slice(span, state, sequence_id))

    return [*descriptors, *packets]
