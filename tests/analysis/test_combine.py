"""Tests for `gcmon combine`: what it reads, how it normalises, what it writes."""

import json
from pathlib import Path

import msgspec
import pytest
from perfetto.protos.perfetto.trace.perfetto_trace_pb2 import TrackEvent

from gcmon.analysis.combine import _normalize_trace_timestamps, _starts_at, combine_files
from gcmon.analysis.jsonl_io import (
    convert_jsonl_to_trace_format,
    normalize_jsonl_timestamps,
    read_jsonl,
    write_jsonl,
)
from gcmon.model.data import LossMsg
from gcmon.model.trace_event import (
    Counter,
    EventArgs,
    Instant,
    Slice,
    TraceEvent,
)
from tests.data_helpers import create_instant_msg
from tests.exporters.conftest import make_inc_jsonl_record
from tests.helpers import (
    assert_valid_perfetto_trace,
    create_jsonl_record,
    create_mock_loss_item,
    create_mock_stats_item,
    interpreter_track,
    loss_track,
    process_track,
)


def gc_slices(path: Path) -> list[tuple[str, int, int]]:
    """`(name, ts_start, ts_stop)` per `GC ` slice in a combined trace.

    An end event carries no name of its own, so the slices are paired on a
    stack per track, the way a trace processor pairs them.
    """
    open_slices: dict[int, list[tuple[str, int]]] = {}
    drawn: list[tuple[str, int, int]] = []
    for packet in assert_valid_perfetto_trace(path):
        if not packet.HasField("track_event"):
            continue
        event = packet.track_event
        if event.type == TrackEvent.Type.TYPE_SLICE_BEGIN:
            open_slices.setdefault(event.track_uuid, []).append((event.name, packet.timestamp))
        elif event.type == TrackEvent.Type.TYPE_SLICE_END:
            name, ts_start = open_slices[event.track_uuid].pop()
            if name.startswith("GC "):
                drawn.append((name, ts_start, packet.timestamp))
    return sorted(drawn, key=lambda slice_: slice_[1])


def instant_events(path: Path) -> list[tuple[str, int]]:
    """`(name, ts)` per zero-duration marker in a combined trace."""
    return [
        (packet.track_event.name, packet.timestamp)
        for packet in assert_valid_perfetto_trace(path)
        if packet.HasField("track_event") and packet.track_event.type == TrackEvent.Type.TYPE_INSTANT
    ]


