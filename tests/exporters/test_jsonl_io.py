"""Tests for the JSONL capture format's reader and writer."""

import json
from pathlib import Path

import msgspec
import pytest

from gcmon.analysis.combine import combine_files
from gcmon.exporters.jsonl_io import (
    convert_jsonl_to_trace_format,
    json_to_item,
    normalize_jsonl_timestamps,
    read_jsonl,
    write_jsonl,
)
from gcmon.model.data import GCStatsInfo, LossMsg
from gcmon.model.protocol import has_incremental
from gcmon.model.trace_event import Counter, Slice
from tests.data_helpers import create_instant_msg
from tests.exporters.conftest import make_inc_item, make_inc_jsonl_record
from tests.helpers import (
    JsonlRecord,
    assert_valid_perfetto_trace,
    create_jsonl_record,
    create_mock_stats_item,
    interpreter_track,
)


class TestJsonToItem:
    def test_returns_pid_and_item(self) -> None:
        data = create_jsonl_record(pid=123, gen=0)
        pid, item = json_to_item(data)
        assert pid == 123
        assert hasattr(item, "gen")
        assert item.gen == 0

    def test_returns_incremental_item(self) -> None:
        data = make_inc_jsonl_record(pid=456, gen=1, increment_size=500)
        pid, item = json_to_item(data)
        assert pid == 456
        assert has_incremental(item)
        assert item.increment_size == 500

    def test_pid_as_string(self) -> None:
        record = create_jsonl_record(pid=789)
        data: JsonlRecord = {**record, "pid": "789"}
        pid, _ = json_to_item(data)
        assert pid == 789


