"""Does the trace processor draw loss spans where gcmon means them to go?

Two claims that nothing at the wire level can settle. First, that a loss span
lands on a row of its own rather than among the collections, which is the whole
point of the `LossTrack` and of a track descriptor Perfetto could ignore.
Second, that consecutive poll intervals come back as neighbours on that row
rather than as one span inside another. Both ask trace processor directly, and
are marked ``fuzz`` for the cost.

The negative control matters more than usual. A track is a stack and an END
closes whatever is on top, so a pair of overlapping spans produces a trace that
parses cleanly, reports ``misplaced_end_event = 0``, and hands each span the
other's width. Without evidence of that, the positive test would pass equally
in a world where the shape did not matter.
"""

from pathlib import Path

import pytest

from gcmon.exporters.perfetto_builders import build_trace
from gcmon.exporters.perfetto_format import convert_trace_events_to_perfetto, finalize_perfetto_packets
from gcmon.exporters.perfetto_track_state import PerfettoTrackState
from gcmon.exporters.trace_converter import convert_item_to_trace_format, convert_loss_to_trace_format
from gcmon.model.data import GCStatsInfo, LossMsg
from gcmon.model.trace_event import TraceEvent
from tests.helpers import create_mock_loss_item, open_trace_processor, proc

pytestmark = pytest.mark.fuzz

PID = 100
IID = 0
SEQUENCE_ID = 4242

Slice = tuple[str, str, int, int]
"""``(track_name, slice_name, ts, dur)``."""


def _pause(ts_start: int, ts_stop: int, collections: int) -> GCStatsInfo:
    return GCStatsInfo(
        gen=0,
        iid=IID,
        ts_start=ts_start,
        ts_stop=ts_stop,
        heap_size=1024,
        collections=collections,
        collected=1,
        uncollectable=0,
        candidates=1,
        duration=0.001,
    )


def _write(events: list[TraceEvent], tmp_path: Path, name: str) -> Path:
    """Write the trace the way the exporter does, close included: the
    ``Lifetime`` bar the process row carries is drawn there, and it is what a
    misparented loss span would reshape."""
    state = PerfettoTrackState()
    descriptors, packets = convert_trace_events_to_perfetto(events, state, SEQUENCE_ID)
    closeout = finalize_perfetto_packets(state, SEQUENCE_ID)
    path = tmp_path / f"{name}.pftrace"
    path.write_bytes(build_trace([*descriptors, *packets, *closeout]))
    return path


def _load(events: list[TraceEvent], tmp_path: Path, name: str) -> tuple[int, list[Slice]]:
    path = _write(events, tmp_path, name)

    with open_trace_processor(path) as tp:
        rows = list(tp.query("SELECT value FROM stats WHERE name = 'misplaced_end_event'"))
        misplaced = rows[0].value if rows else 0
        slices = [
            (row.track_name, row.name, row.ts, row.dur)
            for row in tp.query(
                # The process's own row and the shared `Processes` row are
                # the span rows, asserted on in `_process_slices` and in the
                # exporter's own suite.
                "SELECT t.name AS track_name, s.name, s.ts, s.dur "
                "FROM slice s JOIN track t ON s.track_id = t.id "
                "WHERE s.depth = 0 "
                f"AND t.name NOT IN ('Processes', 'Process {PID}') "
                "ORDER BY s.ts"
            )
        ]
    return misplaced, slices


def _loss(ts_start: int, ts_stop: int, lost_pause: int, iid: int = IID, gen: int = 0) -> LossMsg:
    return create_mock_loss_item(
        iid=iid, gen=gen, ts_start=ts_start, ts_stop=ts_stop, lost_count=1, lost_pause_ns=lost_pause
    )


LossSlice = tuple[str, int, int, int]
"""``(slice_name, ts, dur, depth)`` on a ``GC Loss`` row."""


def _loss_row(events: list[TraceEvent], tmp_path: Path, name: str) -> tuple[int, list[LossSlice]]:
    """Every slice the trace processor built on a loss row, with its depth.

    Depth is the whole point: it is what says which span the processor decided
    was the parent, and it is the only place a wrongly ordered emission shows
    up at all.
    """
    with open_trace_processor(_write(events, tmp_path, name)) as tp:
        rows = list(tp.query("SELECT value FROM stats WHERE name = 'misplaced_end_event'"))
        misplaced = rows[0].value if rows else 0
        slices = [
            (row.name, row.ts, row.dur, row.depth)
            for row in tp.query(
                "SELECT s.name, s.ts, s.dur, s.depth FROM slice s JOIN track t ON s.track_id = t.id "
                "WHERE t.name LIKE 'GC Loss%' ORDER BY s.ts, s.depth"
            )
        ]
    return misplaced, slices


def _consecutive_intervals() -> list[LossMsg]:
    """Two polls' worth, meeting at the instant between them.

    Touching edges are the shape worth putting to the processor: it sorts by
    timestamp, so one span's END and the next one's BEGIN arrive together and
    only their order says which reading is meant.
    """
    return [_loss(2_000, 5_000, 100), _loss(5_000, 9_000, 100)]


