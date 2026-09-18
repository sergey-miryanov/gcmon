"""One `Slice` becomes the BEGIN/END pair the wire format has.

Perfetto has no complete-slice event, so a span's two ends are written as two
packets rather than as one. The pair goes out adjacent rather than interleaved into stack order,
which is what `finalize_perfetto_packets` already does on the `Processes`
track (ADR-0011); the nesting is left to the trace processor, which sorts by
timestamp and breaks ties by position in the sequence.

ADR-0011's argument does not carry over whole, because it leans on gcmon
naming every END there and a GC row's END is anonymous. So the shapes where
that difference could show -- two slices meeting at a timestamp, two ending at
one, two starting at one -- are asked of the real trace processor here rather
than reasoned about.
"""

from pathlib import Path

import pytest
from perfetto.protos.perfetto.trace.perfetto_trace_pb2 import TracePacket
from perfetto.trace_processor import TraceProcessor

from gcmon.exporters.perfetto_builders import build_trace
from gcmon.exporters.perfetto_format import _PAUSE_TRACK_NAME, convert_trace_events_to_perfetto
from gcmon.exporters.perfetto_process_lifetime import process_track_name
from gcmon.exporters.perfetto_proto import TrackEventType
from gcmon.exporters.perfetto_track_state import PerfettoTrackState
from gcmon.model.names import (
    DEDUCE_UNREACHABLE,
    DELETE_GARBAGE,
    GENERATION,
    MARK_ALIVE,
    PAUSE,
    gc_pause_slice_name,
    phase_slice_name,
)
from gcmon.model.trace_event import EventArgs, Slice, TraceEvent
from tests.exporters.perfetto_helpers import parse_track_descriptor
from tests.helpers import interpreter_track, open_trace_processor, proc

PID = 4242
ROW = interpreter_track(PID, 0)
ROW_PROCESS_NAME = process_track_name(proc(PID))
SEQUENCE_ID = 7

type Converted = tuple[list[bytes], list[bytes]]
"""What a conversion pass returns: its track descriptors and its packets."""


def _track_events(packets: list[bytes]) -> list[TracePacket]:
    parsed = [TracePacket() for _ in packets]
    for packet, raw in zip(parsed, packets, strict=True):
        packet.ParseFromString(raw)
    return [p for p in parsed if p.HasField("track_event")]


def _slice_packets(packets: list[bytes]) -> list[TracePacket]:
    """The slice packets, in emission order."""
    return [
        p
        for p in _track_events(packets)
        if p.track_event.type in (TrackEventType.SLICE_BEGIN, TrackEventType.SLICE_END)
    ]


def _convert(events: list[TraceEvent]) -> Converted:
    return convert_trace_events_to_perfetto(events, PerfettoTrackState(), SEQUENCE_ID)


def _pause_slice(ts_stop: int = 1_500, args: EventArgs | None = None) -> Slice:
    """The span these tests expand: one gen-0 pause on the interpreter row."""
    return Slice(ROW, gc_pause_slice_name(0), PAUSE.category, 1_000, ts_stop, args or {})


@pytest.fixture
def expansion() -> Converted:
    """What `_pause_slice()` converts to, as ``(descriptors, packets)``."""
    return _convert([_pause_slice()])


class TestASliceExpandsIntoAPair:
    """The packets one `Slice` produces, read back off the wire."""

    def test_a_slice_produces_a_begin_then_an_end(self, expansion: Converted) -> None:
        _, packets = expansion
        events = [p.track_event for p in _slice_packets(packets)]
        assert [e.type for e in events] == [TrackEventType.SLICE_BEGIN, TrackEventType.SLICE_END]

    def test_the_begin_carries_the_name_the_category_and_the_args(self) -> None:
        _, packets = _convert([_pause_slice(args={GENERATION: 0})])
        begin = _slice_packets(packets)[0]
        assert begin.timestamp == 1_000
        assert begin.track_event.name == gc_pause_slice_name(0)
        assert list(begin.track_event.categories) == [PAUSE.category]
        assert [a.name for a in begin.track_event.debug_annotations] == [GENERATION]

    def test_the_end_lands_at_ts_stop_and_carries_only_the_track(self, expansion: Converted) -> None:
        """A `Slice` states both its ends; the second packet is where the
        second one is written."""
        _, packets = expansion
        end = _slice_packets(packets)[1]
        assert end.timestamp == 1_500
        assert not end.track_event.name
        assert not end.track_event.categories
        assert not end.track_event.debug_annotations

    def test_both_packets_name_the_track_the_slice_names(self, expansion: Converted) -> None:
        descriptors, packets = expansion

        row = [td.uuid for td in map(parse_track_descriptor, descriptors) if td and td.name == _PAUSE_TRACK_NAME]
        assert [p.track_event.track_uuid for p in _slice_packets(packets)] == row * 2

    def test_a_zero_length_slice_still_produces_both_packets(self) -> None:
        """A span whose ends are equal. BEGIN first, so it reads as
        ``dur = 0`` rather than ``-1`` (ADR-0011)."""
        _, packets = _convert([_pause_slice(ts_stop=1_000)])
        events = _slice_packets(packets)
        assert [e.track_event.type for e in events] == [TrackEventType.SLICE_BEGIN, TrackEventType.SLICE_END]
        assert [e.timestamp for e in events] == [1_000, 1_000]

    def test_a_slice_describes_its_track_before_naming_it(self, expansion: Converted) -> None:
        descriptors, _ = expansion
        named = [td.name for td in (parse_track_descriptor(d) for d in descriptors) if td is not None and td.name]
        assert ROW_PROCESS_NAME in named
        assert _PAUSE_TRACK_NAME in named

    def test_a_slice_places_no_instant(self, expansion: Converted) -> None:
        """The conversion pass writes nothing but the pair. The process row
        is kept rendered by the ``Lifetime`` slice ``finalize_perfetto_packets``
        draws at close, which is not an instant and not emitted here."""
        _, packets = expansion
        instants = [p.track_event.name for p in _track_events(packets) if p.track_event.type == TrackEventType.INSTANT]
        assert instants == []