class TestNormalizeTraceTimestamps:
    def test_normalizes_to_zero(self) -> None:
        args: EventArgs = {
            "generation": 0,
            "iid": 1,
            "collections": 1,
            "heap_size": 100,
            "collected": 10,
            "uncollectable": 0,
            "candidates": 5,
        }
        e1 = Slice(interpreter_track(1, 1), name="e1", cat="c", ts_start=5_000_000, ts_stop=5_001_000, args=args)
        e2 = Counter(
            interpreter_track(1, 1),
            metric="collected",
            display_name="c1 collected",
            ts=3_000_000,
            value=10,
        )
        events: list[TraceEvent] = [e1, e2]
        _normalize_trace_timestamps(events)
        assert e1.ts_start == 2_000_000  # 5_000_000 - 3_000_000
        assert e2.ts == 0  # 3_000_000 - 3_000_000

    def test_single_event_ts_becomes_zero(self) -> None:
        args: EventArgs = {
            "generation": 0,
            "iid": 1,
            "collections": 1,
            "heap_size": 100,
            "collected": 10,
            "uncollectable": 0,
            "candidates": 5,
        }
        e1 = Slice(interpreter_track(1, 1), name="e1", cat="c", ts_start=1_000_000, ts_stop=1_001_000, args=args)
        events: list[TraceEvent] = [e1]
        _normalize_trace_timestamps(events)
        assert e1.ts_start == 0

    def test_empty_events_is_noop(self) -> None:
        events: list[TraceEvent] = []
        _normalize_trace_timestamps(events)
        assert events == []

    def test_per_pid_normalization(self) -> None:
        args: EventArgs = {
            "generation": 0,
            "iid": 1,
            "collections": 1,
            "heap_size": 100,
            "collected": 10,
            "uncollectable": 0,
            "candidates": 5,
        }
        e1 = Slice(interpreter_track(1, 1), name="e1", cat="c", ts_start=10_000_000, ts_stop=10_001_000, args=args)
        e2 = Slice(interpreter_track(1, 1), name="e2", cat="c", ts_start=12_000_000, ts_stop=12_001_000, args=args)
        e3 = Slice(interpreter_track(2, 1), name="e3", cat="c", ts_start=5_000_000, ts_stop=5_001_000, args=args)
        e4 = Slice(interpreter_track(2, 1), name="e4", cat="c", ts_start=7_000_000, ts_stop=7_001_000, args=args)
        events: list[TraceEvent] = [e1, e2, e3, e4]
        _normalize_trace_timestamps(events)
        assert e1.ts_start == 0  # pid=1: 10_000_000 - 10_000_000
        assert e2.ts_start == 2_000_000  # pid=1: 12_000_000 - 10_000_000
        assert e3.ts_start == 0  # pid=2: 5_000_000 - 5_000_000
        assert e4.ts_start == 2_000_000  # pid=2: 7_000_000 - 5_000_000

    def test_every_kind_of_event_is_shifted(self) -> None:
        """Every timestamp on every member of the union moves.

        The normalizer used to select events by `ph`, and a kind added later
        would have been left behind holding raw ones. A `Slice` is the case
        that can still go half-done: it carries two absolute timestamps, and
        shifting only the one it starts at would stretch every span back to
        the old origin without failing anything else.
        """
        pause = Slice(interpreter_track(1, 0), name="GC Pause(0)", cat="gc", ts_start=8_000, ts_stop=9_000, args={})
        counter = Counter(interpreter_track(1, 0), metric="collected", display_name="G0 collected", ts=8_000, value=1)
        rss = Counter(process_track(1), metric="rss", display_name="rss", ts=6_000, value=4096)
        mark = Instant(process_track(1), name="benchmark", ts=5_000)
        loss = Slice(loss_track(1, 0), name="GC Loss(0)", cat="gc.loss", ts_start=5_000, ts_stop=7_000, args={})
        events: list[TraceEvent] = [pause, counter, rss, mark, loss]

        _normalize_trace_timestamps(events)

        assert [_starts_at(e) for e in events] == [3_000, 3_000, 1_000, 0, 0]
        assert (pause.ts_stop, loss.ts_stop) == (4_000, 2_000), (
            "a `Slice` carries an absolute end, so both of its timestamps have to move"
        )

    def test_negative_timestamps(self) -> None:
        args: EventArgs = {
            "generation": 0,
            "iid": 1,
            "collections": 1,
            "heap_size": 100,
            "collected": 10,
            "uncollectable": 0,
            "candidates": 5,
        }
        e1 = Slice(interpreter_track(1, 1), name="e1", cat="c", ts_start=-100, ts_stop=900, args=args)
        e2 = Slice(interpreter_track(1, 1), name="e2", cat="c", ts_start=-500, ts_stop=500, args=args)
        events: list[TraceEvent] = [e1, e2]
        _normalize_trace_timestamps(events)
        assert e1.ts_start == 400  # -100 - (-500)
        assert e2.ts_start == 0


