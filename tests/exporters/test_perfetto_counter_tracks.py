"""Tests for counter track shape: Y-axis share keys and the RSS track."""

import pytest

from gcmon.exporters.perfetto_format import convert_trace_events_to_perfetto
from gcmon.exporters.perfetto_track_state import PerfettoTrackState
from gcmon.exporters.trace_converter import counter_display_name
from gcmon.model.names import CANDIDATES, COLLECTED, DURATION, HEAP_SIZE, RSS, UNCOLLECTABLE
from gcmon.model.trace_event import Counter, TraceEvent
from tests.exporters.perfetto_helpers import (
    parse_track_descriptor,
)
from tests.helpers import gen_counter, interpreter_track, proc, process_track

# The process whose counter tracks these tests read.
TARGET_PID: int = 100


def _rss_sample() -> Counter:
    """One RSS reading on the process row. RSS is the metric and the display
    name both, being the only counter outside the per-generation group."""
    return Counter(process_track(TARGET_PID), RSS, RSS, 1_000, 4096)


def _counter_track_y_axis_share_key(
    descriptors: list[bytes],
    track_name: str,
) -> str | None:
    """Find the counter TrackDescriptor whose name equals *track_name*
    and return its ``y_axis_share_key`` (or ``None`` if the
    ``CounterDescriptor`` submessage is empty). Returns ``None`` if no
    such track descriptor exists at all.
    """
    for d in descriptors:
        td = parse_track_descriptor(d)
        if td is None:
            continue
        if td.name != track_name:
            continue
        if not td.HasField("counter") or td.counter.SerializeToString() == b"":
            return None
        return td.counter.y_axis_share_key or None
    return None


class TestCounterTrackYAxisShareKey:
    """End-to-end wire tests that drive ``convert_trace_events_to_perfetto``
    and inspect the resulting counter track descriptors for the
    ``y_axis_share_key`` value."""

    def test_grouped_counters_share_y_axis_by_metric(self, state: PerfettoTrackState) -> None:
        events: list[TraceEvent] = [
            gen_counter(interpreter_track(TARGET_PID, 0), 0, COLLECTED, 1_000, 100),
            gen_counter(interpreter_track(TARGET_PID, 0), 0, CANDIDATES, 1_000, 50),
            gen_counter(interpreter_track(TARGET_PID, 0), 0, DURATION, 1_000, 0.005),
            gen_counter(interpreter_track(TARGET_PID, 0), 1, COLLECTED, 1_001, 80),
            gen_counter(interpreter_track(TARGET_PID, 0), 1, CANDIDATES, 1_001, 40),
            gen_counter(interpreter_track(TARGET_PID, 0), 1, DURATION, 1_001, 0.004),
            gen_counter(interpreter_track(TARGET_PID, 0), 2, COLLECTED, 1_002, 60),
            gen_counter(interpreter_track(TARGET_PID, 0), 2, CANDIDATES, 1_002, 30),
            gen_counter(interpreter_track(TARGET_PID, 0), 2, DURATION, 1_002, 0.003),
        ]

        descriptors, _ = convert_trace_events_to_perfetto(
            events,
            state,
            sequence_id=1,
        )

        for gen in (0, 1, 2):
            for metric in (COLLECTED, CANDIDATES, DURATION):
                track_name = counter_display_name(gen, metric)
                assert _counter_track_y_axis_share_key(descriptors, track_name) == metric, (
                    f"{track_name} should share Y-axis under {metric!r}"
                )

    def test_heap_size_has_no_share_key(self, state: PerfettoTrackState) -> None:
        events: list[TraceEvent] = [
            Counter(interpreter_track(TARGET_PID, 0), HEAP_SIZE, HEAP_SIZE, 1_000, 4096),
        ]

        descriptors, _ = convert_trace_events_to_perfetto(
            events,
            state,
            sequence_id=1,
        )

        assert _counter_track_y_axis_share_key(descriptors, HEAP_SIZE) is None

    def test_uncollectable_share_key_emitted_when_nonzero(self, state: PerfettoTrackState) -> None:
        events: list[TraceEvent] = [
            gen_counter(interpreter_track(TARGET_PID, 0), 0, COLLECTED, 1_000, 1),
            gen_counter(interpreter_track(TARGET_PID, 0), 0, UNCOLLECTABLE, 1_000, 1),
            gen_counter(interpreter_track(TARGET_PID, 0), 0, CANDIDATES, 1_000, 1),
            gen_counter(interpreter_track(TARGET_PID, 0), 0, DURATION, 1_000, 1),
        ]

        descriptors, _ = convert_trace_events_to_perfetto(
            events,
            state,
            sequence_id=1,
        )

        assert _counter_track_y_axis_share_key(descriptors, counter_display_name(0, UNCOLLECTABLE)) == UNCOLLECTABLE

    def test_different_pids_have_independent_share_groups(self, state: PerfettoTrackState) -> None:
        """Two pids each emit a ``G0 collected`` counter. Both must
        carry ``y_axis_share_key = "collected"``; the parent-scoping
        is what the docs require for safe sharing, and is implicit in
        the existing per-``(pid, iid)`` ``GC Metrics`` group.
        """
        events: list[TraceEvent] = [
            gen_counter(interpreter_track(TARGET_PID, 0), 0, COLLECTED, 1_000, 10),
            gen_counter(interpreter_track(TARGET_PID, 0), 0, CANDIDATES, 1_000, 5),
            gen_counter(interpreter_track(200, 0), 0, COLLECTED, 1_001, 20),
            gen_counter(interpreter_track(200, 0), 0, CANDIDATES, 1_001, 6),
        ]

        descriptors, _ = convert_trace_events_to_perfetto(
            events,
            state,
            sequence_id=1,
        )

        parent_uuids: set[int] = set()
        for d in descriptors:
            td = parse_track_descriptor(d)
            if td is None:
                continue
            if td.name != counter_display_name(0, COLLECTED):
                continue
            parent = td.parent_uuid
            assert parent != 0
            parent_uuids.add(parent)
            assert td.HasField("counter") and td.counter.SerializeToString() != b""
            assert td.counter.y_axis_share_key == COLLECTED
        assert len(parent_uuids) == 2, (
            f"expected G0 collected tracks under 2 distinct parent groups "
            f"(one per pid), got {len(parent_uuids)}: {parent_uuids}"
        )


