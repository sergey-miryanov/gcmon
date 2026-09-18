"""Tests for the shared ``Processes`` track: spans, clipping, closeout.

The clipping sweep is covered twice over: directly as a pure function,
and through ``finalize_perfetto_packets``, where the emitted BEGIN/END
pairs also have to be ones the trace processor can pair up. See ADR-0011.
"""

import random

import pytest
from perfetto.protos.perfetto.trace.perfetto_trace_pb2 import (
    TracePacket,
    TrackDescriptor,
    TrackEvent,
)

from gcmon.exporters.perfetto_format import convert_trace_events_to_perfetto
from gcmon.exporters.perfetto_process_lifetime import (
    _PROCESS_LIFETIME_TRACK_NAME,
    _PROCESS_ROW_PREFIX,
    _PROCESS_ROW_SLICE_NAME,
    _clip_spans_to_laminar,
    _emit_process_lifetime_track_descriptor,
    emit_retired_process_row,
    finalize_perfetto_packets,
    process_track_name,
)
from gcmon.exporters.perfetto_proto import ProcessOrdering, TrackEventType
from gcmon.exporters.perfetto_track_state import PerfettoTrackState, ProcessSpan
from gcmon.exporters.trace_converter import (
    convert_item_to_trace_format,
    convert_loss_to_trace_format,
    duration_text,
)
from gcmon.model.data import GCStatsInfo
from gcmon.model.names import (
    CLIPPED,
    CMDLINE,
    LOST_COUNT,
    LOST_PAUSE,
    LOST_PAUSE_NS,
    PID,
    PID_EPOCH,
    REAL_END_TS,
    REAL_START_TS,
    SAMPLED_COUNT,
)
from gcmon.model.process import Process
from gcmon.model.trace_event import TraceEvent
from tests.exporters.perfetto_helpers import (
    clipped_span,
    convert_item,
    convert_items,
    lifetime_slices,
    parse_track_descriptor,
    pause_item,
    span,
)
from tests.helpers import (
    create_mock_incremental_item,
    create_mock_loss_item,
    create_mock_stats_item,
    proc,
)

TARGET_PID: int = 100
OTHER_PID: int = 200
THIRD_PID: int = 300

# The ``Processes`` row a pid draws. A descriptor carries it and an
# assertion reads it back, so both go through one spelling.
TARGET_ROW_NAME: str = process_track_name(proc(TARGET_PID))
OTHER_ROW_NAME: str = process_track_name(proc(OTHER_PID))


def _process_descriptors(packets: list[bytes]) -> dict[str, TrackDescriptor]:
    """``{track name: descriptor}`` for every process descriptor in *packets*."""
    out: dict[str, TrackDescriptor] = {}
    for raw in packets:
        descriptor = parse_track_descriptor(raw)
        if descriptor is not None and descriptor.HasField("process"):
            out[descriptor.name] = descriptor
    return out


def _row_slices(packets: list[bytes], row_uuid: int) -> list[tuple[int, int, str]]:
    """``[(ts, type, name), ...]`` for the slice events on one process's own
    row, in packet order."""
    out: list[tuple[int, int, str]] = []
    for raw in packets:
        packet = TracePacket()
        packet.ParseFromString(raw)
        if not packet.HasField("track_event"):
            continue
        event = packet.track_event
        if event.track_uuid != row_uuid:
            continue
        if event.type not in (TrackEvent.Type.TYPE_SLICE_BEGIN, TrackEvent.Type.TYPE_SLICE_END):
            continue
        out.append((packet.timestamp, event.type, event.name))
    return out


def _finalize_spans(
    spans: list[ProcessSpan],
) -> tuple[dict[int, tuple[int, int]], dict[int, tuple[int, int]]]:
    """Run *spans* through
    ``finalize_perfetto_packets`` and decode the result.

    Returns ``({pid: (ts, end_ts)}, {pid: (real_start_ts, real_end_ts)})``:
    the span each slice *draws*, and the span it *records*. Every pid
    with a span appears in both -- nothing is ever dropped -- so the two
    dicts always have the same keys.

    Also asserts the emitted order is one the trace processor can pair
    up: each BEGIN is immediately followed by its own END, and of two
    BEGINs on one timestamp the first outlives the second. Getting the
    second wrong corrupts both slices, since a named END force-closes
    whatever sits above the slice it matched.
    """
    state = PerfettoTrackState()
    for one in spans:
        state.update_process_lifetime(one.process, one.start_ts)
        state.update_process_lifetime(one.process, one.end_ts)
    packets = finalize_perfetto_packets(state, sequence_id=1)
    lifetime_uuid = state.get_or_create_process_lifetime_track_uuid()

    emitted = lifetime_slices(packets, lifetime_uuid)
    intervals: dict[int, tuple[int, int]] = {}
    real: dict[int, tuple[int, int]] = {}
    open_slice: tuple[str, int] | None = None
    for ts, event_type, name, annotations in emitted:
        if event_type == TrackEventType.SLICE_BEGIN:
            assert open_slice is None, f"BEGIN for {name!r} while {open_slice} is still open"
            open_slice = (name, ts)
            real[int(name.removeprefix(_PROCESS_ROW_PREFIX))] = (
                int(annotations[REAL_START_TS]),
                int(annotations[REAL_END_TS]),
            )
        else:
            assert open_slice is not None, f"slice END for {name!r} at ts {ts} with nothing open"
            open_name, open_ts = open_slice
            assert open_name == name, f"slice END for {name!r} closed {open_name!r}"
            intervals[int(name.removeprefix(_PROCESS_ROW_PREFIX))] = (open_ts, ts)
            open_slice = None
    assert open_slice is None, f"unclosed slice left open: {open_slice}"
    assert intervals.keys() == real.keys()

    begins = [
        (ts, int(name.removeprefix(_PROCESS_ROW_PREFIX)))
        for ts, t, name, _ in emitted
        if t == TrackEventType.SLICE_BEGIN
    ]
    for i, (ts, pid) in enumerate(begins):
        for later_ts, later_pid in begins[i + 1 :]:
            if later_ts != ts:
                continue
            assert intervals[pid][1] >= intervals[later_pid][1], (
                f"pids {pid} and {later_pid} share start {ts}, but {pid} is emitted first and "
                f"ends at {intervals[pid][1]}, inside {later_pid}'s end {intervals[later_pid][1]}: "
                "the outer BEGIN has to come first or the nesting opens inside-out"
            )
    return intervals, real


def _assert_laminar(intervals: dict[int, tuple[int, int]]) -> None:
    """Assert that no two intervals cross: each pair is either disjoint
    or one strictly contains the other."""
    items = sorted(intervals.items(), key=lambda kv: kv[1])
    for i, (pid_a, (start_a, end_a)) in enumerate(items):
        for pid_b, (start_b, end_b) in items[i + 1 :]:
            disjoint = end_a < start_b or end_b < start_a
            nested = (start_a <= start_b and end_b <= end_a) or (start_b <= start_a and end_a <= end_b)
            assert disjoint or nested, f"pids {pid_a} [{start_a}, {end_a}] and {pid_b} [{start_b}, {end_b}] cross"