class TestCombineFiles:
    def test_jsonl_to_jsonl(self, tmp_path: Path) -> None:
        f1 = tmp_path / "a.jsonl"
        out = tmp_path / "out.jsonl"
        r = create_jsonl_record(pid=1)
        f1.write_bytes(msgspec.json.encode(r) + b"\n")
        combine_files([f1], out, output_format="jsonl")
        lines = out.read_text(encoding="utf-8").strip().split("\n")
        assert json.loads(lines[0])["pid"] == 1

    def test_an_unknown_output_format_raises(self, tmp_path: Path) -> None:
        """`cmd_combine` never reaches this: argparse `choices` refuses the word
        first. A caller of `combine_files` does not go through argparse."""
        with pytest.raises(ValueError, match="Unsupported output format"):
            combine_files([], tmp_path / "out.bin", output_format="protobuf")

    def test_jsonl_to_jsonl_normalize(self, tmp_path: Path) -> None:
        f1 = tmp_path / "a.jsonl"
        out = tmp_path / "out.jsonl"
        r1 = create_jsonl_record(pid=1, ts_start=10_000_000, ts_stop=11_000_000)
        r2 = create_jsonl_record(pid=1, ts_start=5_000_000, ts_stop=6_000_000)
        lines = [
            msgspec.json.encode(r1),
            msgspec.json.encode(r2),
        ]
        f1.write_bytes(b"\n".join(lines) + b"\n")
        combine_files([f1], out, normalize=True, output_format="jsonl")
        records = [json.loads(line) for line in out.read_text(encoding="utf-8").strip().split("\n") if line]
        assert records[0]["ts_start"] == 5_000_000
        assert records[1]["ts_start"] == 0

    def test_jsonl_to_jsonl_merges_same_pid(self, tmp_path: Path) -> None:
        f1 = tmp_path / "a.jsonl"
        f2 = tmp_path / "b.jsonl"
        out = tmp_path / "out.jsonl"
        for f, pid in [(f1, 1), (f2, 2)]:
            f.write_bytes(
                msgspec.json.encode(create_jsonl_record(pid=pid)) + b"\n",
            )
        combine_files([f1, f2], out, output_format="jsonl")
        records = [json.loads(line) for line in out.read_text(encoding="utf-8").strip().split("\n") if line]
        assert len(records) == 2
        assert {r["pid"] for r in records} == {1, 2}

    def test_jsonl_to_jsonl_append_same_pid(self, tmp_path: Path) -> None:
        f1 = tmp_path / "a.jsonl"
        f2 = tmp_path / "b.jsonl"
        out = tmp_path / "out.jsonl"
        r1 = create_jsonl_record(pid=1, ts_start=1000, ts_stop=2000)
        r2 = create_jsonl_record(pid=1, ts_start=3000, ts_stop=4000)
        for f, lines in [(f1, [r1]), (f2, [r2])]:
            f.write_bytes(
                b"\n".join(msgspec.json.encode(r) for r in lines) + b"\n",
            )
        combine_files([f1, f2], out, output_format="jsonl")
        records = [json.loads(line) for line in out.read_text(encoding="utf-8").strip().split("\n") if line]
        assert len(records) == 2
        assert records[0]["ts_start"] == 1000
        assert records[1]["ts_start"] == 3000

    def test_jsonl_to_jsonl_with_incremental(self, tmp_path: Path) -> None:
        f1 = tmp_path / "a.jsonl"
        out = tmp_path / "out.jsonl"
        record = make_inc_jsonl_record(pid=1, ts_start=1000, ts_stop=5000)
        f1.write_bytes(msgspec.json.encode(record) + b"\n")
        combine_files([f1], out, output_format="jsonl")
        records = [json.loads(line) for line in out.read_text(encoding="utf-8").strip().split("\n") if line]
        assert records[0]["increment_size"] == 500
        assert records[0]["alive_size"] == 300

    def test_an_instant_record_reaches_the_trace(self, tmp_path: Path) -> None:
        """A monitored run writes one of these at startup, so a capture
        converted a month later has to still carry it. The third record kind
        `combine` handles, beside GC records and loss windows."""
        source = tmp_path / "in.jsonl"
        out = tmp_path / "out.pftrace"
        write_jsonl(
            source,
            {42: [create_instant_msg(name="GC monitor started", ts=1_400_000_000), create_mock_stats_item(iid=0)]},
        )

        combine_files([source], out, output_format="perfetto")

        assert ("GC monitor started", 1_400_000_000) in instant_events(out)