class TestRssCounterTrack:
    """RSS counter track shape and process-level parenting."""

    def test_counter_track_parented_to_process(self, state: PerfettoTrackState) -> None:
        events: list[TraceEvent] = [
            _rss_sample(),
        ]

        descriptors, _ = convert_trace_events_to_perfetto(events, state, sequence_id=1)

        proc_uuid = state.get_process_track_uuid(proc(TARGET_PID))
        ctr_key = (process_track(TARGET_PID), RSS)
        assert state.has_counter_track(*ctr_key)
        ctr_uuid = state.get_or_create_counter_track_uuid(*ctr_key)
        found_ctr = False
        for d in descriptors:
            td = parse_track_descriptor(d)
            if td is None:
                continue
            if td.uuid == ctr_uuid:
                assert td.parent_uuid == proc_uuid, (
                    f"RSS counter track parent should be process track; "
                    f"got parent_uuid={td.parent_uuid}, expected {proc_uuid}"
                )
                assert td.name == RSS
                assert td.HasField("counter")
                found_ctr = True
                break
        assert found_ctr, "RSS counter track descriptor was not emitted"

    def test_display_name_is_metric_name(self, state: PerfettoTrackState) -> None:
        """The RSS track carries the ``display_name`` the producer wrote,
        ``"rss"``, unqualified by any owner."""
        events: list[TraceEvent] = [
            Counter(process_track(TARGET_PID), RSS, RSS, 1_000, 8192),
        ]

        descriptors, _ = convert_trace_events_to_perfetto(events, state, sequence_id=1)

        ctr_key = (process_track(TARGET_PID), RSS)
        ctr_uuid = state.get_or_create_counter_track_uuid(*ctr_key)
        for d in descriptors:
            td = parse_track_descriptor(d)
            if td is not None and td.uuid == ctr_uuid:
                assert td.name == RSS
                return
        pytest.fail("RSS counter track descriptor not found")

    def test_the_rss_track_gets_no_thread_descriptor(self, state: PerfettoTrackState) -> None:
        """RSS is process-level, so no ``ThreadDescriptor`` track is emitted
        for it."""
        events: list[TraceEvent] = [
            _rss_sample(),
        ]

        descriptors, _ = convert_trace_events_to_perfetto(events, state, sequence_id=1)

        parsed = [td for td in map(parse_track_descriptor, descriptors) if td]
        assert RSS in [td.name for td in parsed]
        assert [td.name for td in parsed if td.HasField("thread")] == []

    def test_multiple_pids_get_separate_rss_tracks(self, state: PerfettoTrackState) -> None:
        events: list[TraceEvent] = [
            _rss_sample(),
            Counter(process_track(200), RSS, RSS, 2_000, 8192),
        ]

        descriptors, _ = convert_trace_events_to_perfetto(events, state, sequence_id=1)

        rss = [td for td in map(parse_track_descriptor, descriptors) if td and td.name == RSS]
        assert [td.parent_uuid for td in rss] == [
            state.get_process_track_uuid(proc(TARGET_PID)),
            state.get_process_track_uuid(proc(200)),
        ]
        assert len({td.uuid for td in rss}) == 2

    def test_rss_renders_at_top_level(self, state: PerfettoTrackState) -> None:
        """RSS is a top-level counter metric, parented directly to the
        process track, NOT inside the GC Metrics group."""
        events: list[TraceEvent] = [
            # RSS sample (process-level)
            _rss_sample(),
            # GC counter (per interpreter, inside the GC Metrics group)
            gen_counter(interpreter_track(TARGET_PID, 0), 0, COLLECTED, 1_000, 42),
            gen_counter(interpreter_track(TARGET_PID, 0), 0, CANDIDATES, 1_000, 10),
            gen_counter(interpreter_track(TARGET_PID, 0), 0, DURATION, 1_000, 0.005),
        ]

        descriptors, _ = convert_trace_events_to_perfetto(events, state, sequence_id=1)

        proc_uuid = state.get_process_track_uuid(proc(TARGET_PID))
        rss_key = (process_track(TARGET_PID), RSS)
        rss_uuid = state.get_or_create_counter_track_uuid(*rss_key)
        g0_uuid = state.get_or_create_counter_track_uuid(
            interpreter_track(TARGET_PID, 0), counter_display_name(0, DURATION)
        )
        rss_parent = None
        g0_parent = None
        for d in descriptors:
            td = parse_track_descriptor(d)
            if td is None:
                continue
            if td.uuid == rss_uuid:
                rss_parent = td.parent_uuid
            elif td.uuid == g0_uuid:
                g0_parent = td.parent_uuid
        assert rss_parent == proc_uuid, "RSS should be parented directly to process track"
        assert g0_parent is not None and g0_parent != proc_uuid, (
            "GC counters should be inside GC Metrics group, not directly on process track"
        )