class TestClipSpansToLaminar:
    """Direct tests for the ``_clip_spans_to_laminar`` sweep.

    It is a pure function, so it can be exercised without building
    packets. ``TestProcessLifetimeLaminarClipping`` below covers the same
    ground through ``finalize_perfetto_packets`` and additionally checks
    the emitted packets are ones the trace processor can pair up; these
    tests pin the sweep's own contract, including the parts the packet
    view cannot see, such as output ordering.
    """

    def test_no_spans(self) -> None:
        assert _clip_spans_to_laminar([]) == []

    def test_single_span_is_unchanged(self) -> None:
        assert _clip_spans_to_laminar([span(TARGET_PID, 500, 9_000)]) == [
            clipped_span(TARGET_PID, 500, 9_000, 500, 9_000)
        ]

    def test_disjoint_spans_are_unchanged(self) -> None:
        """The first span has closed before the second opens, so the
        sweep pops it and clips nothing."""
        spans = [span(TARGET_PID, 500, 1_000), span(OTHER_PID, 5_000, 9_000)]

        assert _clip_spans_to_laminar(spans) == [
            clipped_span(TARGET_PID, 500, 1_000, 500, 1_000),
            clipped_span(OTHER_PID, 5_000, 9_000, 5_000, 9_000),
        ]

    def test_nested_span_is_unchanged(self) -> None:
        """A span contained by the one still open stops the walk, which
        is the common shape of a parent outliving its child."""
        spans = [span(TARGET_PID, 500, 9_000), span(OTHER_PID, 1_000, 5_000)]

        assert _clip_spans_to_laminar(spans) == [
            clipped_span(TARGET_PID, 500, 9_000, 500, 9_000),
            clipped_span(OTHER_PID, 1_000, 5_000, 1_000, 5_000),
        ]

    def test_crossing_clips_the_outer_end(self) -> None:
        """The whole point: the earlier span's end is pulled back to one
        nanosecond before the later one starts."""
        spans = [span(TARGET_PID, 500, 1_500), span(OTHER_PID, 1_000, 5_000)]

        assert _clip_spans_to_laminar(spans) == [
            clipped_span(TARGET_PID, 500, 999, 500, 1_500),
            clipped_span(OTHER_PID, 1_000, 5_000, 1_000, 5_000),
        ]

    def test_touching_is_treated_as_crossing(self) -> None:
        """``A.end == B.start`` is clipped too: the relative order of an
        END and a BEGIN sharing a timestamp is not ours to control."""
        spans = [span(TARGET_PID, 500, 1_000), span(OTHER_PID, 1_000, 5_000)]

        assert _clip_spans_to_laminar(spans) == [
            clipped_span(TARGET_PID, 500, 999, 500, 1_000),
            clipped_span(OTHER_PID, 1_000, 5_000, 1_000, 5_000),
        ]

    def test_equal_starts_always_nest(self) -> None:
        """The sweep's own sort puts equal starts longest-first, so they
        can never cross, which is what keeps ``start - 1`` from landing
        before the clipped span's own start."""
        spans = [span(TARGET_PID, 500, 1_000), span(OTHER_PID, 500, 9_000)]

        assert _clip_spans_to_laminar(spans) == [
            clipped_span(OTHER_PID, 500, 9_000, 500, 9_000),
            clipped_span(TARGET_PID, 500, 1_000, 500, 1_000),
        ]

    def test_walk_pops_every_span_already_closed(self) -> None:
        """Two spans are open and nested when a third starts after both
        have ended; the sweep unwinds the whole stack in one walk and
        clips neither."""
        spans = [span(TARGET_PID, 0, 100), span(OTHER_PID, 10, 20), span(THIRD_PID, 200, 300)]

        assert _clip_spans_to_laminar(spans) == [
            clipped_span(TARGET_PID, 0, 100, 0, 100),
            clipped_span(OTHER_PID, 10, 20, 10, 20),
            clipped_span(THIRD_PID, 200, 300, 200, 300),
        ]

    def test_one_span_crossed_by_two_later_spans(self) -> None:
        """The sweep is not a pairwise check of neighbours. Pid 200 nests
        inside pid 100, so comparing only adjacent spans would stop there
        and never notice that pid 300 crosses pid 100."""
        spans = [span(TARGET_PID, 500, 5_000), span(OTHER_PID, 1_000, 2_000), span(THIRD_PID, 3_000, 9_000)]

        assert _clip_spans_to_laminar(spans) == [
            clipped_span(TARGET_PID, 500, 2_999, 500, 5_000),
            clipped_span(OTHER_PID, 1_000, 2_000, 1_000, 2_000),
            clipped_span(THIRD_PID, 3_000, 9_000, 3_000, 9_000),
        ]

    def test_chain_of_crossings_clips_each_in_turn(self) -> None:
        spans = [span(TARGET_PID, 0, 100), span(OTHER_PID, 10, 200), span(THIRD_PID, 20, 300)]

        assert _clip_spans_to_laminar(spans) == [
            clipped_span(TARGET_PID, 0, 9, 0, 100),
            clipped_span(OTHER_PID, 10, 19, 10, 200),
            clipped_span(THIRD_PID, 20, 300, 20, 300),
        ]

    def test_clip_can_reduce_a_span_to_zero_length(self) -> None:
        """A crossing span starting one nanosecond later leaves nothing
        to draw. The span is still returned -- dropping it is the
        caller's decision, and the caller does not make it."""
        spans = [span(TARGET_PID, 500, 5_000), span(OTHER_PID, 501, 9_000)]

        assert _clip_spans_to_laminar(spans) == [
            clipped_span(TARGET_PID, 500, 500, 500, 5_000),
            clipped_span(OTHER_PID, 501, 9_000, 501, 9_000),
        ]

    def test_zero_length_input_survives(self) -> None:
        """A pid observed at a single instant arrives zero-length and is
        passed through, not discarded."""
        spans = [span(TARGET_PID, 500, 500), span(OTHER_PID, 500, 9_000)]

        assert _clip_spans_to_laminar(spans) == [
            clipped_span(OTHER_PID, 500, 9_000, 500, 9_000),
            clipped_span(TARGET_PID, 500, 500, 500, 500),
        ]

    @pytest.mark.parametrize("order", [(2, 0, 1), (2, 1, 0), (0, 1, 2)])
    def test_output_is_sorted_whatever_the_input_order(self, order: tuple[int, int, int]) -> None:
        """The result comes back in the sweep's own sort order, not the
        caller's, so ``finalize_perfetto_packets`` can emit it as given."""
        spans = [span(TARGET_PID, 0, 100), span(OTHER_PID, 10, 200), span(THIRD_PID, 20, 300)]

        swept = _clip_spans_to_laminar([spans[i] for i in order])

        assert [row.process.pid for row in swept] == [100, 200, 300]

    @pytest.mark.parametrize("seed", range(50))
    def test_invariants_hold_for_random_spans(self, seed: int) -> None:
        """Whatever goes in, in whatever order: the result is laminar and
        sorted, and every span survives. The per-span invariants are
        named by the assertions."""
        rng = random.Random(seed)
        spans = [
            span(pid, start, start + rng.randrange(0, 2_000))
            for pid in range(100, 100 + rng.randint(2, 12))
            for start in (rng.randrange(0, 2_000),)
        ]
        rng.shuffle(spans)

        clipped = _clip_spans_to_laminar(spans)

        originals = {s.process: (s.start_ts, s.end_ts) for s in spans}
        assert {row.process for row in clipped} == originals.keys(), "every span survives"
        assert clipped == sorted(clipped, key=lambda row: (row.start_ts, -row.real_end_ts, row.process)), (
            "the sweep sorts its own input, so the result comes back in that order"
        )
        for row in clipped:
            assert (row.real_start_ts, row.real_end_ts) == originals[row.process], "the observed span is passed through"
            assert row.start_ts == row.real_start_ts, "a start is never moved"
            assert row.real_start_ts <= row.end_ts <= row.real_end_ts, "an end only ever moves inwards"
        _assert_laminar({row.process.pid: (row.start_ts, row.end_ts) for row in clipped})


