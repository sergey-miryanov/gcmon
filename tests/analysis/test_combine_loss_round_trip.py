"""Does `gcmon combine` redraw the loss row the live run drew?

A live run never hands a span's shape to anyone. It emits one record per poll
and the row reads as a sequence because consecutive intervals meet at a poll
instant. `combine` rebuilds the row from records instead, through JSONL, and
the trip is not free: one span's END shares its timestamp with the next one's
BEGIN, and a trace processor sorting by timestamp leaves those two in the order
they were emitted. Emitted the wrong way round they read as nested. Field-by-
field round-trip assertions cannot reach that claim: every one of them passes
on a file whose lines were shuffled.

So the check walks the combined **output**, and it walks it with the reader
that decides. The trace is loaded into the real trace processor and its `depth`
column is read back: a span opened inside another shows up there as depth 1,
the one place ADR-0015 says the mistake is visible at all, since the processor
itself reports ``misplaced_end_event = 0`` and reads a crossing as a nesting
rather than rejecting it.

The live side comes from `loss_row`, which polls a real monitor on a fixed
clock and resolves the row the same way. Two walks, one capture: the point is
that the paths agree, which is worth nothing if they were given different data.
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from perfetto.trace_processor import TraceProcessor

from gcmon.analysis.combine import combine_files
from gcmon.exporters.jsonl_io import read_jsonl, write_jsonl
from gcmon.model.protocol import TItem, is_loss
from tests.exporters.loss_row import (
    IID,
    PID,
    POLL_TIMES,
    SliceRow,
    ingest,
    loss_slices,
    three_generations,
)
from tests.helpers import create_mock_loss_item, loss_track, open_trace_processor

LOSS_ROW = loss_track(PID, IID)

LIVE_ROW: list[SliceRow] = [
    ("GC Loss(0,1,2)", 1_000_000, 10_000_000, 0),
    ("GC Loss(0,1,2)", 10_000_000, 20_000_000, 0),
]
"""The row `three_generations` draws, in the nanoseconds a trace carries.

