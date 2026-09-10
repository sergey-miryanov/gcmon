"""Everything that draws a process: its row, its name, and its two spans.

The root descriptor that makes process order explicit, one process
descriptor per process, one BEGIN/END pair per process on the shared
``Processes`` track clipped laminar, and one over the observed interval on
the process's own row. See ADR-0011.
"""

from typing import NamedTuple

from ..model.process import Process
from ..model.trace_event import LossTrack, Slice, TraceEvent
from .perfetto_builders import (
    _build_debug_annotation_bool,
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
from .trace_converter import GC_PAUSE_CATEGORY, duration_text

__all__ = ["emit_retired_process_row", "finalize_perfetto_packets", "process_track_name"]


class ClippedSpan(NamedTuple):
    """A :class:`ProcessSpan` as the slice draws it, and as it was
    observed.

    Clipping moves ``start_ts`` / ``end_ts`` and leaves ``real_start_ts``
    / ``real_end_ts`` alone (ADR-0011).
    """

    process: Process
    start_ts: int
    end_ts: int
    real_start_ts: int
    real_end_ts: int


# Name of the shared top-level Perfetto track that shows one
# TYPE_SLICE_BEGIN / TYPE_SLICE_END pair per process.
_PROCESS_LIFETIME_TRACK_NAME: str = "Processes"

# Name of the slice each process's own row carries over the interval gcmon
# observed it (ADR-0010).
_PROCESS_ROW_SLICE_NAME: str = "Lifetime"


def process_track_name(process: Process) -> str:
    """What *process* is called (ADR-0011)."""
    return f"Process {process}"


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
    state: PerfettoTrackState,
    sequence_id: int,
) -> bytes:
    """Build the shared ``Processes`` track descriptor."""
    assert not state.has_process_lifetime_emitted(), (
        "the Processes track descriptor has already gone out for this trace"
    )
    track_uuid = state.get_or_create_process_lifetime_track_uuid()
    desc = build_track_descriptor(track_uuid, _PROCESS_LIFETIME_TRACK_NAME)
    return build_trace_packet(sequence_id, track_descriptor=desc)


def _cmdline_annotation(process: Process, state: PerfettoTrackState) -> list[bytes]:
    """The ``cmdline`` annotation both of *process*'s spans carry, or
    nothing where gcmon read no command line for it (ADR-0010)."""
    cmdline = state.get_cmdline(process)
    if not cmdline:
        return []
    return [_build_debug_annotation_string("cmdline", " ".join(cmdline))]


def _emit_process_lifetime_slice(
    span: ClippedSpan,
    state: PerfettoTrackState,
    sequence_id: int,
) -> list[bytes]:
    """Emit the ``TYPE_SLICE_BEGIN`` / ``TYPE_SLICE_END`` pair drawing *span*,
    BEGIN first: the trace processor breaks timestamp ties by position in
    the sequence, so a zero-length span with its END first reads as
    ``dur = -1``.

    The END repeats the name: matching is by name, so two spans on one pid
    would otherwise close each other (ADR-0011)."""
    track_uuid = state.get_or_create_process_lifetime_track_uuid()
    name = process_track_name(span.process)
    debug_annotations = [
        *_cmdline_annotation(span.process, state),
        _build_debug_annotation_int("pid", span.process.pid),
        _build_debug_annotation_int("pid_epoch", span.process.pid_epoch),
        _build_debug_annotation_int("real_start_ts", span.real_start_ts),
        _build_debug_annotation_int("real_end_ts", span.real_end_ts),
        # On the slice the sweep decides rather than on the bar, which a
        # retired process writes before the sweep has decided anything.
        _build_debug_annotation_bool("clipped", span.end_ts != span.real_end_ts),
    ]
    return [
        build_trace_packet(
            sequence_id,
            timestamp=span.start_ts,
            track_event=build_track_event(
                type=TrackEventType.SLICE_BEGIN,
                track_uuid=track_uuid,
                name=name,
                debug_annotations=debug_annotations,
            ),
        ),
        build_trace_packet(
            sequence_id,
            timestamp=span.end_ts,
            track_event=build_track_event(
                type=TrackEventType.SLICE_END,
                track_uuid=track_uuid,
                name=name,
            ),
        ),
    ]


def _emit_process_row_lifetime_slice(
    span: ClippedSpan,
    state: PerfettoTrackState,
    sequence_id: int,
) -> list[bytes]:
    """Emit the ``Lifetime`` pair on *span*'s own process track, which is
    what keeps that track non-empty so the Perfetto UI renders its
    ``description`` (ADR-0010).

    Drawn over the observed pair rather than the clipped one: clipping
    keeps the *shared* track's slice stack laminar, and this row holds one
    slice and the workload's ``Instant`` marks, which nest without closing
    anything (ADR-0011). BEGIN first, so a process observed at a single
    instant reads as ``dur = 0`` rather than ``-1``.

    The caller describes *span*'s process first, so the track uuid this
    names is one a packet has described.

    Returns nothing for a process whose bar has already gone out, which is
    every process gcmon retired before the end of the run.
    """
    assert state.has_process_descriptor(span.process), (
        "a Lifetime bar needs a described row to draw on; describe the process first"
    )
    if state.has_process_row_drawn(span.process):
        return []
    state.mark_process_row_drawn(span.process)
    track_uuid = state.get_process_track_uuid(span.process)
    lost_pause_ns = state.get_lost_pause_ns(span.process)
    debug_annotations = [
        *_cmdline_annotation(span.process, state),
        _build_debug_annotation_int("pid", span.process.pid),
        _build_debug_annotation_int("pid_epoch", span.process.pid_epoch),
        _build_debug_annotation_int("interpreters", state.get_interpreter_count(span.process)),
        _build_debug_annotation_int("sampled_count", state.get_sampled_count(span.process)),
        _build_debug_annotation_int("lost_count", state.get_lost_count(span.process)),
        _build_debug_annotation_string("lost_pause", duration_text(lost_pause_ns)),
        _build_debug_annotation_int("lost_pause_ns", lost_pause_ns),
    ]
    return [
        build_trace_packet(
            sequence_id,
            timestamp=span.real_start_ts,
            track_event=build_track_event(
                type=TrackEventType.SLICE_BEGIN,
                track_uuid=track_uuid,
                name=_PROCESS_ROW_SLICE_NAME,
                debug_annotations=debug_annotations,
            ),
        ),
        build_trace_packet(
            sequence_id,
            timestamp=span.real_end_ts,
            track_event=build_track_event(
                type=TrackEventType.SLICE_END,
                track_uuid=track_uuid,
            ),
        ),
    ]