class TestProcessLifetimeLaminarClipping:
    """``finalize_perfetto_packets`` clips spans so that the shared
    ``Processes`` track only ever holds disjoint or strictly nested
    slices. Slices on one Perfetto track are a stack, so a crossing pair
    cannot be expressed: the trace processor closes both at the earlier
    END and discards the later one as a ``misplaced_end_event``."""

    def test_crossing_clips_the_earlier_end(self) -> None:
        intervals, real = _finalize_spans([span(TARGET_PID, 500, 1_500), span(OTHER_PID, 1_000, 5_000)])

        assert intervals == {100: (500, 999), 200: (1_000, 5_000)}
        assert real == {100: (500, 1_500), 200: (1_000, 5_000)}
        _assert_laminar(intervals)

    def test_containment_is_left_alone(self) -> None:
        """A parent outliving its child nests correctly, so the common
        multi-process shape costs nothing."""
        intervals, real = _finalize_spans([span(TARGET_PID, 500, 9_000), span(OTHER_PID, 1_000, 5_000)])

        assert intervals == {100: (500, 9_000), 200: (1_000, 5_000)}
        assert real == intervals
        _assert_laminar(intervals)

    def test_disjoint_is_left_alone(self) -> None:
        intervals, real = _finalize_spans([span(TARGET_PID, 500, 1_000), span(OTHER_PID, 5_000, 9_000)])

        assert intervals == {100: (500, 1_000), 200: (5_000, 9_000)}
        assert real == intervals

    def test_touching_counts_as_crossing(self) -> None:
        """``A.end == B.start`` is clipped too: the relative order of an
        END and a BEGIN sharing a timestamp is not ours to control."""
        intervals, real = _finalize_spans([span(TARGET_PID, 500, 1_000), span(OTHER_PID, 1_000, 5_000)])

        assert intervals == {100: (500, 999), 200: (1_000, 5_000)}
        assert real == {100: (500, 1_000), 200: (1_000, 5_000)}

    def test_equal_starts_nest_longest_first(self) -> None:
        """Spans sharing a start can never cross, so none is clipped."""
        intervals, real = _finalize_spans([span(TARGET_PID, 500, 1_000), span(OTHER_PID, 500, 9_000)])

        assert intervals == {200: (500, 9_000), 100: (500, 1_000)}
        assert real == intervals
        _assert_laminar(intervals)

    def test_one_span_crossed_by_two_later_spans(self) -> None:
        """The clip is a sweep, not a pairwise comparison of neighbours.

        Pid 100 spans everything below. Pid 200 nests inside it, so a
        check that only compared each span with the next one would stop
        there and never notice that pid 300 crosses pid 100.
        """
        intervals, real = _finalize_spans(
            [span(TARGET_PID, 500, 5_000), span(OTHER_PID, 1_000, 2_000), span(THIRD_PID, 3_000, 9_000)],
        )

        assert intervals == {100: (500, 2_999), 200: (1_000, 2_000), 300: (3_000, 9_000)}
        assert real == {100: (500, 5_000), 200: (1_000, 2_000), 300: (3_000, 9_000)}
        _assert_laminar(intervals)

    def test_single_instant_span_is_still_drawn(self) -> None:
        """A pid observed at a single instant gets a zero-duration slice
        rather than nothing. It is the only place the track records that
        the process existed, and omission is the one distortion a reader
        has no way to notice."""
        intervals, real = _finalize_spans([span(TARGET_PID, 500, 500)])

        assert intervals == {100: (500, 500)}
        assert real == {100: (500, 500)}

    def test_span_clipped_to_zero_is_still_drawn(self) -> None:
        """Pid 100 is clipped to ``[500, 500]`` by pid 200 starting one
        nanosecond later. Nothing is left to draw, but the slice is
        emitted anyway and its annotations still carry the real 4.5us
        span."""
        intervals, real = _finalize_spans([span(TARGET_PID, 500, 5_000), span(OTHER_PID, 501, 9_000)])

        assert intervals == {100: (500, 500), 200: (501, 9_000)}
        assert real == {100: (500, 5_000), 200: (501, 9_000)}
        _assert_laminar(intervals)

    def test_no_spans_emits_nothing(self) -> None:
        assert _finalize_spans([]) == ({}, {})

    def test_undescribed_pid_without_a_cmdline_still_gets_a_slice(self, state: PerfettoTrackState) -> None:
        """A span is drawn for a pid that never reached ``mark_process_descriptor``
        -- one polled OK for a whole run that never collected, so it named no
        track and no convert pass described it. gcmon read no command line for
        this one, so its slice carries only ``pid_epoch`` and the ``real_*``
        annotations. Its own row is ``TestAQuietProcessGetsARow``'s subject."""
        state.update_process_lifetime(proc(TARGET_PID), 500)
        state.update_process_lifetime(proc(TARGET_PID), 5_000)
        assert not state.has_process_descriptor(proc(TARGET_PID))
        lifetime_uuid = state.get_or_create_process_lifetime_track_uuid()

        packets = finalize_perfetto_packets(state, sequence_id=1)

        assert lifetime_slices(packets, lifetime_uuid) == [
            (
                500,
                TrackEventType.SLICE_BEGIN,
                TARGET_ROW_NAME,
                {PID: 100, PID_EPOCH: 1, REAL_START_TS: 500, REAL_END_TS: 5_000, CLIPPED: False},
            ),
            (5_000, TrackEventType.SLICE_END, TARGET_ROW_NAME, {}),
        ]

    def test_descriptor_refuses_a_second_emission(self, state: PerfettoTrackState) -> None:
        """The descriptor emitter asserts rather than trusting its
        caller: two descriptors for one uuid are accepted silently by the
        trace processor, so nothing downstream would report it."""
        state.mark_process_lifetime_emitted()

        with pytest.raises(AssertionError, match="already gone out"):
            _emit_process_lifetime_track_descriptor(state, sequence_id=1)

    def test_track_is_marked_emitted_only_after_a_slice_goes_out(self, state: PerfettoTrackState) -> None:
        """A trace with no drawable span emits nothing and leaves the
        flag clear, so the guard cannot swallow a later real closeout."""
        assert finalize_perfetto_packets(state, sequence_id=1) == []
        assert not state.has_process_lifetime_emitted()

        state.update_process_lifetime(proc(TARGET_PID), 500)
        state.update_process_lifetime(proc(TARGET_PID), 5_000)

        assert finalize_perfetto_packets(state, sequence_id=1) != []
        assert state.has_process_lifetime_emitted()

    @pytest.mark.parametrize("seed", range(25))
    def test_output_is_always_laminar(self, seed: int) -> None:
        """Whatever spans go in, no two slices come out crossing, every
        BEGIN is closed by its own END, every pid keeps a slice, and
        every slice reports its observed span truthfully."""
        rng = random.Random(seed)
        spans: list[ProcessSpan] = []
        for pid in range(100, 100 + rng.randint(2, 12)):
            start = rng.randrange(0, 2_000)
            spans.append(span(pid, start, start + rng.randrange(0, 2_000)))

        intervals, real = _finalize_spans(spans)

        _assert_laminar(intervals)
        assert intervals.keys() == {one.process.pid for one in spans}, "no pid is ever dropped"
        for pid, (start_ts, end_ts) in intervals.items():
            original = next((one.start_ts, one.end_ts) for one in spans if one.process.pid == pid)
            assert real[pid] == original, "the recorded span is the observed one"
            assert start_ts == original[0], "a span's start is never moved"
            assert start_ts <= end_ts <= original[1], "an end is only ever pulled in"