def _pause_row(tp: TraceProcessor) -> list[tuple[str, int, int, int]]:
    """Every slice on the interpreter's row, as ``(name, ts, dur, depth)``."""
    return [
        (row.name, row.ts, row.dur, row.depth)
        for row in tp.query(
            "SELECT s.name, s.ts, s.dur, s.depth FROM slice s "
            "JOIN process_track pt ON s.track_id = pt.id "
            "JOIN process p ON pt.upid = p.upid "
            # By name: `p.pid` is the row's, one gcmon hands out (ADR-0011).
            f"WHERE pt.name = '{_PAUSE_TRACK_NAME}' AND p.name = '{ROW_PROCESS_NAME}' ORDER BY s.ts, s.depth"
        )
    ]


def _as_read_back(rows: list[Slice], tmp_path: Path, name: str) -> list[tuple[str, int, int, int]]:
    """*rows* through the encoder and the real trace processor."""
    descriptors, packets = _convert(list(rows))
    path = tmp_path / f"{name}.pftrace"
    path.write_bytes(build_trace([*descriptors, *packets]))
    with open_trace_processor(path) as tp:
        return _pause_row(tp)


def _nested(inner_start: int, inner_stop: int) -> list[Slice]:
    """A span from 1_000 to 2_000 and a shorter one inside it.

    The outer span is the same every time; what each test varies is where
    the inner one sits, because that is what decides the nesting.
    """
    return [
        Slice(ROW, "outer", "c", 1_000, 2_000, {}),
        Slice(ROW, "inner", "c", inner_start, inner_stop, {}),
    ]


class TestTheTraceProcessorBuildsTheNesting:
    """gcmon hands over one duration per span and no stack. These are the
    shapes where that could go wrong."""

    def test_a_slice_inside_another_reads_back_nested(self, tmp_path: Path) -> None:
        row = _as_read_back(
            [
                *_nested(1_200, 1_500),
            ],
            tmp_path,
            "nested",
        )
        assert row == [("outer", 1_000, 1_000, 0), ("inner", 1_200, 300, 1)]

    def test_two_slices_that_touch_read_back_as_siblings(self, tmp_path: Path) -> None:
        """The tie rule: one span's END shares a timestamp with the next
        one's BEGIN, and the wrong order round makes them nest.

        `Finalize Garbage` starts at `ts_handle_weakref_callbacks_stop`, so
        this is the shape of an ordinary record rather than a corner.
        """
        row = _as_read_back(
            [
                Slice(ROW, "first", "c", 1_000, 1_500, {}),
                Slice(ROW, "second", "c", 1_500, 2_000, {}),
            ],
            tmp_path,
            "touching",
        )
        assert row == [("first", 1_000, 500, 0), ("second", 1_500, 500, 0)]

    def test_a_child_ending_where_its_parent_ends_keeps_both_durations(self, tmp_path: Path) -> None:
        """Two anonymous ENDs at one timestamp, the case ADR-0011's argument
        does not cover: it relies on the `Processes` track naming every END.

        They need no rule because both slices end there, so each takes the
        same duration whichever is popped first. `Delete Garbage` ending at
        `ts_stop` is this shape.
        """
        row = _as_read_back(
            [
                *_nested(1_800, 2_000),
            ],
            tmp_path,
            "co_terminating",
        )
        assert row == [("outer", 1_000, 1_000, 0), ("inner", 1_800, 200, 1)]

    def test_a_child_starting_where_its_parent_starts_reads_back_nested(self, tmp_path: Path) -> None:
        """`Mark Alive` can start at `ts_start`. The pause is built first, so
        its BEGIN wins the tie and the sub-phase nests inside it."""
        row = _as_read_back(
            [
                *_nested(1_000, 1_300),
            ],
            tmp_path,
            "co_starting",
        )
        assert row == [("outer", 1_000, 1_000, 0), ("inner", 1_000, 300, 1)]

    def test_a_whole_record_shape_reads_back_intact(self, tmp_path: Path) -> None:
        """A pause with three sub-phases: one starting with it, two meeting
        each other, the last ending with it. Every tie above at once."""
        row = _as_read_back(
            [
                Slice(ROW, gc_pause_slice_name(2), PAUSE.category, 1_000, 1_900, {}),
                Slice(ROW, phase_slice_name(MARK_ALIVE, 2), MARK_ALIVE.category, 1_000, 1_200, {}),
                Slice(ROW, phase_slice_name(DEDUCE_UNREACHABLE, 2), DEDUCE_UNREACHABLE.category, 1_400, 1_700, {}),
                Slice(ROW, phase_slice_name(DELETE_GARBAGE, 2), DELETE_GARBAGE.category, 1_700, 1_900, {}),
            ],
            tmp_path,
            "record",
        )
        assert row == [
            (gc_pause_slice_name(2), 1_000, 900, 0),
            (phase_slice_name(MARK_ALIVE, 2), 1_000, 200, 1),
            (phase_slice_name(DEDUCE_UNREACHABLE, 2), 1_400, 300, 1),
            (phase_slice_name(DELETE_GARBAGE, 2), 1_700, 200, 1),
        ]
