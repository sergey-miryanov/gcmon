"""Tests for the shared ``Processes`` row: spans, tracks, closeout.

Every process draws its span on a track of its own, and the row is those
tracks merged by their shared name. What the merge then does with them is
the integration suite's subject; here the packets are read on the wire. See
ADR-0011.
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
    _PROCESS_ROW_SLICE_NAME,
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
    CMDLINE,
    LOST_COUNT,
    LOST_PAUSE,
    LOST_PAUSE_NS,
    PID,
    PID_EPOCH,
    SAMPLED_COUNT,
)
from gcmon.model.process import Process
from gcmon.model.trace_event import TraceEvent
from tests.exporters.perfetto_helpers import (
    convert_item,
    convert_items,
    lifetime_slices,
    parse_track_descriptor,
    pause_item,
    processes_row_uuids,
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


def _finalize_spans(spans: list[ProcessSpan]) -> dict[int, tuple[int, int]]:
    """Run *spans* through ``finalize_perfetto_packets`` and return
    ``{pid: (ts, end_ts)}``, the pair each slice draws.

    Every pid with a span appears, since nothing is ever dropped. Each pid
    here holds one process, so the pid identifies the span.

    Also asserts the shape a reader pairs up: one BEGIN and one END per
    process, the BEGIN named and the END not, each pair adjacent in the
    packet stream.
    """
    state = PerfettoTrackState()
    for one in spans:
        state.update_process_lifetime(one.process, one.start_ts)
        state.update_process_lifetime(one.process, one.end_ts)
    packets = finalize_perfetto_packets(state, sequence_id=1)

    drawn: dict[int, tuple[int, int]] = {}
    for one in spans:
        events = lifetime_slices(packets, processes_row_uuids(state, one.process))
        assert [(event_type, name) for _ts, event_type, name, _ann in events] == [
            (TrackEventType.SLICE_BEGIN, process_track_name(one.process)),
            (TrackEventType.SLICE_END, ""),
        ], f"{one.process} draws {events}"
        drawn[one.process.pid] = (events[0][0], events[1][0])

    whole_row = lifetime_slices(packets, processes_row_uuids(state, *(one.process for one in spans)))
    assert [event_type for _ts, event_type, _name, _ann in whole_row] == [
        TrackEventType.SLICE_BEGIN,
        TrackEventType.SLICE_END,
    ] * len(spans), "each BEGIN is followed by its own END, so nothing is left open between two pairs"
    return drawn


class TestEverySpanIsDrawnAsObserved:
    """``finalize_perfetto_packets`` draws every span at the width gcmon
    observed, whatever the other spans do. A track of its own is what lets
    it: slices on one Perfetto track are a stack, and a crossing pair
    cannot be expressed on one (ADR-0011)."""

    def test_crossing_spans_both_keep_their_width(self) -> None:
        """The pair one shared track cannot express: pid 200 opens inside
        pid 100 and closes after it."""
        drawn = _finalize_spans([span(TARGET_PID, 500, 1_500), span(OTHER_PID, 1_000, 5_000)])

        assert drawn == {100: (500, 1_500), 200: (1_000, 5_000)}

    def test_nesting_keeps_both_widths(self) -> None:
        """A parent outliving its child, which is the common multi-process
        shape."""
        drawn = _finalize_spans([span(TARGET_PID, 500, 9_000), span(OTHER_PID, 1_000, 5_000)])

        assert drawn == {100: (500, 9_000), 200: (1_000, 5_000)}

    def test_disjoint_spans_keep_their_widths(self) -> None:
        drawn = _finalize_spans([span(TARGET_PID, 500, 1_000), span(OTHER_PID, 5_000, 9_000)])

        assert drawn == {100: (500, 1_000), 200: (5_000, 9_000)}

    def test_touching_spans_keep_their_ends(self) -> None:
        """``A.end == B.start``, where a shared track had to give one of
        them up: the relative order of an END and a BEGIN sharing a
        timestamp is not ours to control."""
        drawn = _finalize_spans([span(TARGET_PID, 500, 1_000), span(OTHER_PID, 1_000, 5_000)])

        assert drawn == {100: (500, 1_000), 200: (1_000, 5_000)}

    def test_spans_sharing_a_start_keep_their_widths(self) -> None:
        """The fan-out shape: children polled on one tick share the tick's
        timestamp."""
        drawn = _finalize_spans([span(TARGET_PID, 500, 1_000), span(OTHER_PID, 500, 9_000)])

        assert drawn == {100: (500, 1_000), 200: (500, 9_000)}

    def test_one_span_crossed_by_two_later_spans(self) -> None:
        """Pid 200 nests inside pid 100 and pid 300 crosses it. Both leave
        pid 100 alone."""
        drawn = _finalize_spans(
            [span(TARGET_PID, 500, 5_000), span(OTHER_PID, 1_000, 2_000), span(THIRD_PID, 3_000, 9_000)],
        )

        assert drawn == {100: (500, 5_000), 200: (1_000, 2_000), 300: (3_000, 9_000)}

    def test_a_fanned_out_pool_keeps_every_width(self) -> None:
        """What the row is for. Eight workers start one nanosecond apart and
        run for a thousand, and each reads back a thousand wide."""
        workers = [span(TARGET_PID + i, 500 + i, 1_500 + i) for i in range(8)]

        drawn = _finalize_spans(workers)

        assert drawn == {one.process.pid: (one.start_ts, one.end_ts) for one in workers}

    def test_single_instant_span_is_still_drawn(self) -> None:
        """A pid observed at a single instant gets a zero-duration slice
        rather than nothing. It is the only place the row records that the
        process existed, and omission is the one distortion a reader has no
        way to notice."""
        drawn = _finalize_spans([span(TARGET_PID, 500, 500)])

        assert drawn == {100: (500, 500)}

    def test_a_zero_length_span_beside_a_longer_one_survives(self) -> None:
        drawn = _finalize_spans([span(TARGET_PID, 500, 500), span(OTHER_PID, 500, 9_000)])

        assert drawn == {100: (500, 500), 200: (500, 9_000)}

    def test_no_spans_emits_nothing(self) -> None:
        assert finalize_perfetto_packets(PerfettoTrackState(), sequence_id=1) == []

    def test_undescribed_pid_without_a_cmdline_still_gets_a_slice(self, state: PerfettoTrackState) -> None:
        """A span is drawn for a pid that never reached ``mark_process_descriptor``
        -- one polled OK for a whole run that never collected, so it named no
        track and no convert pass described it. gcmon read no command line for
        this one, so its slice carries the pid and the epoch and nothing else.
        Its own row is ``TestAQuietProcessGetsARow``'s subject."""
        state.update_process_lifetime(proc(TARGET_PID), 500)
        state.update_process_lifetime(proc(TARGET_PID), 5_000)
        assert not state.has_process_descriptor(proc(TARGET_PID))

        packets = finalize_perfetto_packets(state, sequence_id=1)

        assert lifetime_slices(packets, processes_row_uuids(state, proc(TARGET_PID))) == [
            (
                500,
                TrackEventType.SLICE_BEGIN,
                TARGET_ROW_NAME,
                {PID: 100, PID_EPOCH: 1},
            ),
            (5_000, TrackEventType.SLICE_END, "", {}),
        ]

    def test_descriptor_refuses_a_second_emission(self, state: PerfettoTrackState) -> None:
        """The descriptor emitter asserts rather than trusting its
        caller: two descriptors for one uuid are accepted silently by the
        trace processor, so nothing downstream would report it."""
        target = proc(TARGET_PID)
        _emit_process_lifetime_track_descriptor(target, state, sequence_id=1)

        with pytest.raises(AssertionError, match="already gone out"):
            _emit_process_lifetime_track_descriptor(target, state, sequence_id=1)

    def test_each_process_describes_its_own_track(self, state: PerfettoTrackState) -> None:
        """One descriptor per process, so the guard is per process too."""
        _emit_process_lifetime_track_descriptor(proc(TARGET_PID), state, sequence_id=1)

        assert _emit_process_lifetime_track_descriptor(proc(OTHER_PID), state, sequence_id=1) != b""

    def test_track_is_marked_emitted_only_after_a_slice_goes_out(self, state: PerfettoTrackState) -> None:
        """A trace with no drawable span emits nothing and leaves the
        flag clear, so the guard cannot swallow a later real closeout."""
        assert finalize_perfetto_packets(state, sequence_id=1) == []
        assert not state.has_process_lifetime_emitted()

        state.update_process_lifetime(proc(TARGET_PID), 500)
        state.update_process_lifetime(proc(TARGET_PID), 5_000)

        assert finalize_perfetto_packets(state, sequence_id=1) != []
        assert state.has_process_lifetime_emitted()

    def test_the_closeout_bytes_do_not_depend_on_arrival_order(self) -> None:
        """The closeout sorts its spans, and that sort is the only thing
        keeping these bytes stable: a uuid, a row pid and a rank are all
        handed out in the order the pass walks the spans."""
        spans = [span(THIRD_PID, 20, 300), span(TARGET_PID, 0, 100), span(OTHER_PID, 10, 200)]

        def closeout_of(arrivals: list[ProcessSpan]) -> list[bytes]:
            state = PerfettoTrackState()
            for one in arrivals:
                state.update_process_lifetime(one.process, one.start_ts)
                state.update_process_lifetime(one.process, one.end_ts)
            return finalize_perfetto_packets(state, sequence_id=1)

        assert closeout_of(spans) == closeout_of(list(reversed(spans)))

    @pytest.mark.parametrize("seed", range(25))
    def test_every_random_span_is_drawn_as_observed(self, seed: int) -> None:
        """Whatever goes in: every pid keeps a slice, and each one is as
        wide as it was observed."""
        rng = random.Random(seed)
        spans: list[ProcessSpan] = []
        for pid in range(100, 100 + rng.randint(2, 12)):
            start = rng.randrange(0, 2_000)
            spans.append(span(pid, start, start + rng.randrange(0, 2_000)))

        drawn = _finalize_spans(spans)

        assert drawn == {one.process.pid: (one.start_ts, one.end_ts) for one in spans}