class TestProcessLifetimeSlices:
    """The ``Processes`` track as emitted through a full convert plus
    finalize pass: one BEGIN/END pair per pid on one shared track.
    """

    def test_process_lifetime_track_emitted_once(self, state: PerfettoTrackState) -> None:
        """The ``Processes`` track descriptor is emitted at most
        once for a single pid, even across multiple convert passes."""
        item = pause_item()

        descriptors, convert_packets, closeout = convert_items(
            [
                (proc(TARGET_PID), item),
                (
                    proc(TARGET_PID),
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
                ),
            ],
            state,
            sequence_id=1,
        )

        lifetime_uuid = state.get_or_create_process_lifetime_track_uuid()
        # The convert passes emit no "Processes" descriptor at all; the
        # track is described once, at closeout, alongside its slices.
        seen = 0
        for desc_bytes in (*descriptors, *convert_packets, *closeout):
            packet = TracePacket()
            packet.ParseFromString(desc_bytes)
            if packet.HasField("track_descriptor"):
                td = packet.track_descriptor
                if td.uuid == lifetime_uuid:
                    assert td.name == _PROCESS_LIFETIME_TRACK_NAME
                    # The descriptor carries no parent_uuid (root), no
                    # process, no thread, no counter, no child_ordering,
                    # no sibling_order_rank, no description.
                    assert not td.HasField("parent_uuid")
                    assert not td.HasField("process")
                    assert not td.HasField("thread")
                    assert not td.HasField("counter")
                    assert not td.HasField("child_ordering")
                    assert not td.HasField("sibling_order_rank")
                    assert not td.HasField("description")
                    seen += 1
        assert seen == 1, f"expected exactly one Processes track descriptor, got {seen}"
        assert not any(TracePacket.FromString(d).track_descriptor.uuid == lifetime_uuid for d in descriptors), (
            "the Processes descriptor must come from finalize, not from a convert pass"
        )

    def test_process_lifetime_slice_begin_at_first_event_ts(self, state: PerfettoTrackState) -> None:
        """The ``Process <pid>`` slice BEGIN is emitted at the ts of the
        first non-meta event for the pid, on the shared ``Processes``
        track. It carries the observed span as ``real_start_ts`` /
        ``real_end_ts``, plus a ``cmdline`` debug annotation joined with
        single spaces when the process carries one."""
        target = proc(TARGET_PID)
        state.set_cmdline(target, ("python3", "-m", "fake_target"))
        item = pause_item()

        _, packets = convert_item(target, item, state, sequence_id=1)

        lifetime_uuid = state.get_or_create_process_lifetime_track_uuid()
        begin_packets: list[TracePacket] = []
        for p in packets:
            packet = TracePacket()
            packet.ParseFromString(p)
            if (
                packet.track_event.type == TrackEvent.Type.TYPE_SLICE_BEGIN
                and packet.track_event.track_uuid == lifetime_uuid
            ):
                begin_packets.append(packet)
        assert len(begin_packets) == 1, f"expected exactly one slice BEGIN on Processes track, got {len(begin_packets)}"
        first_packet = begin_packets[0]
        assert first_packet.timestamp == 1_000
        assert first_packet.track_event.name == TARGET_ROW_NAME
        annotations = first_packet.track_event.debug_annotations
        assert [a.name for a in annotations] == [
            CMDLINE,
            PID,
            PID_EPOCH,
            REAL_START_TS,
            REAL_END_TS,
            CLIPPED,
        ]
        by_name = {a.name: a for a in annotations}
        assert by_name[CMDLINE].string_value == "python3 -m fake_target"
        assert by_name[REAL_START_TS].int_value == 1_000
        assert by_name[REAL_END_TS].int_value == 2_000

    def test_process_lifetime_slice_begin_no_cmdline_omits_arg(self, state: PerfettoTrackState) -> None:
        """When the process carries no cmdline, the slice BEGIN on
        the ``Processes`` track carries only the observed span: the
        ``cmdline`` annotation is dropped, the ``real_*`` pair is not,
        since every slice records its span whatever else is known."""
        item = pause_item()

        _, packets = convert_item(proc(TARGET_PID), item, state, sequence_id=1)

        lifetime_uuid = state.get_or_create_process_lifetime_track_uuid()
        begin_packets: list[TracePacket] = []
        for p in packets:
            packet = TracePacket()
            packet.ParseFromString(p)
            if (
                packet.track_event.type == TrackEvent.Type.TYPE_SLICE_BEGIN
                and packet.track_event.track_uuid == lifetime_uuid
            ):
                begin_packets.append(packet)
        assert len(begin_packets) == 1
        annotations = begin_packets[0].track_event.debug_annotations
        assert [a.name for a in annotations] == [PID, PID_EPOCH, REAL_START_TS, REAL_END_TS, CLIPPED]

    def test_process_lifetime_slice_end_at_last_event_ts(self, state: PerfettoTrackState) -> None:
        """The ``Process <pid>`` slice END is emitted at the ts of the
        last non-meta event for the pid, on the shared ``Processes``
        track."""
        item = pause_item()

        _, packets = convert_item(proc(TARGET_PID), item, state, sequence_id=1)

        lifetime_uuid = state.get_or_create_process_lifetime_track_uuid()
        end_packets: list[TracePacket] = []
        for p in packets:
            packet = TracePacket()
            packet.ParseFromString(p)
            if (
                packet.track_event.type == TrackEvent.Type.TYPE_SLICE_END
                and packet.track_event.track_uuid == lifetime_uuid
            ):
                end_packets.append(packet)
        assert len(end_packets) == 1, f"expected exactly one slice END on Processes track, got {len(end_packets)}"
        # Last non-meta event ts in this fixture is 2_000 (ts_stop).
        assert end_packets[0].timestamp == 2_000
        assert end_packets[0].track_event.name == TARGET_ROW_NAME

    def test_process_lifetime_two_pids_one_shared_track(self, state: PerfettoTrackState) -> None:
        """Two distinct pids share the same ``Processes`` track UUID and
        each get their own slice pair. These two spans cross -- pid 100
        runs ``[500, 1500]`` and pid 200 ``[1000, 5000]`` -- so pid 100's
        end is clipped to one nanosecond before pid 200 begins. Both
        BEGINs record their observed span in ``real_start_ts`` /
        ``real_end_ts``, so pid 100's clipped end is recoverable and pid
        200's untouched one reads the same way. Each BEGIN also carries
        a ``cmdline`` annotation reflecting the program that process
        was running."""
        early, late = proc(TARGET_PID), proc(OTHER_PID)
        state.set_cmdline(early, ("python3", "-m", "early_target"))
        state.set_cmdline(late, ("python3", "-m", "late_target"))
        item_late_pid = pause_item(ts_stop=5_000)
        item_early_pid = pause_item(ts_start=500, ts_stop=1_500)

        _, convert_packets, closeout = convert_items(
            [(late, item_late_pid), (early, item_early_pid)],
            state,
            sequence_id=1,
        )

        lifetime_uuid = state.get_or_create_process_lifetime_track_uuid()
        assert lifetime_slices(convert_packets, lifetime_uuid) == [], (
            "convert passes must emit no Processes-track slices"
        )
        # One BEGIN/END pair per pid, in clipped-span order: pid 100
        # opens first and is closed at 999 because pid 200 crosses it,
        # then pid 200 opens and closes.
        assert lifetime_slices(closeout, lifetime_uuid) == [
            (
                500,
                TrackEventType.SLICE_BEGIN,
                TARGET_ROW_NAME,
                {
                    CMDLINE: "python3 -m early_target",
                    PID: 100,
                    PID_EPOCH: 1,
                    REAL_START_TS: 500,
                    REAL_END_TS: 1_500,
                    CLIPPED: True,
                },
            ),
            (999, TrackEventType.SLICE_END, TARGET_ROW_NAME, {}),
            (
                1_000,
                TrackEventType.SLICE_BEGIN,
                OTHER_ROW_NAME,
                {
                    CMDLINE: "python3 -m late_target",
                    PID: 200,
                    PID_EPOCH: 1,
                    REAL_START_TS: 1_000,
                    REAL_END_TS: 5_000,
                    CLIPPED: False,
                },
            ),
            (5_000, TrackEventType.SLICE_END, OTHER_ROW_NAME, {}),
        ]

    def test_two_processes_on_one_pid_name_their_own_programs(self, state: PerfettoTrackState) -> None:
        """A pid the operating system handed out twice draws two spans,
        each annotated with the program its own process was running.

        The monitor sends the command line as it creates the process, so
        the second names what it was running and not what its predecessor
        was (ADR-0010)."""
        first, second = proc(TARGET_PID), proc(TARGET_PID, pid_epoch=2)
        state.set_cmdline(first, ("python3", "-m", "first_target"))
        state.set_cmdline(second, ("python3", "-m", "second_target"))
        item1 = pause_item()
        item2 = pause_item(
            ts_start=3_000, ts_stop=4_000, heap_size=2000, collections=2, collected=20, candidates=10, duration=0.002
        )

        _, _, closeout = convert_items([(first, item1), (second, item2)], state, sequence_id=1)

        lifetime_uuid = state.get_or_create_process_lifetime_track_uuid()
        assert lifetime_slices(closeout, lifetime_uuid) == [
            (
                1_000,
                TrackEventType.SLICE_BEGIN,
                TARGET_ROW_NAME,
                {
                    CMDLINE: "python3 -m first_target",
                    PID: 100,
                    PID_EPOCH: 1,
                    REAL_START_TS: 1_000,
                    REAL_END_TS: 2_000,
                    CLIPPED: False,
                },
            ),
            (2_000, TrackEventType.SLICE_END, TARGET_ROW_NAME, {}),
            (
                3_000,
                TrackEventType.SLICE_BEGIN,
                process_track_name(proc(TARGET_PID, 2)),
                {
                    CMDLINE: "python3 -m second_target",
                    PID: 100,
                    PID_EPOCH: 2,
                    REAL_START_TS: 3_000,
                    REAL_END_TS: 4_000,
                    CLIPPED: False,
                },
            ),
            (4_000, TrackEventType.SLICE_END, process_track_name(proc(TARGET_PID, 2)), {}),
        ]

    def test_process_lifetime_idempotent_across_converts(self, state: PerfettoTrackState) -> None:
        """Two convert passes for the same pid produce a single slice
        pair spanning both batches: the second pass widens the recorded
        span, and the pair is emitted once at closeout. One pid alone can
        never cross anything, so the drawn span and the ``real_*``
        annotations agree."""
        target = proc(TARGET_PID)
        state.set_cmdline(target, ("python3", "-m", "fake_target"))
        item1 = pause_item()
        item2 = pause_item(
            gen=1,
            ts_start=3_000,
            ts_stop=4_000,
            heap_size=2000,
            collections=2,
            collected=20,
            candidates=10,
            duration=0.002,
        )

        _, convert_packets, closeout = convert_items(
            [(target, item1), (target, item2)],
            state,
            sequence_id=1,
        )

        lifetime_uuid = state.get_or_create_process_lifetime_track_uuid()
        assert lifetime_slices(convert_packets, lifetime_uuid) == []
        # One pair only, spanning the first batch's ts_start to the
        # second batch's ts_stop.
        assert lifetime_slices(closeout, lifetime_uuid) == [
            (
                1_000,
                TrackEventType.SLICE_BEGIN,
                TARGET_ROW_NAME,
                {
                    CMDLINE: "python3 -m fake_target",
                    PID: 100,
                    PID_EPOCH: 1,
                    REAL_START_TS: 1_000,
                    REAL_END_TS: 4_000,
                    CLIPPED: False,
                },
            ),
            (4_000, TrackEventType.SLICE_END, TARGET_ROW_NAME, {}),
        ]