class TestJsonlLossRoundTrip:
    """`combine` reads and writes JSONL, so a loss span has to survive the
    round trip as well as a GC record does; otherwise a converted capture
    silently loses the spans the live run drew."""

    def _msg(self, **kw: int) -> LossMsg:
        kw.setdefault("iid", 1)
        kw.setdefault("ts_start", 5_000)
        kw.setdefault("ts_stop", 6_000)
        return create_mock_loss_item(**kw)

    def test_the_line_carries_every_field(self, tmp_path: Path) -> None:
        """Read off the wire rather than through `read_jsonl`, which would be
        equally happy with a field gcmon writes and reads under one wrong name.
        Every value is distinct, so a pair swapped in transit shows up."""
        path = tmp_path / "loss.jsonl"

        write_jsonl(
            path, {42: [self._msg(gen=1, observed_count=4, lost_from=413, lost_count=5, lost_pause_ns=8_100_000)]}
        )

        assert json.loads(path.read_text(encoding="utf-8")) == {
            "pid": 42,
            "iid": 1,
            "ts_start": 5_000,
            "ts_stop": 6_000,
            "gens": [
                {
                    "gen": 1,
                    "observed_count": 4,
                    "lost_from": 413,
                    "lost_count": 5,
                    "lost_pause_ns": 8_100_000,
                }
            ],
        }

    def test_write_then_read(self, tmp_path: Path) -> None:
        path = tmp_path / "loss.jsonl"
        msg = self._msg(gen=1, lost_from=413, lost_count=5, lost_pause_ns=8_100_000)

        write_jsonl(path, {42: [msg]})

        assert read_jsonl(path) == {42: [msg]}

    def test_the_range_survives_into_the_drawing(self, tmp_path: Path) -> None:
        """`lost_from` earns its place on the wire by naming collections in the
        trace. A record that came back with it defaulted still draws a span, so
        the args are where the loss of the field would be visible."""
        path = tmp_path / "loss.jsonl"
        write_jsonl(path, {42: [self._msg(lost_from=413, lost_count=19)]})

        args = [e.args for e in convert_jsonl_to_trace_format(path) if isinstance(e, Slice)]

        assert [a["gen0"]["lost_collections"] for a in args] == ["413..431"]  # type: ignore[index]

    def test_written_naming_the_interpreter_that_lost_the_records(self, tmp_path: Path) -> None:
        path = tmp_path / "loss.jsonl"

        write_jsonl(path, {42: [self._msg(lost_count=76)]})

        record = json.loads(path.read_text(encoding="utf-8"))
        assert record["iid"] == 1
        assert "tid" not in record

    def test_normalize_shifts_a_loss_span(self) -> None:
        """It is neither a GC record nor an instant, so without a branch of its
        own it would keep raw timestamps while everything around it moved."""
        msg = self._msg(ts_start=7_000, ts_stop=8_000, lost_count=76)
        item = create_mock_stats_item(ts_start=5_000, ts_stop=6_000)

        normalize_jsonl_timestamps({1: [item, msg]})

        assert (msg.ts_start, msg.ts_stop) == (2_000, 3_000)
        assert item.ts_start == 0

    def test_a_loss_span_can_set_the_origin(self) -> None:
        """A window opens at the record before the gap and closes at the one
        after it, so a capture whose first poll already lost records starts on
        a loss span. If the origin were taken from GC records alone every
        timestamp in the combined trace would be off by the difference, and
        the span itself would go negative."""
        msg = self._msg(ts_start=3_000, ts_stop=5_000, lost_count=76)
        item = create_mock_stats_item(ts_start=5_000, ts_stop=6_000)

        normalize_jsonl_timestamps({1: [msg, item]})

        assert (msg.ts_start, msg.ts_stop) == (0, 2_000)
        assert (item.ts_start, item.ts_stop) == (2_000, 3_000)

    def test_combine_normalizes_from_a_loss_span(self, tmp_path: Path) -> None:
        """The same claim down the path an operator takes, and read back off
        the trace rather than off the structs `combine` mutated in place.

        A loss span is the earliest thing in this capture, so a normalization
        that only looked at GC records would leave it at a negative timestamp
        and zero the pause instead.
        """
        source = tmp_path / "in.jsonl"
        out = tmp_path / "out.pftrace"
        msg = self._msg(ts_start=3_000_000, ts_stop=5_000_000, lost_count=76)
        write_jsonl(source, {42: [msg, create_mock_stats_item(iid=1, ts_start=5_000_000, ts_stop=6_000_000)]})

        combine_files([source], out, output_format="perfetto", normalize=True)

        assert gc_slices(out) == [
            ("GC Loss(0)", 0, 2_000_000),
            ("GC Pause(0)", 2_000_000, 3_000_000),
        ]

    def test_combine_jsonl_to_jsonl_keeps_the_span(self, tmp_path: Path) -> None:
        source = tmp_path / "in.jsonl"
        out = tmp_path / "out.jsonl"
        msg = self._msg(lost_count=76)
        write_jsonl(source, {42: [msg]})

        combine_files([source], out, output_format="jsonl")

        assert read_jsonl(out) == {42: [msg]}