class TestProcessLifetimeSlices:
    """The ``Processes`` row as emitted through a full convert plus
    finalize pass: one BEGIN/END pair per pid, each on a track of its own.
    """

    def test_process_lifetime_track_emitted_once(self, state: PerfettoTrackState) -> None:
        """The descriptor for a process's ``Processes`` track is emitted at
        most once, even across multiple convert passes."""
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

        lifetime_uuid = state.get_process_lifetime_track_uuid(proc(TARGET_PID))
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
                    # no sibling_order_rank, no description, and nothing
                    # about the merge: the shared name is what merges it.
                    assert not td.HasField("parent_uuid")
                    assert not td.HasField("process")
                    assert not td.HasField("thread")
                    assert not td.HasField("counter")
                    assert not td.HasField("child_ordering")
                    assert not td.HasField("sibling_order_rank")
                    assert not td.HasField("description")
                    assert not td.HasField("sibling_merge_behavior")
                    assert not td.HasField("sibling_merge_key")
                    seen += 1
        assert seen == 1, f"expected exactly one Processes track descriptor, got {seen}"
        assert not any(TracePacket.FromString(d).track_descriptor.uuid == lifetime_uuid for d in descriptors), (
            "the Processes descriptor must come from finalize, not from a convert pass"
        )

    def test_each_process_gets_a_track_of_its_own_under_one_name(self, state: PerfettoTrackState) -> None:
        """Two processes, two descriptors, one name. The name is the whole
        of what merges them into a row (ADR-0011)."""
        early, late = proc(TARGET_PID), proc(OTHER_PID)

        _descriptors, _convert, closeout = convert_items(
            [(early, pause_item()), (late, pause_item(ts_start=3_000, ts_stop=4_000))],
            state,
            sequence_id=1,
        )

        described = {
            td.uuid: td.name
            for td in map(parse_track_descriptor, closeout)
            if td is not None and td.name == _PROCESS_LIFETIME_TRACK_NAME
        }
        assert described == {
            state.get_process_lifetime_track_uuid(early): _PROCESS_LIFETIME_TRACK_NAME,
            state.get_process_lifetime_track_uuid(late): _PROCESS_LIFETIME_TRACK_NAME,
        }

    def test_the_pair_spans_the_first_and_last_event(self, state: PerfettoTrackState) -> None:
        """The BEGIN lands on the ts of the first non-meta event for the pid
        and the END on the last, and only the BEGIN is named. It carries a
        ``cmdline`` annotation joined with single spaces where the process
        has one."""
        target = proc(TARGET_PID)
        state.set_cmdline(target, ("python3", "-m", "fake_target"))

        _, packets = convert_item(target, pause_item(), state, sequence_id=1)

        assert lifetime_slices(packets, processes_row_uuids(state, target)) == [
            (
                1_000,
                TrackEventType.SLICE_BEGIN,
                TARGET_ROW_NAME,
                {
                    CMDLINE: "python3 -m fake_target",
                    PID: 100,
                    PID_EPOCH: 1,
                },
            ),
            (2_000, TrackEventType.SLICE_END, "", {}),
        ]

    def test_a_process_without_a_cmdline_omits_the_annotation(self, state: PerfettoTrackState) -> None:
        """Every other annotation stays, so a consumer reads them without
        an annotation-present check."""
        target = proc(TARGET_PID)

        _, packets = convert_item(target, pause_item(), state, sequence_id=1)

        begin = lifetime_slices(packets, processes_row_uuids(state, target))[0]
        assert list(begin[3]) == [PID, PID_EPOCH]


    def test_two_crossing_pids_each_keep_their_pair(self, state: PerfettoTrackState) -> None:
        """Pid 100 runs ``[500, 1500]`` and pid 200 ``[1000, 5000]``, so the
        two cross and neither gives anything up. Each BEGIN carries a
        ``cmdline`` annotation naming the program that process was
        running."""
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

        row = processes_row_uuids(state, early, late)
        assert lifetime_slices(convert_packets, row) == [], "convert passes must emit no Processes-row slices"
        # One BEGIN/END pair per pid, in ascending start: pid 100 opens and
        # closes at the width it was observed at, then pid 200.
        assert lifetime_slices(closeout, row) == [
            (
                500,
                TrackEventType.SLICE_BEGIN,
                TARGET_ROW_NAME,
                {
                    CMDLINE: "python3 -m early_target",
                    PID: 100,
                    PID_EPOCH: 1,
                },
            ),
            (1_500, TrackEventType.SLICE_END, "", {}),
            (
                1_000,
                TrackEventType.SLICE_BEGIN,
                OTHER_ROW_NAME,
                {
                    CMDLINE: "python3 -m late_target",
                    PID: 200,
                    PID_EPOCH: 1,
                },
            ),
            (5_000, TrackEventType.SLICE_END, "", {}),
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

        assert lifetime_slices(closeout, processes_row_uuids(state, first, second)) == [
            (
                1_000,
                TrackEventType.SLICE_BEGIN,
                TARGET_ROW_NAME,
                {
                    CMDLINE: "python3 -m first_target",
                    PID: 100,
                    PID_EPOCH: 1,
                },
            ),
            (2_000, TrackEventType.SLICE_END, "", {}),
            (
                3_000,
                TrackEventType.SLICE_BEGIN,
                process_track_name(proc(TARGET_PID, 2)),
                {
                    CMDLINE: "python3 -m second_target",
                    PID: 100,
                    PID_EPOCH: 2,
                },
            ),
            (4_000, TrackEventType.SLICE_END, "", {}),
        ]

    def test_process_lifetime_idempotent_across_converts(self, state: PerfettoTrackState) -> None:
        """Two convert passes for the same pid produce a single slice
        pair spanning both batches: the second pass widens the recorded
        span, and the pair is emitted once at closeout."""
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

        row = processes_row_uuids(state, target)
        assert lifetime_slices(convert_packets, row) == []
        # One pair only, spanning the first batch's ts_start to the
        # second batch's ts_stop.
        assert lifetime_slices(closeout, row) == [
            (
                1_000,
                TrackEventType.SLICE_BEGIN,
                TARGET_ROW_NAME,
                {
                    CMDLINE: "python3 -m fake_target",
                    PID: 100,
                    PID_EPOCH: 1,
                },
            ),
            (4_000, TrackEventType.SLICE_END, "", {}),
        ]


class TestAQuietProcessGetsARow:
    """A process gcmon polled and read no collections from. Liveness folded a
    span in and nothing else ever named it, so no convert pass described it.

    Finalization describes it instead, and it draws a row like any other:
    ADR-0028 puts a process gcmon watched and read nothing from on the
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
    The span on the shared ``Processes`` row does not come with it; it waits
    for close (ADR-0028).
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

    def test_close_draws_its_span_at_the_width_it_was_observed_at(self) -> None:
        """``LATE`` is discovered after ``RETIRED``'s row was drawn and opens a
        span inside it. Each has a track of its own, so neither loses an end."""
        state, _packets = self._retire()
        for ts in (2_000, 9_000):
            state.update_process_lifetime(self.LATE, ts)

        closeout = finalize_perfetto_packets(state, sequence_id=1)

        drawn = lifetime_slices(closeout, processes_row_uuids(state, self.RETIRED, self.LATE))
        assert [(ts, event_type, name) for ts, event_type, name, _ in drawn] == [
            (500, TrackEventType.SLICE_BEGIN, TARGET_ROW_NAME),
            (5_000, TrackEventType.SLICE_END, ""),
            (2_000, TrackEventType.SLICE_BEGIN, OTHER_ROW_NAME),
            (9_000, TrackEventType.SLICE_END, ""),
        ]

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
    """``interpreters``, which the exporter counts from what it is holding
    rather than reading off a record.

    It goes on the ``Lifetime`` bar, the row that is the process, and it is
    final the moment gcmon lets go of the pid (ADR-0028).
    """

    BUSY = proc(TARGET_PID)
    LATE = proc(OTHER_PID)

    def _item(self, iid: int, ts_start: int, ts_stop: int) -> GCStatsInfo:
        return pause_item(iid=iid, ts_start=ts_start, ts_stop=ts_stop, heap_size=1024, collected=1, candidates=1)

    def _begin(self, packets: list[bytes], *track_uuids: int) -> dict[str, str | int]:
        """The annotations on the one BEGIN drawn on *track_uuids*."""
        begins = [
            annotations
            for _ts, event_type, _name, annotations in lifetime_slices(packets, track_uuids)
            if event_type == TrackEventType.SLICE_BEGIN
        ]
        assert len(begins) == 1, f"expected one BEGIN on {track_uuids}, got {len(begins)}"
        return begins[0]

    def _read_from(self, *iids: int) -> tuple[PerfettoTrackState, list[bytes]]:
        """One record per iid, converted and then finalized."""
        state = PerfettoTrackState()
        items = [(self.BUSY, self._item(iid, 1_000 * (iid + 1), 2_000 * (iid + 1))) for iid in iids]
        _descriptors, _convert, closeout = convert_items(items, state, sequence_id=1)
        return state, closeout

    def _crossing(self) -> tuple[PerfettoTrackState, list[bytes]]:
        """``LATE`` opens a span inside ``BUSY``'s and closes after it."""
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

    def test_the_bar_of_a_crossed_process_reads_the_same(self) -> None:
        """``BUSY`` is crossed by ``LATE``, and its bar is the interval gcmon
        watched either way."""
        state, closeout = self._crossing()

        assert self._begin(closeout, state.get_process_track_uuid(self.BUSY))["interpreters"] == 0

    def test_the_shared_slice_does_not_carry_interpreters(self) -> None:
        """One count per process, on the row that is the process."""
        state, closeout = self._read_from(0, 1)

        assert "interpreters" not in self._begin(closeout, *processes_row_uuids(state, self.BUSY))


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
            for _ts, event_type, _name, annotations in lifetime_slices(packets, {state.get_process_track_uuid(process)})
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

        shared = next(
            annotations
            for _ts, event_type, _name, annotations in lifetime_slices(
                closeout, processes_row_uuids(state, self.BUSY)
            )
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
        ``TYPE_SLICE_END`` on a ``Processes`` track; closeout is the
        caller's job (see ``finalize_perfetto_packets``)."""
        target = proc(TARGET_PID)
        gc_events = convert_item_to_trace_format(target, pause_item())

        _, packets = convert_trace_events_to_perfetto(gc_events, state, sequence_id=1)

        row = processes_row_uuids(state, target)
        assert lifetime_slices(packets, row) == [], "a convert pass draws nothing on the Processes row"

        # Calling finalize_perfetto_packets now produces exactly one pair.
        closeout = finalize_perfetto_packets(state, sequence_id=1)
        assert [(ts, event_type) for ts, event_type, _name, _ann in lifetime_slices(closeout, row)] == [
            (1_000, TrackEventType.SLICE_BEGIN),
            (2_000, TrackEventType.SLICE_END),
        ]

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
        row = processes_row_uuids(state, proc(TARGET_PID))

        def _count(packets: list[bytes], event_type: int) -> int:
            return sum(1 for _ts, drawn, _name, _ann in lifetime_slices(packets, row) if drawn == event_type)

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
        ends = [ts for ts, drawn, _name, _ann in lifetime_slices(all_packets, row) if drawn == TrackEventType.SLICE_END]
        assert ends == [4_000]

        # Calling finalize again is a no-op (the track is marked emitted).
        assert finalize_perfetto_packets(state, sequence_id=1) == []