class TestAQuietProcessGetsARow:
    """A process gcmon polled and read no collections from. Liveness folded a
    span in and nothing else ever named it, so no convert pass described it.

    Finalization describes it instead, and it draws a row like any other:
    ADR-0011 puts a process gcmon watched and read nothing from on the
    timeline, distinct from one it never reached.
    """

    QUIET = proc(TARGET_PID)
    BUSY = proc(OTHER_PID)
    QUIET_CMDLINE = ("python3", "-m", "quiet_target")

    def _quiet_only(self) -> tuple[PerfettoTrackState, list[bytes]]:
        """A trace with nothing in it but one process's liveness."""
        state = PerfettoTrackState()
        state.set_cmdline(self.QUIET, self.QUIET_CMDLINE)
        for ts in (500, 5_000):
            state.update_process_lifetime(self.QUIET, ts)
        return state, finalize_perfetto_packets(state, sequence_id=1)

    def _quiet_and_busy(self) -> tuple[PerfettoTrackState, list[bytes], list[bytes]]:
        """``BUSY`` collects first, ``QUIET`` only ever answers a poll.

        Returns the state, what the convert pass emitted, and the closeout.
        """
        state = PerfettoTrackState()
        events = convert_item_to_trace_format(
            self.BUSY,
            pause_item(heap_size=1024, collected=1, candidates=1),
        )
        descriptors, _ = convert_trace_events_to_perfetto(events, state, sequence_id=1)
        for ts in (3_000, 9_000):
            state.update_process_lifetime(self.QUIET, ts)
        return state, descriptors, finalize_perfetto_packets(state, sequence_id=1)

    def test_the_descriptor_carries_its_own_name_start_and_cmdline(self) -> None:
        """As complete as any other process's: the monitor publishes a command
        line for every process it creates, so nothing here is second class."""
        state, packets = self._quiet_only()

        descriptors = _process_descriptors(packets)
        assert list(descriptors) == [TARGET_ROW_NAME]
        described = descriptors[TARGET_ROW_NAME]
        assert described.process.pid == state.get_row_pid(self.QUIET)
        assert described.process.start_timestamp_ns == 500
        assert list(described.process.cmdline) == list(self.QUIET_CMDLINE)
        assert described.description == " ".join(self.QUIET_CMDLINE)

    def test_it_draws_a_lifetime_bar_on_its_own_row(self) -> None:
        """The row is worth drawing because of what is on it: one bar over the
        interval gcmon watched, and nothing under it."""
        state, packets = self._quiet_only()

        row_uuid = state.get_process_track_uuid(self.QUIET)
        assert _row_slices(packets, row_uuid) == [
            (500, TrackEventType.SLICE_BEGIN, _PROCESS_ROW_SLICE_NAME),
            (5_000, TrackEventType.SLICE_END, ""),
        ]

    def test_the_root_descriptor_goes_out_for_a_trace_with_no_events(self) -> None:
        """Ranks are a hint the UI honours only under an explicit root
        descriptor, and a run in which nothing ever collected reaches close
        without a convert pass having emitted one."""
        _state, packets = self._quiet_only()

        roots = [td for td in map(parse_track_descriptor, packets) if td is not None and td.uuid == 0]
        assert len(roots) == 1
        assert roots[0].process_ordering == ProcessOrdering.EXPLICIT

    def test_ranks_are_contiguous_across_quiet_and_busy_processes(self) -> None:
        """What closes ADR-0011's rank gaps: the quiet process consumed a rank
        all along and had no descriptor to spend it on, so the drawn rows ran
        0, 2, 3."""
        _state, descriptors, closeout = self._quiet_and_busy()

        described = _process_descriptors([*descriptors, *closeout])
        assert {name: td.sibling_order_rank for name, td in described.items()} == {
            OTHER_ROW_NAME: 0,
            TARGET_ROW_NAME: 1,
        }

    def test_a_described_process_is_not_described_twice(self) -> None:
        """The convert pass already described ``BUSY``; finalization walks
        every process it holds and must leave that one alone."""
        _state, descriptors, closeout = self._quiet_and_busy()

        assert list(_process_descriptors(descriptors)) == [OTHER_ROW_NAME]
        assert list(_process_descriptors(closeout)) == [TARGET_ROW_NAME]