def emit_retired_process_row(
    process: Process,
    state: PerfettoTrackState,
    sequence_id: int,
) -> list[bytes]:
    """Everything *process*'s own row needs, before the end of the run: the
    root descriptor, its process descriptor and its ``Lifetime`` bar.

    gcmon has let go of the pid, so *process*'s span is final: a record read
    afterwards is filed under whatever holds the pid now (ADR-0025), and
    liveness and RSS both work off the tick's live set. What a run killed
    mid-flight loses shrinks to the processes still running. The Perfetto UI
    hides a row holding no events, so a bar that never reached the file takes
    its whole row with it, thread tracks and all.

    The ``Processes`` slice does not come with it. That one is clipped against
    its siblings and the sweep is global, so it waits for close; a process
    discovered later can still open a span inside this one (ADR-0011).

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
    ]
    drawn = ClippedSpan(process, span.start_ts, span.end_ts, span.start_ts, span.end_ts)
    packets.extend(_emit_process_row_lifetime_slice(drawn, state, sequence_id))
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
        lost_count = event.args["lost_count"]
        lost_pause_ns = event.args["lost_pause_ns"]
        assert isinstance(lost_count, int) and isinstance(lost_pause_ns, int), (
            f"a GC Loss slice must carry integer totals, got {lost_count!r} and {lost_pause_ns!r}"
        )
        state.record_loss(event.track.process, lost_count, lost_pause_ns)
    elif event.cat.startswith(GC_PAUSE_CATEGORY):
        state.record_sampled(event.track.process)


def _clip_spans_to_laminar(spans: list[ProcessSpan]) -> list[ClippedSpan]:
    """Clip *spans* so any two are disjoint or strictly nested, sorted by
    ascending start, longer span first on a tie, then process. The drawn
    pair moves; the observed pair is carried through untouched. See
    ADR-0011.
    """
    spans = sorted(spans, key=lambda span: (span.start_ts, -span.end_ts, span.process))
    ends: dict[Process, int] = {}
    open_processes: list[Process] = []
    for process, start, end in spans:
        # Walk out through the spans still open at *start*, closing the
        # ones that ended before it and clipping the ones it crosses.
        # Only a span that contains this one stops the walk.
        while open_processes:
            outer = open_processes[-1]
            outer_end = ends[outer]
            if outer_end < start:
                open_processes.pop()
                continue
            if outer_end >= end:
                break
            ends[outer] = start - 1
            open_processes.pop()
        ends[process] = end
        open_processes.append(process)
    return [ClippedSpan(process, start, ends[process], start, end) for process, start, end in spans]


def finalize_perfetto_packets(
    state: PerfettoTrackState,
    sequence_id: int,
) -> list[bytes]:
    """Emit every descriptor and span packet the end of the trace owes:
    the root descriptor, a process descriptor for each process still
    without one, the ``Processes`` track descriptor, then per process its
    clipped slice on that track and its ``Lifetime`` bar on its own row.
    Call this once, at the end of the trace (typically the encoder's
    ``close()``).

    Every process with a span gets both, including one the monitor loop
    only ever reported as live. Describing that one here is what puts it
    on the timeline at all: no event ever named its track, so no convert
    pass described it, and it reached the file as a slice on the shared
    row and nothing else. Its descriptor is as complete as any other,
    since gcmon reads a command line for every process it creates.

    No span is dropped: a pid observed at a single instant, or clipped to
    zero, still gets a zero-duration slice. Slices go out in the order
    ``_clip_spans_to_laminar`` returns. See ADR-0011.

    Safe to call with no spans, and safe to call twice; both return an
    empty list. The ``_process_lifetime_emitted`` flag on *state* guards
    the second call, and it guards the whole track: the descriptor is not
    idempotent on its own.
    """
    if state.has_process_lifetime_emitted():
        return []
    spans = state.get_process_lifetimes()
    if not spans:
        return []

    clipped = _clip_spans_to_laminar(spans)
    state.rank_processes(span.process for span in clipped)
    descriptors = _emit_root_descriptor(state, sequence_id)
    for span in clipped:
        descriptors.extend(
            _emit_process_descriptor(
                span.process,
                state,
                sequence_id,
                sibling_order_rank=state.get_process_track_rank(span.process),
                start_timestamp_ns=state.get_process_lifetime_start_ts(span.process),
            )
        )
    descriptors.append(_emit_process_lifetime_track_descriptor(state, sequence_id))

    packets: list[bytes] = []
    for span in clipped:
        packets.extend(_emit_process_lifetime_slice(span, state, sequence_id))
        packets.extend(_emit_process_row_lifetime_slice(span, state, sequence_id))

    state.mark_process_lifetime_emitted()
    return [*descriptors, *packets]