def _events(*losses: LossMsg) -> list[TraceEvent]:
    """A collection, the gap the losses sit in, then the next collection."""
    events: list[TraceEvent] = [
        *convert_item_to_trace_format(proc(PID), _pause(1_000, 2_000, 1)),
    ]
    for loss in losses:
        events.extend(convert_loss_to_trace_format(proc(PID), loss))
    events.extend(convert_item_to_trace_format(proc(PID), _pause(9_000, 10_000, 3)))
    return events


def test_a_loss_span_lands_on_its_own_track(tmp_path: Path) -> None:
    """A `LossTrack` has to survive as a real, named row: the descriptor is
    emitted off the slices, and nothing else in the trace refers to it."""
    misplaced, slices = _load(_events(_loss(2_000, 9_000, 500)), tmp_path, "own_track")

    assert misplaced == 0
    assert slices == [
        ("GC Pauses", "GC Pause(0)", 1_000, 1_000),
        ("GC Loss", "GC Loss(0)", 2_000, 7_000),
        ("GC Pauses", "GC Pause(0)", 9_000, 1_000),
    ]


def test_the_bar_is_the_whole_interval(tmp_path: Path) -> None:
    """Drawn end to end, over the collections in it: what is known is the
    interval between two reads, not where in it the lost records ran."""
    _, slices = _load(_events(_loss(2_000, 9_000, 500)), tmp_path, "fills_gap")

    loss = next((ts, dur) for _t, name, ts, dur in slices if name == "GC Loss(0)")
    assert loss == (2_000, 7_000)


def test_two_interpreters_get_two_rows(tmp_path: Path) -> None:
    """Both rows are named ``GC Loss``, so what has to keep them apart is the
    group each sits in. Sharing a name *and* a parent is what the trace
    processor merges (ADR-0027)."""
    events = _events(_loss(2_000, 9_000, 500), _loss(2_000, 9_000, 500, iid=7))

    with open_trace_processor(_write(events, tmp_path, "two_rows")) as tp:
        rows = [
            (row.iname, row.id)
            for row in tp.query(
                "SELECT ig.name AS iname, t.id AS id FROM track t "
                "JOIN track ig ON t.parent_id = ig.id "
                "WHERE t.name = 'GC Loss' ORDER BY ig.name"
            )
        ]

    assert [iname for iname, _id in rows] == ["Interpreter 0", "Interpreter 7"]
    assert len({track_id for _iname, track_id in rows}) == 2, f"the two rows merged: {rows}"


def _process_slices(events: list[TraceEvent], tmp_path: Path, name: str) -> list[Slice]:
    """What sits on the process track itself, which `_load` filters out."""
    with open_trace_processor(_write(events, tmp_path, name)) as tp:
        return [
            (row.track_name, row.name, row.ts, row.dur)
            for row in tp.query(
                "SELECT t.name AS track_name, s.name, s.ts, s.dur FROM slice s "
                f"JOIN track t ON s.track_id = t.id WHERE t.name = 'Process {PID}'"
            )
        ]


def test_the_process_row_is_untouched_by_loss_spans(tmp_path: Path) -> None:
    """The loss track sits inside its interpreter's group, so a descriptor
    naming the wrong parent would land its spans on the process's own row and
    reshape the `Lifetime` bar ADR-0013 keeps clear of it.

    The losses here sit inside the interval the collections already bound, so
    the bar is the same width either way and any difference is the spans
    landing where they should not."""
    without = _process_slices(_events(), tmp_path, "no_loss")
    with_loss = _process_slices(_events(_loss(2_000, 9_000, 500)), tmp_path, "with_loss")

    assert len(without) == 1
    assert with_loss == without


def test_consecutive_intervals_come_back_as_neighbours(tmp_path: Path) -> None:
    """Both at depth 0, each keeping its own width. The processor sorts by
    timestamp and the two spans meet at one, so this is where the converter's
    time order earns its place."""
    misplaced, slices = _loss_row(_events(*_consecutive_intervals()), tmp_path, "neighbours")

    assert misplaced == 0
    assert slices == [
        ("GC Loss(0)", 2_000, 3_000, 0),
        ("GC Loss(0)", 5_000, 4_000, 0),
    ]


def test_an_overlapping_pair_is_silently_reshaped(tmp_path: Path) -> None:
    """The negative control, and what makes the flat row worth asserting.

    Two spans that genuinely overlap, a shape one span per poll interval
    cannot produce, and one the old per-generation windows produced by design.
    A track is a stack and an END closes whatever is on top, so the first END
    closes the wrong span: both come back at widths neither was given, one
    parented on the other, and the trace processor reports nothing wrong.
    """
    overlapping = [_loss(2_000, 9_000, 100), _loss(4_000, 12_000, 100)]

    misplaced, slices = _loss_row(_events(*overlapping), tmp_path, "overlapping")

    assert misplaced == 0
    # Given 2_000..9_000 and 4_000..12_000. What comes back: the first END
    # closes the span opened second, so the outer one runs to 12_000.
    assert [(ts, dur, depth) for _name, ts, dur, depth in slices] == [(2_000, 10_000, 0), (4_000, 5_000, 1)]