class TestARetiredProcessRowGoesOutEarly:
    """gcmon has let go of the pid, so the process's span is final and its row
    can be drawn without waiting for the end of the run.

    What a run killed mid-flight loses shrinks to the processes still running.
    The shared ``Processes`` slice cannot follow: it is clipped against its
    siblings and a process discovered later can still open a span inside this
    one (ADR-0011).
    """

    RETIRED = proc(TARGET_PID)
    LATE = proc(OTHER_PID)

    def _retire(self) -> tuple[PerfettoTrackState, list[bytes]]:
        state = PerfettoTrackState()
        state.set_cmdline(self.RETIRED, ("python3", "-m", "child"))
        for ts in (500, 5_000):
            state.update_process_lifetime(self.RETIRED, ts)
        return state, emit_retired_process_row(self.RETIRED, state, sequence_id=1)

    def test_the_row_is_written_before_close(self) -> None:
        """Everything the row needs: the descriptor the UI hangs the name and
        the command line on, and the bar that keeps it rendered."""
        state, packets = self._retire()

        assert list(_process_descriptors(packets)) == [TARGET_ROW_NAME]
        assert _row_slices(packets, state.get_process_track_uuid(self.RETIRED)) == [
            (500, TrackEventType.SLICE_BEGIN, _PROCESS_ROW_SLICE_NAME),
            (5_000, TrackEventType.SLICE_END, ""),
        ]

    def test_close_repeats_neither_the_descriptor_nor_the_bar(self) -> None:
        state, _packets = self._retire()
        row_uuid = state.get_process_track_uuid(self.RETIRED)

        closeout = finalize_perfetto_packets(state, sequence_id=1)

        assert _process_descriptors(closeout) == {}
        assert _row_slices(closeout, row_uuid) == []

    def test_close_still_draws_its_processes_slice_clipped(self) -> None:
        """Why the shared slice waits. ``LATE`` is discovered after ``RETIRED``
        was drawn and opens a span inside it, so the sweep pulls ``RETIRED``
        back. A slice written early could not have been clipped."""
        state, _packets = self._retire()
        for ts in (2_000, 9_000):
            state.update_process_lifetime(self.LATE, ts)

        closeout = finalize_perfetto_packets(state, sequence_id=1)

        lifetime_uuid = state.get_or_create_process_lifetime_track_uuid()
        drawn = lifetime_slices(closeout, lifetime_uuid)
        assert [(ts, event_type, name) for ts, event_type, name, _ in drawn] == [
            (500, TrackEventType.SLICE_BEGIN, TARGET_ROW_NAME),
            (1_999, TrackEventType.SLICE_END, TARGET_ROW_NAME),
            (2_000, TrackEventType.SLICE_BEGIN, OTHER_ROW_NAME),
            (9_000, TrackEventType.SLICE_END, OTHER_ROW_NAME),
        ]
        retired = drawn[0][3]
        assert retired[CLIPPED] is True
        assert retired[REAL_END_TS] == 5_000, "the observed pair is untouched by clipping"

    def test_a_process_with_no_span_writes_nothing(self, state: PerfettoTrackState) -> None:
        """gcmon never observed it, so there is nothing to draw."""

        assert emit_retired_process_row(proc(TARGET_PID), state, sequence_id=1) == []

    def test_retiring_twice_writes_nothing_the_second_time(self) -> None:
        state, packets = self._retire()

        assert packets != []
        assert emit_retired_process_row(self.RETIRED, state, sequence_id=1) == []

    def test_retiring_after_close_writes_nothing(self, state: PerfettoTrackState) -> None:
        """A retirement racing ``close()`` would write into a trace whose
        closeout has gone out, where nothing downstream would report it."""
        for ts in (500, 5_000):
            state.update_process_lifetime(self.RETIRED, ts)
        finalize_perfetto_packets(state, sequence_id=1)

        assert emit_retired_process_row(self.RETIRED, state, sequence_id=1) == []