Spelled out rather than derived so that a combined trace is measured against a
fixed shape and not only against whatever the live path happened to produce.
"""


def _capture(tmp_path: Path, name: str = "capture.jsonl") -> Path:
    """One lossy run, written to JSONL the way `--format jsonl` writes it."""
    path = tmp_path / name
    write_jsonl(path, {PID: ingest(*three_generations())})
    return path


@contextmanager
def _combined(tmp_path: Path, source: Path, name: str = "combined") -> Iterator[TraceProcessor]:
    """Combine *source* to Perfetto and load the result into the processor."""
    out = tmp_path / f"{name}.pftrace"
    combine_files([source], out, output_format="perfetto")

    with open_trace_processor(out) as tp:
        yield tp


def _loss_row(tp: TraceProcessor) -> list[SliceRow]:
    """Every slice on the loss track, as `(name, ts_start, ts_stop, depth)`.

    A span opened while another is still open lands at depth 1 here, and
    renders as a nested slice in the UI.
    """
    return [
        (row.name, row.ts, row.ts + row.dur, row.depth)
        for row in tp.query(
            "SELECT s.name, s.ts, s.dur, s.depth FROM slice s JOIN track t ON s.track_id = t.id "
            "WHERE t.name LIKE 'GC Loss%' ORDER BY s.ts, s.depth"
        )
    ]


def _misplaced_ends(tp: TraceProcessor) -> int:
    """Zero even on a crossing row, which is why `_loss_row` reads depth."""
    rows = list(tp.query("SELECT value FROM stats WHERE name = 'misplaced_end_event'"))
    return int(rows[0].value) if rows else 0


def _loss_args(tp: TraceProcessor) -> list[dict[str, Any]]:
    """The annotations of each loss slice, in timestamp order.

    The processor flattens a `dict_entries` group by joining the names with a
    dot, so a per-generation group reaches SQL as `gen1.lost_count`. A group
    that arrived as a plain value instead would leave these keys missing rather
    than wrong.
    """
    by_ts: dict[int, dict[str, Any]] = {}
    for row in tp.query(
        "SELECT s.ts, a.flat_key, a.string_value, a.int_value "
        "FROM slice s JOIN track t ON s.track_id = t.id JOIN args a ON a.arg_set_id = s.arg_set_id "
        "WHERE t.name LIKE 'GC Loss%' ORDER BY s.ts, a.flat_key"
    ):
        if not row.flat_key.startswith("debug."):
            continue
        # An args row fills one value column and leaves the other NULL,
        # which the stub's non-optional types do not describe.
        text: Any = row.string_value
        value = text if text is not None else row.int_value
        by_ts.setdefault(row.ts, {})[row.flat_key.removeprefix("debug.")] = value
    return [by_ts[ts] for ts in sorted(by_ts)]


class TestTheCombinedRowIsTheLiveRow:
    """The drawing, rebuilt offline, against the drawing made live."""

    def test_the_row_comes_back_flat(self, tmp_path: Path) -> None:
        """The claim JSONL could drop. Depth is the processor's, so a
        file whose spans were emitted out of order nests here, and would have
        passed a check that only counted two loss slices."""
        with _combined(tmp_path, _capture(tmp_path)) as tp:
            assert _misplaced_ends(tp) == 0
            assert [(name, depth) for name, _s, _e, depth in _loss_row(tp)] == [
                ("GC Loss(0,1,2)", 0),
                ("GC Loss(0,1,2)", 0),
            ]

    def test_every_span_keeps_its_interval(self, tmp_path: Path) -> None:
        with _combined(tmp_path, _capture(tmp_path)) as tp:
            assert _loss_row(tp) == LIVE_ROW

    def test_consecutive_spans_still_meet(self, tmp_path: Path) -> None:
        """The intervals tile: the second span opens where the first closes
        and nowhere else."""
        with _combined(tmp_path, _capture(tmp_path)) as tp:
            first, second = _loss_row(tp)

        assert first[2] == second[1]

    def test_the_two_paths_agree(self, tmp_path: Path) -> None:
        """Live and offline, one capture, resolved by two independent walks:
        one over the converter's objects, one over the trace on disk."""
        live = loss_slices(ingest(*three_generations()))[LOSS_ROW]

        with _combined(tmp_path, _capture(tmp_path)) as tp:
            assert _loss_row(tp) == live

    def test_the_counts_ride_on_the_generation_that_lost_them(self, tmp_path: Path) -> None:
        """A span redrawn over the right interval with another generation's
        counters says the wrong thing just as loudly."""
        with _combined(tmp_path, _capture(tmp_path)) as tp:
            args = _loss_args(tp)

        assert [
            {gen: (a[f"gen{gen}.lost_count"], a[f"gen{gen}.lost_collections"]) for gen in (0, 1, 2)} for a in args
        ] == [
            {0: (2, "11..12"), 1: (2, "21..22"), 2: (2, "31..32")},
            {0: (2, "14..15"), 1: (2, "24..25"), 2: (2, "34..35")},
        ]

    def test_the_totals_survive_the_trip(self, tmp_path: Path) -> None:
        with _combined(tmp_path, _capture(tmp_path)) as tp:
            args = _loss_args(tp)

        assert [(a["observed_count"], a["lost_count"], a["lost_pause_ns"]) for a in args] == [
            (3, 6, 600_000),
            (3, 6, 600_000),
        ]


class TestTheFileCarriesTheIntervals:
    """Why the row survives at all: JSONL is a sequence of records, one per
    poll, and each carries its own two edges. Pinned separately from the
    drawing so that a regression says which half broke."""

    def test_one_line_per_poll_interval(self, tmp_path: Path) -> None:
        lines = [json.loads(line) for line in _capture(tmp_path).read_text(encoding="utf-8").splitlines() if line]

        assert [(r["ts_start"], r["ts_stop"]) for r in lines if "gens" in r] == [
            (POLL_TIMES[0], POLL_TIMES[1]),
            (POLL_TIMES[1], POLL_TIMES[2]),
        ]

    def test_each_line_names_every_generation_in_its_interval(self, tmp_path: Path) -> None:
        lines = [json.loads(line) for line in _capture(tmp_path).read_text(encoding="utf-8").splitlines() if line]

        assert [[entry["gen"] for entry in r["gens"]] for r in lines if "gens" in r] == [[0, 1, 2], [0, 1, 2]]

    def test_a_jsonl_to_jsonl_pass_does_not_reshuffle_them(self, tmp_path: Path) -> None:
        """`combine` can also write JSONL, and a capture that went through it
        has to still draw the same row when it is converted later."""
        source = _capture(tmp_path)
        out = tmp_path / "combined.jsonl"

        combine_files([source], out, output_format="jsonl")

        assert [item for item in read_jsonl(out)[PID] if is_loss(item)] == [
            item for item in read_jsonl(source)[PID] if is_loss(item)
        ]