class TestReadJsonl:
    def test_reads_single_record(self, tmp_path: Path) -> None:
        path = tmp_path / "test.jsonl"
        record = create_jsonl_record()
        path.write_bytes(msgspec.json.encode(record) + b"\n")

        result = read_jsonl(path)
        assert 123 in result
        assert len(result[123]) == 1
        assert hasattr(result[123][0], "gen")
        assert result[123][0].gen == 0

    def test_reads_multiple_pids(self, tmp_path: Path) -> None:
        path = tmp_path / "test.jsonl"
        lines = [
            msgspec.json.encode(create_jsonl_record(pid=1)),
            msgspec.json.encode(create_jsonl_record(pid=2)),
        ]
        path.write_bytes(b"\n".join(lines) + b"\n")

        result = read_jsonl(path)
        assert set(result.keys()) == {1, 2}

    def test_ignores_empty_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "test.jsonl"
        record = create_jsonl_record()
        path.write_bytes(msgspec.json.encode(record) + b"\n\n\n")
        result = read_jsonl(path)
        assert len(result[123]) == 1

    def test_returns_empty_dict_for_empty_file(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.jsonl"
        path.write_text("", encoding="utf-8")
        result = read_jsonl(path)
        assert result == {}

    def test_reads_incremental_record(self, tmp_path: Path) -> None:
        path = tmp_path / "inc.jsonl"
        record = make_inc_jsonl_record(pid=1)
        path.write_bytes(msgspec.json.encode(record) + b"\n")
        result = read_jsonl(path)
        assert has_incremental(result[1][0])

    def test_raises_on_malformed_json(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.jsonl"
        path.write_text("not valid json\n", encoding="utf-8")
        with pytest.raises(msgspec.DecodeError):
            read_jsonl(path)

    def test_a_capture_carrying_the_old_tid_field_still_reads(self, tmp_path: Path) -> None:
        """Captures written before `tid` left carry it on every GC and loss
        line. `from_mapping` rebuilds a record from its own fields and has
        never looked at `tid`, which is why the field could go at all; a test
        keeps that a decision rather than an accident."""
        path = tmp_path / "old.jsonl"
        gc_line = {**create_jsonl_record(pid=42, iid=1), "tid": 1}
        loss_line = {
            "pid": 42,
            "tid": -3,
            "iid": 1,
            "ts_start": 5_000,
            "ts_stop": 6_000,
            "gens": [{"gen": 0, "observed_count": 4, "lost_from": 413, "lost_count": 5, "lost_pause_ns": 8_100_000}],
        }
        path.write_bytes(msgspec.json.encode(gc_line) + b"\n" + msgspec.json.encode(loss_line) + b"\n")

        items = read_jsonl(path)

        gc_record, loss_record = items[42]
        assert isinstance(gc_record, GCStatsInfo)
        assert isinstance(loss_record, LossMsg)
        assert gc_record.iid == 1
        assert loss_record.iid == 1

    def test_an_old_capture_combines_into_the_same_trace_as_a_new_one(self, tmp_path: Path) -> None:
        """The extra field survives the whole `combine` path, not just the
        reader: a capture with `tid` and the same capture without it draw the
        same trace.

        Compared packet by packet rather than byte by byte. `combine_files`
        builds its own encoder, whose `trusted_packet_sequence_id` is `id(self)`
        masked, so two runs never agree on those bytes.
        """
        old = tmp_path / "old.jsonl"
        new = tmp_path / "new.jsonl"
        record = create_jsonl_record(pid=42, iid=1)
        old.write_bytes(msgspec.json.encode({**record, "tid": 1}) + b"\n")
        new.write_bytes(msgspec.json.encode(record) + b"\n")

        combine_files([old], tmp_path / "old.pftrace", output_format="perfetto")
        combine_files([new], tmp_path / "new.pftrace", output_format="perfetto")

        def packets(path: Path) -> list[str]:
            decoded: list[str] = []
            for packet in assert_valid_perfetto_trace(path):
                packet.ClearField("trusted_packet_sequence_id")
                decoded.append(str(packet))
            return decoded

        assert packets(tmp_path / "old.pftrace") == packets(tmp_path / "new.pftrace")

    def test_a_file_opening_with_an_array_is_reported_as_a_chrome_trace(self, tmp_path: Path) -> None:
        """The one shape a JSONL reader can name rather than choke on: a
        Chrome trace from an earlier release opens with the `[` of a JSON
        array. Any other file opening that way gets the same message, which
        is the price of naming the one an operator is actually holding."""
        path = tmp_path / "old.json"
        path.write_text('[\n{"ph": "B"}\n]\n', encoding="utf-8")

        with pytest.raises(ValueError, match="Chrome Trace"):
            read_jsonl(path)


class TestWriteJsonl:
    def test_writes_one_line_per_event(self, tmp_path: Path) -> None:
        path = tmp_path / "out.jsonl"
        item = create_mock_stats_item()
        write_jsonl(path, {12345: [item]})

        lines = path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["pid"] == 12345

    def test_writes_multiple_pids(self, tmp_path: Path) -> None:
        path = tmp_path / "out.jsonl"
        item1 = create_mock_stats_item(gen=0)
        item2 = create_mock_stats_item(gen=1)
        write_jsonl(path, {1: [item1], 2: [item2]})

        lines = path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2
        pids = {json.loads(line)["pid"] for line in lines}
        assert pids == {1, 2}

    def test_writes_incremental_fields(self, tmp_path: Path) -> None:
        path = tmp_path / "out.jsonl"
        item = make_inc_item(increment_size=500, alive_size=300)
        write_jsonl(path, {1: [item]})
        lines = path.read_text(encoding="utf-8").strip().split("\n")
        record = json.loads(lines[0])
        assert record["pid"] == 1
        assert record["increment_size"] == 500
        assert record["alive_size"] == 300

    def test_empty_items_produces_empty_file(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.jsonl"
        write_jsonl(path, {})
        content = path.read_text(encoding="utf-8")
        assert content == ""

    def test_multiple_events_per_pid(self, tmp_path: Path) -> None:
        path = tmp_path / "out.jsonl"
        item1 = create_mock_stats_item(gen=0)
        item2 = create_mock_stats_item(gen=1)
        write_jsonl(path, {1: [item1, item2]})
        lines = path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2
        assert all(json.loads(line)["pid"] == 1 for line in lines)

    def test_writes_instant_msg(self, tmp_path: Path) -> None:
        path = tmp_path / "out.jsonl"
        item = create_instant_msg(name="event", ts=1_000)
        write_jsonl(path, {1: [item]})
        lines = path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["type"] == "i"
        assert "tid" not in record


class TestNormalizeJsonlTimestamps:
    def test_normalizes_all_timestamps(self) -> None:
        item = make_inc_item(ts_start=5000, ts_stop=6000)
        items = {1: [item]}
        normalize_jsonl_timestamps(items)
        assert item.ts_start == 0
        assert item.ts_stop == 1000
        assert item.ts_mark_alive_start == 0
        assert item.ts_mark_alive_stop == 100

    def test_no_items_is_noop(self) -> None:
        normalize_jsonl_timestamps({})

    def test_non_incremental_skips_sub_steps(self) -> None:
        item = create_mock_stats_item(ts_start=5000, ts_stop=6000)
        items = {1: [item]}
        normalize_jsonl_timestamps(items)
        assert item.ts_start == 0
        assert item.ts_stop == 1000

    def test_mixed_types(self) -> None:
        non_inc = create_mock_stats_item(ts_start=5000, ts_stop=6000)
        inc = make_inc_item(ts_start=7000, ts_stop=8000)
        items = {1: [non_inc, inc]}
        normalize_jsonl_timestamps(items)
        assert non_inc.ts_start == 0
        assert inc.ts_start == 2000
        assert inc.ts_mark_alive_start == 2000
        assert inc.ts_mark_alive_stop == 2100

    def test_instant_only_normalize(self) -> None:
        item = create_instant_msg(name="event", ts=5_000)
        items = {1: [item]}
        normalize_jsonl_timestamps(items)
        assert item.ts == 0

    def test_multiple_pids(self) -> None:
        item1 = make_inc_item(ts_start=10000, ts_stop=11000)
        item2 = make_inc_item(ts_start=5000, ts_stop=6000)
        items = {1: [item1], 2: [item2]}
        normalize_jsonl_timestamps(items)
        assert item1.ts_start == 0  # per-PID min
        assert item2.ts_start == 0  # per-PID min


class TestConvertJsonlToTraceFormat:
    def test_converts_jsonl_to_trace_events(self, tmp_path: Path) -> None:
        path = tmp_path / "test.jsonl"
        record = create_jsonl_record()
        path.write_bytes(msgspec.json.encode(record) + b"\n")

        events = convert_jsonl_to_trace_format(path)
        assert len(events) > 0
        assert any(isinstance(e, Slice) for e in events)  # the pause
        assert any(isinstance(e, Counter) for e in events)  # counter

    def test_empty_file_returns_empty_list(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.jsonl"
        path.write_text("", encoding="utf-8")
        events = convert_jsonl_to_trace_format(path)
        assert events == []

    def test_incremental_record_creates_sub_events(self, tmp_path: Path) -> None:
        path = tmp_path / "inc.jsonl"
        record = make_inc_jsonl_record(pid=1, ts_start=1000, ts_stop=5000)
        path.write_bytes(msgspec.json.encode(record) + b"\n")
        events = convert_jsonl_to_trace_format(path)
        spans = [e for e in events if isinstance(e, Slice)]
        assert any("Mark Alive" in e.name for e in spans)
        assert any("Fill increment" in e.name for e in spans)
        assert any("Deduce Unreachable" in e.name for e in spans)
        assert any("Handle Weakrefs" in e.name for e in spans)
        assert any("Finalize Garbage" in e.name for e in spans)
        assert any("Handle Resurrected" in e.name for e in spans)
        assert any("Clear Weakrefs" in e.name for e in spans)
        assert any("Delete Garbage" in e.name for e in spans)

    def test_multiple_pids_each_get_their_own_tracks(self, tmp_path: Path) -> None:
        path = tmp_path / "multi.jsonl"
        lines = [
            msgspec.json.encode(create_jsonl_record(pid=1, iid=0)),
            msgspec.json.encode(create_jsonl_record(pid=2, iid=0)),
        ]
        path.write_bytes(b"\n".join(lines) + b"\n")
        events = convert_jsonl_to_trace_format(path)
        assert {e.track for e in events} == {interpreter_track(1, 0), interpreter_track(2, 0)}


class TestAnOldFormatLossRecord:
    """A capture from before the record went one-per-generation.

    That gcmon flattened three generations into `lost_gen_0`..`lost_gen_2` and
    wrote no `lost_count`, which is the field `from_mapping` now discriminates
    on. Such a line therefore falls through to the GC-record branch, and the
    danger is that it lands there: an interval gcmon was blind in would
    be read back as a collection it observed, and counted into `--stats` and
    drawn on an interpreter's own row as a `GC Pause`.

    It cannot. A GC record is built around counters a loss record never had (
    `gen`, `collections`, `heap_size`), so the conversion fails on the first of
    them. Pinned here because "it happens to be missing a required field" is a
    property of the two shapes, not a decision anything states, and a later
    default on `GCStatsInfo.gen` would turn the failure into a silent misread.
    """

    def _line(self, **kw: int) -> dict[str, int]:
        return {
            "pid": 42,
            "tid": -3,
            "iid": 1,
            "ts_start": 5_000,
            "ts_stop": 6_000,
            "lost_gen_0": kw.pop("lost_gen_0", 76),
            "lost_gen_1": 5,
            "lost_gen_2": 0,
            "lost_pause_gen_0": 8_100_000,
            "lost_pause_gen_1": 0,
            "lost_pause_gen_2": 0,
            **kw,
        }

    def _write(self, tmp_path: Path) -> Path:
        path = tmp_path / "old.jsonl"
        path.write_bytes(msgspec.json.encode(self._line()) + b"\n")
        return path

    def test_it_is_not_read_as_a_collection(self) -> None:
        with pytest.raises(msgspec.ValidationError) as excinfo:
            json_to_item(self._line())

        assert "gen" in str(excinfo.value)

    def test_reading_the_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(msgspec.ValidationError):
            read_jsonl(self._write(tmp_path))

    def test_combine_refuses_the_capture(self, tmp_path: Path) -> None:
        """The path an operator actually takes. Nothing here catches the error,
        so `combine` stops rather than writing a trace with a phantom pause on
        it."""
        with pytest.raises(msgspec.ValidationError):
            combine_files(
                [self._write(tmp_path)],
                tmp_path / "out.pftrace",
                output_format="perfetto",
            )

    def test_a_per_generation_record_is_refused_the_same_way(self) -> None:
        """The shape between the two: one record per generation, its counts at
        the top level and no ``gens``. It carries a ``gen`` where the flattened
        one had none, so it gets further into ``GCStatsInfo`` before failing,
        but fail it must, and for the same reason. Read as a collection it
        would put a pause on an interpreter's own row for an interval nothing
        was observed in."""
        line = {
            "pid": 42,
            "tid": -3,
            "iid": 1,
            "gen": 1,
            "ts_start": 5_000,
            "ts_stop": 6_000,
            "lost_from": 413,
            "lost_count": 5,
            "lost_pause_ns": 8_100_000,
        }

        with pytest.raises(msgspec.ValidationError) as excinfo:
            json_to_item(line)

        assert "heap_size" in str(excinfo.value)