class TestWhatCloseAlreadyKnows:
    """Two annotations the exporter computes from what it is holding:
    ``interpreters`` on the ``Lifetime`` bar, ``clipped`` on the
    ``Processes`` slice.

    They sit on different rows because they settle at different moments.
    The interpreter count is final when the process retires, and the bar
    can go out then (ADR-0011); ``clipped`` is the close-time sweep's
    verdict, so only the slice that waits for close can carry it.
    """

    BUSY = proc(TARGET_PID)
    LATE = proc(OTHER_PID)

    def _item(self, iid: int, ts_start: int, ts_stop: int) -> GCStatsInfo:
        return pause_item(iid=iid, ts_start=ts_start, ts_stop=ts_stop, heap_size=1024, collected=1, candidates=1)

    def _begin(self, packets: list[bytes], track_uuid: int) -> dict[str, str | int]:
        """The annotations on the one BEGIN drawn on *track_uuid*."""
        begins = [
            annotations
            for _ts, event_type, _name, annotations in lifetime_slices(packets, track_uuid)
            if event_type == TrackEventType.SLICE_BEGIN
        ]
        assert len(begins) == 1, f"expected one BEGIN on track {track_uuid}, got {len(begins)}"
        return begins[0]

    def _read_from(self, *iids: int) -> tuple[PerfettoTrackState, list[bytes]]:
        """One record per iid, converted and then finalized."""
        state = PerfettoTrackState()
        items = [(self.BUSY, self._item(iid, 1_000 * (iid + 1), 2_000 * (iid + 1))) for iid in iids]
        _descriptors, _convert, closeout = convert_items(items, state, sequence_id=1)
        return state, closeout

    def _crossing(self) -> tuple[PerfettoTrackState, list[bytes]]:
        """``BUSY``'s span is pulled back by ``LATE`` opening inside it."""
        state = PerfettoTrackState()
        for ts in (500, 5_000):
            state.update_process_lifetime(self.BUSY, ts)
        for ts in (2_000, 9_000):
            state.update_process_lifetime(self.LATE, ts)
        return state, finalize_perfetto_packets(state, sequence_id=1)

    def test_the_bar_counts_every_interpreter_that_collected(self) -> None:
        """What the row's name cannot say: one process, two interpreters."""
        state, closeout = self._read_from(0, 1)

        assert self._begin(closeout, state.get_process_track_uuid(self.BUSY))["interpreters"] == 2

    def test_two_records_from_one_interpreter_still_count_one(self, state: PerfettoTrackState) -> None:
        items = [(self.BUSY, self._item(0, 1_000, 2_000)), (self.BUSY, self._item(0, 3_000, 4_000))]

        _descriptors, _convert, closeout = convert_items(items, state, sequence_id=1)

        assert self._begin(closeout, state.get_process_track_uuid(self.BUSY))["interpreters"] == 1

    def test_a_process_gcmon_read_nothing_from_counts_none(self, state: PerfettoTrackState) -> None:
        """Zero is a reading, not a gap: gcmon polled this process and it
        collected nothing."""
        for ts in (500, 5_000):
            state.update_process_lifetime(self.BUSY, ts)

        closeout = finalize_perfetto_packets(state, sequence_id=1)

        assert self._begin(closeout, state.get_process_track_uuid(self.BUSY))["interpreters"] == 0

    def test_each_process_counts_its_own_interpreters(self, state: PerfettoTrackState) -> None:
        items = [
            (self.BUSY, self._item(0, 1_000, 2_000)),
            (self.LATE, self._item(0, 3_000, 4_000)),
            (self.LATE, self._item(1, 5_000, 6_000)),
        ]

        _descriptors, _convert, closeout = convert_items(items, state, sequence_id=1)

        assert self._begin(closeout, state.get_process_track_uuid(self.BUSY))["interpreters"] == 1
        assert self._begin(closeout, state.get_process_track_uuid(self.LATE))["interpreters"] == 2

    def test_the_shortened_slice_says_the_sweep_moved_it(self) -> None:
        """Without this an operator reads the two rows' durations and
        subtracts to find out which one the sweep touched."""
        state, closeout = self._crossing()
        lifetime_uuid = state.get_or_create_process_lifetime_track_uuid()

        by_name = {
            name: annotations
            for _ts, event_type, name, annotations in lifetime_slices(closeout, lifetime_uuid)
            if event_type == TrackEventType.SLICE_BEGIN
        }
        assert by_name[TARGET_ROW_NAME][CLIPPED] is True
        assert by_name[OTHER_ROW_NAME][CLIPPED] is False

    def test_clipped_goes_out_on_both_kinds_of_slice(self) -> None:
        """Written whichever way it reads, so a consumer asks for the value
        rather than checking whether the annotation is there."""
        state, closeout = self._crossing()
        lifetime_uuid = state.get_or_create_process_lifetime_track_uuid()

        for _ts, event_type, name, annotations in lifetime_slices(closeout, lifetime_uuid):
            if event_type == TrackEventType.SLICE_BEGIN:
                assert isinstance(annotations[CLIPPED], bool), name

    def test_the_bar_does_not_carry_clipped(self) -> None:
        """A retired process's bar is written before the sweep decides
        anything, so no bar can carry a verdict."""
        state, closeout = self._crossing()

        assert CLIPPED not in self._begin(closeout, state.get_process_track_uuid(self.BUSY))

    def test_the_shared_slice_does_not_carry_interpreters(self) -> None:
        """One count per process, on the row that is the process."""
        state, closeout = self._read_from(0, 1)

        lifetime_uuid = state.get_or_create_process_lifetime_track_uuid()
        assert "interpreters" not in self._begin(closeout, lifetime_uuid)


class TestWhatGcmonReadAndMissed:
    """How complete the capture is for one process, on its own bar:
    ``sampled_count`` against ``lost_count``, and the pause inside what was
    missed.

    Counted in the convert pass, so a trace built by ``gcmon combine`` from
    a capture reads the same as a live one.
    """

    BUSY = proc(TARGET_PID)
    OTHER = proc(OTHER_PID)

    def _bar(self, packets: list[bytes], state: PerfettoTrackState, process: Process) -> dict[str, str | int]:
        """The annotations on *process*'s ``Lifetime`` BEGIN."""
        begins = [
            annotations
            for _ts, event_type, _name, annotations in lifetime_slices(packets, state.get_process_track_uuid(process))
            if event_type == TrackEventType.SLICE_BEGIN
        ]
        assert len(begins) == 1, f"expected one bar for {process}, got {len(begins)}"
        return begins[0]

    def _convert(self, state: PerfettoTrackState, events: list[TraceEvent]) -> None:
        convert_trace_events_to_perfetto(events, state, sequence_id=1)

    def _records(self, process: Process, count: int) -> list[TraceEvent]:
        events: list[TraceEvent] = []
        for index in range(count):
            item = create_mock_stats_item(ts_start=1_000 * (index + 1), ts_stop=1_000 * (index + 1) + 100)
            events.extend(convert_item_to_trace_format(process, item))
        return events

    def test_the_bar_counts_every_record_read(self, state: PerfettoTrackState) -> None:
        self._convert(state, self._records(self.BUSY, 3))

        closeout = finalize_perfetto_packets(state, sequence_id=1)

        assert self._bar(closeout, state, self.BUSY)[SAMPLED_COUNT] == 3

    def test_a_process_that_lost_nothing_carries_its_whole_count(self, state: PerfettoTrackState) -> None:
        """The case the loss path gets wrong: ``observed_count`` rides on a
        ``GC Loss`` slice, and a process that lost nothing has none, so
        summing those would say gcmon read nothing here."""
        self._convert(state, self._records(self.BUSY, 5))

        closeout = finalize_perfetto_packets(state, sequence_id=1)

        bar = self._bar(closeout, state, self.BUSY)
        assert bar[SAMPLED_COUNT] == 5
        assert bar[LOST_COUNT] == 0
        assert bar[LOST_PAUSE_NS] == 0
        assert bar[LOST_PAUSE] == "0ns"

    def test_the_sub_phases_of_a_record_do_not_inflate_the_count(self, state: PerfettoTrackState) -> None:
        """One record is many slices and many counters. The count is of
        records."""
        self._convert(state, convert_item_to_trace_format(self.BUSY, create_mock_incremental_item()))

        closeout = finalize_perfetto_packets(state, sequence_id=1)

        assert self._bar(closeout, state, self.BUSY)[SAMPLED_COUNT] == 1

    def test_a_process_gcmon_read_nothing_from_reads_zero(self, state: PerfettoTrackState) -> None:
        for ts in (500, 5_000):
            state.update_process_lifetime(self.BUSY, ts)

        closeout = finalize_perfetto_packets(state, sequence_id=1)

        bar = self._bar(closeout, state, self.BUSY)
        assert bar[SAMPLED_COUNT] == 0
        assert bar[LOST_COUNT] == 0

    def test_loss_intervals_sum_across_the_interpreters(self, state: PerfettoTrackState) -> None:
        self._convert(
            state,
            [
                *convert_loss_to_trace_format(self.BUSY, create_mock_loss_item(iid=0, lost_count=4)),
                *convert_loss_to_trace_format(self.BUSY, create_mock_loss_item(iid=1, lost_count=6)),
            ],
        )

        closeout = finalize_perfetto_packets(state, sequence_id=1)

        assert self._bar(closeout, state, self.BUSY)[LOST_COUNT] == 10

    def test_lost_pause_reads_the_way_the_loss_slice_writes_it(self, state: PerfettoTrackState) -> None:
        """Same helper, so an operator reading a ``GC Loss`` bar and a
        process bar reads one format."""
        self._convert(
            state,
            convert_loss_to_trace_format(self.BUSY, create_mock_loss_item(lost_count=2, lost_pause_ns=3_316_458_100)),
        )

        closeout = finalize_perfetto_packets(state, sequence_id=1)

        bar = self._bar(closeout, state, self.BUSY)
        assert bar[LOST_PAUSE_NS] == 3_316_458_100
        assert bar[LOST_PAUSE] == duration_text(3_316_458_100)

    def test_the_totals_run_across_batches(self, state: PerfettoTrackState) -> None:
        """A buffered export converts in flushes, and the bar goes out
        after the last of them."""
        self._convert(state, self._records(self.BUSY, 2))
        self._convert(state, self._records(self.BUSY, 3))
        self._convert(state, convert_loss_to_trace_format(self.BUSY, create_mock_loss_item(lost_count=8)))

        closeout = finalize_perfetto_packets(state, sequence_id=1)

        bar = self._bar(closeout, state, self.BUSY)
        assert bar[SAMPLED_COUNT] == 5
        assert bar[LOST_COUNT] == 8

    def test_each_process_carries_its_own_totals(self, state: PerfettoTrackState) -> None:
        self._convert(
            state,
            [
                *self._records(self.BUSY, 1),
                *self._records(self.OTHER, 4),
                *convert_loss_to_trace_format(self.OTHER, create_mock_loss_item(lost_count=2)),
            ],
        )

        closeout = finalize_perfetto_packets(state, sequence_id=1)

        assert self._bar(closeout, state, self.BUSY)[SAMPLED_COUNT] == 1
        assert self._bar(closeout, state, self.BUSY)[LOST_COUNT] == 0
        assert self._bar(closeout, state, self.OTHER)[SAMPLED_COUNT] == 4
        assert self._bar(closeout, state, self.OTHER)[LOST_COUNT] == 2

    def test_a_retired_process_carries_them_on_its_early_bar(self, state: PerfettoTrackState) -> None:
        """The bar leaves before close, and all four are final the moment
        gcmon lets go of the pid."""
        self._convert(state, self._records(self.BUSY, 3))
        self._convert(state, convert_loss_to_trace_format(self.BUSY, create_mock_loss_item(lost_count=9)))

        early = emit_retired_process_row(self.BUSY, state, sequence_id=1)

        bar = self._bar(early, state, self.BUSY)
        assert bar[SAMPLED_COUNT] == 3
        assert bar[LOST_COUNT] == 9

    def test_the_shared_slice_carries_none_of_them(self, state: PerfettoTrackState) -> None:
        """One reading per process, on the row that is the process."""
        self._convert(state, self._records(self.BUSY, 1))

        closeout = finalize_perfetto_packets(state, sequence_id=1)
        lifetime_uuid = state.get_or_create_process_lifetime_track_uuid()

        shared = next(
            annotations
            for _ts, event_type, _name, annotations in lifetime_slices(closeout, lifetime_uuid)
            if event_type == TrackEventType.SLICE_BEGIN
        )
        assert SAMPLED_COUNT not in shared
        assert LOST_COUNT not in shared