class TestTheWalksCanFail:
    """Negative controls for the two resolvers above.

    Every assertion in this file reads a row through one of them, so a walk
    that could not tell a crossing row from a flat one would pass in a world
    where the shape did not matter. Both are fed a pair of spans that genuinely
    overlap, which is a shape one span per poll cannot produce.
    """

    def crossing(self) -> list[TItem]:
        return [
            create_mock_loss_item(iid=IID, ts_start=2_000_000, ts_stop=5_000_000),
            create_mock_loss_item(iid=IID, ts_start=4_000_000, ts_stop=9_000_000),
        ]

    def test_the_object_walk_rejects_a_crossing_row(self) -> None:
        with pytest.raises(AssertionError, match="opened inside"):
            loss_slices(self.crossing())

    def test_the_processor_reads_a_crossing_row_as_nested(self, tmp_path: Path) -> None:
        """What the processor does with a crossing row, and why depth is the
        instrument: it accepts the file, reports no misplaced end, and draws
        the second span inside the first."""
        source = tmp_path / "crossing.jsonl"
        write_jsonl(source, {PID: self.crossing()})

        with _combined(tmp_path, source, "crossing") as tp:
            assert _misplaced_ends(tp) == 0
            assert [depth for _name, _s, _e, depth in _loss_row(tp)] == [0, 1]

    def test_the_live_row_is_flat_to_begin_with(self) -> None:
        """The baseline the combined row is compared against. If the converter
        drew a nested row live, `test_the_two_paths_agree` would pass on two
        wrong rows."""
        row = loss_slices(ingest(*three_generations()))[LOSS_ROW]

        assert [depth for _name, _s, _e, depth in row] == [0, 0]

    def test_the_walks_are_reading_something(self) -> None:
        """Both resolvers return an empty row for a track that carries no
        slices, so an empty walk is indistinguishable from a clean one."""
        row = loss_slices(ingest(*three_generations()))[LOSS_ROW]

        assert len(row) == 2


def _shuffled_capture(tmp_path: Path) -> Path:
    """The same records, written newest first."""
    path = tmp_path / "shuffled.jsonl"
    losses = [item for item in ingest(*three_generations()) if is_loss(item)]
    write_jsonl(path, {PID: list(reversed(losses))})
    return path


def test_a_shuffled_file_still_draws_a_flat_row(tmp_path: Path) -> None:
    """Line order is not part of the record. The converter sorts what it reads
    into time order before it emits, so a capture whose lines were rewritten
    draws the same row as the one gcmon wrote.

    Without that sort the later span's BEGIN would go out before the earlier
    one's END, and since the two share a timestamp the processor would read the
    pair as nested rather than as neighbours.
    """
    with _combined(tmp_path, _shuffled_capture(tmp_path), "shuffled") as tp:
        assert _misplaced_ends(tp) == 0
        assert _loss_row(tp) == LIVE_ROW


def test_the_converter_emits_a_shuffled_capture_in_time_order(tmp_path: Path) -> None:
    """The same claim one level down, where it is cheap.

    `loss_slices` walks the converter's output in emission order, so it fails
    on a row emitted out of order as well as on one that overlaps. The test
    above is what decides -- it asks the trace processor -- but it costs
    seconds, and a sort that stopped working should not depend on a single
    trace load to be noticed.
    """
    assert loss_slices(read_jsonl(_shuffled_capture(tmp_path))[PID])[LOSS_ROW] == LIVE_ROW
