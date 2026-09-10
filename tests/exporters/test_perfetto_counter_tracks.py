"""Tests for counter track shape: Y-axis share keys and the RSS track."""

import pytest

from gcmon.exporters.perfetto_format import convert_trace_events_to_perfetto
from gcmon.exporters.perfetto_track_state import PerfettoTrackState
from gcmon.model.trace_event import Counter, TraceEvent
from tests.exporters.perfetto_helpers import (
    parse_track_descriptor,
)
from tests.helpers import interpreter_track, proc, process_track


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

    def test_grouped_counters_share_y_axis_by_metric(self) -> None:
        state = PerfettoTrackState()
        events: list[TraceEvent] = [
            Counter(interpreter_track(100, 0), "collected", "G0 collected", 1_000, 100),
            Counter(interpreter_track(100, 0), "candidates", "G0 candidates", 1_000, 50),
            Counter(interpreter_track(100, 0), "duration", "G0 duration", 1_000, 0.005),
            Counter(interpreter_track(100, 0), "collected", "G1 collected", 1_001, 80),
            Counter(interpreter_track(100, 0), "candidates", "G1 candidates", 1_001, 40),
            Counter(interpreter_track(100, 0), "duration", "G1 duration", 1_001, 0.004),
            Counter(interpreter_track(100, 0), "collected", "G2 collected", 1_002, 60),
            Counter(interpreter_track(100, 0), "candidates", "G2 candidates", 1_002, 30),
            Counter(interpreter_track(100, 0), "duration", "G2 duration", 1_002, 0.003),
        ]
        descriptors, _ = convert_trace_events_to_perfetto(
            events,
            state,
            sequence_id=1,
        )
        for gen in ("G0", "G1", "G2"):
            for metric in ("collected", "candidates", "duration"):
                track_name = f"{gen} {metric}"
                assert _counter_track_y_axis_share_key(descriptors, track_name) == metric, (
                    f"{track_name} should share Y-axis under {metric!r}"
                )

    def test_heap_size_has_no_share_key(self) -> None:
        state = PerfettoTrackState()
        events: list[TraceEvent] = [
            Counter(interpreter_track(100, 0), "heap_size", "heap_size", 1_000, 4096),
        ]
        descriptors, _ = convert_trace_events_to_perfetto(
            events,
            state,
            sequence_id=1,
        )
        assert _counter_track_y_axis_share_key(descriptors, "heap_size") is None

    def test_uncollectable_share_key_emitted_when_nonzero(self) -> None:
        state = PerfettoTrackState()
        events: list[TraceEvent] = [
            Counter(interpreter_track(100, 0), "collected", "G0 collected", 1_000, 1),
            Counter(interpreter_track(100, 0), "uncollectable", "G0 uncollectable", 1_000, 1),
            Counter(interpreter_track(100, 0), "candidates", "G0 candidates", 1_000, 1),
            Counter(interpreter_track(100, 0), "duration", "G0 duration", 1_000, 1),
        ]
        descriptors, _ = convert_trace_events_to_perfetto(
            events,
            state,
            sequence_id=1,
        )
        assert _counter_track_y_axis_share_key(descriptors, "G0 uncollectable") == "uncollectable"

    def test_different_pids_have_independent_share_groups(self) -> None:
        """Two pids each emit a ``G0 collected`` counter. Both must
        carry ``y_axis_share_key = "collected"``; the parent-scoping
        is what the docs require for safe sharing, and is implicit in
        the existing per-``(pid, tid)`` ``GC Metrics`` group.
        """
        state = PerfettoTrackState()
        events: list[TraceEvent] = [
            Counter(interpreter_track(100, 0), "collected", "G0 collected", 1_000, 10),
            Counter(interpreter_track(100, 0), "candidates", "G0 candidates", 1_000, 5),
            Counter(interpreter_track(200, 0), "collected", "G0 collected", 1_001, 20),
            Counter(interpreter_track(200, 0), "candidates", "G0 candidates", 1_001, 6),
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
            if td.name != "G0 collected":
                continue
            parent = td.parent_uuid
            assert parent != 0
            parent_uuids.add(parent)
            assert td.HasField("counter") and td.counter.SerializeToString() != b""
            assert td.counter.y_axis_share_key == "collected"
        assert len(parent_uuids) == 2, (
            f"expected G0 collected tracks under 2 distinct parent groups "
            f"(one per pid), got {len(parent_uuids)}: {parent_uuids}"
        )


class TestRssCounterTrack:
    """RSS counter track shape and process-level parenting."""

    def test_counter_track_parented_to_process(self) -> None:
        state = PerfettoTrackState()
        events: list[TraceEvent] = [
            Counter(process_track(100), "rss", "rss", 1_000, 4096),
        ]
        descriptors, _ = convert_trace_events_to_perfetto(events, state, sequence_id=1)
        proc_uuid = state.get_process_track_uuid(proc(100))
        ctr_key = (process_track(100), "rss")
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
                assert td.name == "rss"
                assert td.HasField("counter")
                found_ctr = True
                break
        assert found_ctr, "RSS counter track descriptor was not emitted"

    def test_display_name_is_metric_name(self) -> None:
        """The RSS track carries the ``display_name`` the producer wrote,
        ``"rss"``, unqualified by any owner."""
        state = PerfettoTrackState()
        events: list[TraceEvent] = [
            Counter(process_track(100), "rss", "rss", 1_000, 8192),
        ]
        descriptors, _ = convert_trace_events_to_perfetto(events, state, sequence_id=1)
        ctr_key = (process_track(100), "rss")
        ctr_uuid = state.get_or_create_counter_track_uuid(*ctr_key)
        for d in descriptors:
            td = parse_track_descriptor(d)
            if td is not None and td.uuid == ctr_uuid:
                assert td.name == "rss"
                return
        pytest.fail("RSS counter track descriptor not found")

    def test_no_thread_descriptor_for_rss_tid(self) -> None:
        """No ``ThreadDescriptor`` track should be emitted for
        ``tid=-1``; RSS is process-level."""
        state = PerfettoTrackState()
        events: list[TraceEvent] = [
            Counter(process_track(100), "rss", "rss", 1_000, 4096),
        ]
        descriptors, _ = convert_trace_events_to_perfetto(events, state, sequence_id=1)
        for d in descriptors:
            td = parse_track_descriptor(d)
            if td is not None and td.HasField("thread"):
                pytest.fail(f"unexpected thread descriptor for RSS: uuid={td.uuid}")

    def test_multiple_pids_get_separate_rss_tracks(self) -> None:
        state = PerfettoTrackState()
        events: list[TraceEvent] = [
            Counter(process_track(100), "rss", "rss", 1_000, 4096),
            Counter(process_track(200), "rss", "rss", 2_000, 8192),
        ]
        _, _ = convert_trace_events_to_perfetto(events, state, sequence_id=1)
        for pid in (100, 200):
            ctr_key = (process_track(pid), "rss")
            assert state.has_counter_track(*ctr_key), f"no RSS track for pid {pid}"
        # Each RSS counter track is parented to the respective process
        # track, and process tracks have distinct UUIDs.
        assert state.get_process_track_uuid(proc(100)) != state.get_process_track_uuid(proc(200))

    def test_rss_renders_at_top_level(self) -> None:
        """RSS is a top-level counter metric, parented directly to the
        process track, NOT inside the GC Metrics group."""
        state = PerfettoTrackState()
        events: list[TraceEvent] = [
            # RSS sample (tid=-1, process-level)
            Counter(process_track(100), "rss", "rss", 1_000, 4096),
            # GC counter (tid=0, thread-level, inside GC Metrics group)
            Counter(interpreter_track(100, 0), "collected", "G0 collected", 1_000, 42),
            Counter(interpreter_track(100, 0), "candidates", "G0 candidates", 1_000, 10),
            Counter(interpreter_track(100, 0), "duration", "G0 duration", 1_000, 0.005),
        ]
        descriptors, _ = convert_trace_events_to_perfetto(events, state, sequence_id=1)
        proc_uuid = state.get_process_track_uuid(proc(100))
        rss_key = (process_track(100), "rss")
        rss_uuid = state.get_or_create_counter_track_uuid(*rss_key)
        g0_uuid = state.get_or_create_counter_track_uuid(interpreter_track(100, 0), "G0 duration")
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