class TestCloseoutAtFinalize:
    """``convert_trace_events_to_perfetto`` never closes a ``Processes``
    slice; only ``finalize_perfetto_packets`` does.
    """

    def test_no_closeout_emitted_during_convert(self, state: PerfettoTrackState) -> None:
        """``convert_trace_events_to_perfetto`` never emits a
        ``TYPE_SLICE_END`` on the ``Processes`` track; closeout is the
        caller's job (see ``finalize_perfetto_packets``)."""
        item = pause_item()
        gc_events = convert_item_to_trace_format(proc(TARGET_PID), item)

        _, packets = convert_trace_events_to_perfetto(gc_events, state, sequence_id=1)
        lifetime_uuid = state.get_or_create_process_lifetime_track_uuid()
        end_packets: list[TracePacket] = []
        for p in packets:
            packet = TracePacket()
            packet.ParseFromString(p)
            if (
                packet.track_event.type == TrackEvent.Type.TYPE_SLICE_END
                and packet.track_event.track_uuid == lifetime_uuid
            ):
                end_packets.append(packet)
        assert end_packets == [], (
            f"convert_trace_events_to_perfetto must not emit slice END "
            f"on the Processes track; got {len(end_packets)} ENDs"
        )

        # Calling finalize_perfetto_packets now produces exactly one END.
        closeout = finalize_perfetto_packets(state, sequence_id=1)
        end_packets = []
        for p in closeout:
            packet = TracePacket()
            packet.ParseFromString(p)
            if (
                packet.track_event.type == TrackEvent.Type.TYPE_SLICE_END
                and packet.track_event.track_uuid == lifetime_uuid
            ):
                end_packets.append(packet)
        assert len(end_packets) == 1
        assert end_packets[0].timestamp == 2_000

    def test_two_batches_still_leave_the_closeout_to_finalize(self, state: PerfettoTrackState) -> None:
        """Across two ``convert_trace_events_to_perfetto`` calls for the
        same pid, the convert call never emits a slice END on the
        ``Processes`` track (the END is the caller's job, and
        ``finalize_perfetto_packets`` is called exactly once at the end
        of the trace). The single END's ts is the last non-counter
        non-meta event ts of the *second* convert call, not the first.
        """
        item1 = pause_item()
        item2 = pause_item(
            gen=1,
            iid=1,
            ts_start=3_000,
            ts_stop=4_000,
            heap_size=2000,
            collections=2,
            collected=20,
            candidates=10,
            duration=0.002,
        )
        events1: list[TraceEvent] = [
            *convert_item_to_trace_format(proc(TARGET_PID), item1),
        ]
        events2: list[TraceEvent] = [
            *convert_item_to_trace_format(proc(TARGET_PID), item2),
        ]

        _, packets1 = convert_trace_events_to_perfetto(
            events1,
            state,
            sequence_id=1,
        )
        _, packets2 = convert_trace_events_to_perfetto(
            events2,
            state,
            sequence_id=1,
        )
        # finalize is called exactly once at the end (mimicking
        # encoder.close()).
        closeout = finalize_perfetto_packets(state, sequence_id=1)
        all_packets = packets1 + packets2 + closeout
        lifetime_uuid = state.get_or_create_process_lifetime_track_uuid()

        def _count(packets: list[bytes], event_type: int) -> int:
            n = 0
            for p in packets:
                packet = TracePacket()
                packet.ParseFromString(p)
                if packet.track_event.track_uuid == lifetime_uuid and packet.track_event.type == event_type:
                    n += 1
            return n

        # Neither batch emits anything on the Processes track; both ends
        # of the pair are the finalize pass's job.
        assert _count(packets1, TrackEvent.Type.TYPE_SLICE_BEGIN) == 0
        assert _count(packets1, TrackEvent.Type.TYPE_SLICE_END) == 0
        assert _count(packets2, TrackEvent.Type.TYPE_SLICE_BEGIN) == 0
        assert _count(packets2, TrackEvent.Type.TYPE_SLICE_END) == 0
        # The finalize pass: exactly one pair.
        assert _count(closeout, TrackEvent.Type.TYPE_SLICE_BEGIN) == 1
        assert _count(closeout, TrackEvent.Type.TYPE_SLICE_END) == 1
        # Across the union, exactly one BEGIN and one END.
        assert _count(all_packets, TrackEvent.Type.TYPE_SLICE_BEGIN) == 1
        assert _count(all_packets, TrackEvent.Type.TYPE_SLICE_END) == 1
        # The single END's ts is the last non-counter non-meta event ts
        # of the *second* batch (4_000), not the first (2_000).
        end_packet = None
        for p in all_packets:
            packet = TracePacket()
            packet.ParseFromString(p)
            if (
                packet.track_event.type == TrackEvent.Type.TYPE_SLICE_END
                and packet.track_event.track_uuid == lifetime_uuid
            ):
                end_packet = packet
                break
        assert end_packet is not None
        assert end_packet.timestamp == 4_000

        # Calling finalize again is a no-op (the track is marked emitted).
        assert finalize_perfetto_packets(state, sequence_id=1) == []
