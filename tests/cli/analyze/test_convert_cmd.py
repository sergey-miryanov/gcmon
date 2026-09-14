"""Tests for `gcmon combine`: the CLI, its arguments, and what it writes.

JSONL is the only input. The output is a Perfetto trace or a JSONL capture,
and the two differ in where they normalize; ADR-0021 records why.
"""

import json
import subprocess
import sys
from argparse import Namespace
from pathlib import Path
from typing import Protocol

import pytest
from perfetto.protos.perfetto.trace.perfetto_trace_pb2 import TracePacket, TrackEvent

from tests.helpers import JsonlRecord, create_jsonl_record, perfetto_packets


class Combiner(Protocol):
    def __call__(
        self,
        inputs: list[Path],
        output: Path | None = None,
        extra_args: list[str] | None = None,
    ) -> subprocess.CompletedProcess[str]: ...


class RawFileFactory(Protocol):
    def __call__(self, name: str, content: str) -> Path: ...


class JsonlFileFactory(Protocol):
    def __call__(self, name: str, records: list[dict[str, int | float]]) -> Path: ...


# =============================================================================
# Local fixtures
# =============================================================================


@pytest.fixture
def make_raw_file(tmp_path: Path) -> RawFileFactory:
    """Create a file with raw (non-JSON) text content for invalid-JSON tests."""

    def _make(name: str, content: str) -> Path:
        path = tmp_path / name
        path.write_text(content, encoding="utf-8")
        return path

    return _make


@pytest.fixture
def make_jsonl_file(tmp_path: Path) -> JsonlFileFactory:
    """Create a JSONL file with given raw GC stats records."""

    def _make(name: str, records: list[dict[str, int | float]]) -> Path:
        path = tmp_path / name
        with open(path, "w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec) + "\n")
        return path

    return _make


@pytest.fixture
def run_combine() -> Combiner:
    def _run(
        inputs: list[Path], output: Path | None = None, extra_args: list[str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        cmd = [sys.executable, "-m", "gcmon", "combine"]
        cmd.extend(str(f) for f in inputs)
        if output:
            cmd += ["-o", str(output)]
        if extra_args:
            cmd.extend(extra_args)
        return subprocess.run(cmd, capture_output=True, text=True)

    return _run


@pytest.fixture
def combine_output(tmp_path: Path) -> Path:
    """Standard combined output path."""
    return tmp_path / "combined.pftrace"


# =============================================================================
# Perfetto structural helpers
# =============================================================================


def assert_valid_perfetto_format(path: Path) -> list[TracePacket]:
    assert path.exists(), f"File {path} does not exist"
    file_bytes = path.read_bytes()
    assert len(file_bytes) > 0, f"File {path} is empty"

    packets = perfetto_packets(file_bytes)
    assert len(packets) > 0, f"no Trace.PACKET fields in {path}"

    for pkt in packets:
        assert pkt.SerializeToString(), f"empty TracePacket: {pkt!r}"
    return packets


def assert_perfetto_has_track_descriptor_and_event(path: Path) -> None:
    file_bytes = path.read_bytes()
    has_descriptor = False
    has_track_event = False
    for pkt in perfetto_packets(file_bytes):
        if pkt.HasField("track_descriptor"):
            has_descriptor = True
        if pkt.HasField("track_event"):
            has_track_event = True
    assert has_descriptor, f"no TrackDescriptor (field 60) in {path}"
    assert has_track_event, f"no TrackEvent (field 11) in {path}"


def pause_timestamps(path: Path) -> list[int]:
    """The instant each `GC Pause` slice opens, in packet order.

    Packet order is emission order, which is input-file order, so a per-file
    normalization shows up here as a run of timestamps restarting at zero.
    """
    return [
        pkt.timestamp
        for pkt in perfetto_packets(path.read_bytes())
        if pkt.HasField("track_event")
        and pkt.track_event.type == TrackEvent.Type.TYPE_SLICE_BEGIN
        and pkt.track_event.name.startswith("GC Pause")
    ]


def assert_valid_jsonl(path: Path) -> list[JsonlRecord]:
    """Validate that a file contains valid JSONL (one JSON object per line)."""
    assert path.exists(), f"File {path} does not exist"
    records: list[JsonlRecord] = []
    with open(path, encoding="utf-8") as f:
        for idx, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            data = json.loads(line)
            assert isinstance(data, dict), f"Line {idx} is not a JSON object"
            records.append(data)
    return records


# =============================================================================
# Unit Tests for cmd_combine
# =============================================================================


def _args(inputs: list[Path], output: Path, **overrides: object) -> Namespace:
    defaults: dict[str, object] = {
        "inputs": inputs,
        "output": output,
        "verbose": 1,
        "normalize": False,
        "output_format": "perfetto",
    }
    return Namespace(**{**defaults, **overrides})


def test_cmd_combine_basic(make_jsonl_file: JsonlFileFactory, tmp_path: Path) -> None:
    from gcmon.cli.analyze import convert_cmd

    input_file = make_jsonl_file("input.jsonl", [create_jsonl_record()])
    output = tmp_path / "output.pftrace"

    assert convert_cmd.cmd_combine(_args([input_file], output)) == 0
    assert output.exists()
    assert output.read_bytes()


@pytest.mark.parametrize("output_format, output_name", [("perfetto", "output.pftrace"), ("jsonl", "output.jsonl")])
def test_cmd_combine_file_not_found(
    caplog: pytest.LogCaptureFixture, tmp_path: Path, output_format: str, output_name: str
) -> None:
    from gcmon.cli.analyze import convert_cmd

    args = _args([tmp_path / "nonexistent.jsonl"], tmp_path / output_name, output_format=output_format)

    assert convert_cmd.cmd_combine(args) == 1
    assert "Error combining files" in caplog.text


@pytest.mark.parametrize("output_format, output_name", [("perfetto", "output.pftrace"), ("jsonl", "output.jsonl")])
def test_cmd_combine_invalid_json(
    caplog: pytest.LogCaptureFixture,
    make_raw_file: RawFileFactory,
    tmp_path: Path,
    output_format: str,
    output_name: str,
) -> None:
    from gcmon.cli.analyze import convert_cmd

    input_file = make_raw_file("invalid.jsonl", "not valid json")
    args = _args([input_file], tmp_path / output_name, output_format=output_format)

    assert convert_cmd.cmd_combine(args) == 1
    assert "Error combining files" in caplog.text


# =============================================================================
# Subprocess Tests - Combine Command
# =============================================================================


class TestCliCombine:
    def test_basic(
        self,
        make_jsonl_file: JsonlFileFactory,
        run_combine: Combiner,
        combine_output: Path,
    ) -> None:
        f1 = make_jsonl_file("data.jsonl", [create_jsonl_record()])

        result = run_combine([f1], output=combine_output)

        assert result.returncode == 0, result.stderr
        assert_valid_perfetto_format(combine_output)
        assert_perfetto_has_track_descriptor_and_event(combine_output)

    def test_verbose_output(
        self,
        make_jsonl_file: JsonlFileFactory,
        run_combine: Combiner,
        combine_output: Path,
    ) -> None:
        f1 = make_jsonl_file("data.jsonl", [create_jsonl_record()])

        result = run_combine([f1], output=combine_output, extra_args=["-v"])

        assert result.returncode == 0
        assert "Output format: perfetto" in result.stderr
        assert "Combine complete" in result.stderr

    def test_missing_file(self, run_combine: Combiner, combine_output: Path) -> None:
        result = run_combine([Path("nonexistent.jsonl")], output=combine_output)

        assert result.returncode != 0
        assert "Error combining files" in result.stderr

    def test_invalid_json(
        self,
        make_raw_file: RawFileFactory,
        run_combine: Combiner,
        combine_output: Path,
    ) -> None:
        f = make_raw_file("invalid.jsonl", "not valid json{{{")

        result = run_combine([f], output=combine_output)

        assert result.returncode != 0, result.stderr
        assert "Error combining files" in result.stderr
        assert "json" in result.stderr.lower()

    def test_multiple_files(
        self,
        make_jsonl_file: JsonlFileFactory,
        run_combine: Combiner,
        combine_output: Path,
    ) -> None:
        f1 = make_jsonl_file("data1.jsonl", [create_jsonl_record(pid=123, gen=0)])
        f2 = make_jsonl_file("data2.jsonl", [create_jsonl_record(pid=456, gen=1)])

        result = run_combine([f1, f2], output=combine_output)

        assert result.returncode == 0, result.stderr
        packets = assert_valid_perfetto_format(combine_output)
        descriptor_text = b"".join(
            pkt.track_descriptor.SerializeToString() for pkt in packets if pkt.HasField("track_descriptor")
        )
        assert b"123" in descriptor_text
        assert b"456" in descriptor_text


class TestCliCombineNormalize:
    """Tests for combine --normalize behavior."""

    def test_each_input_file_is_zeroed_on_its_own(
        self,
        make_jsonl_file: JsonlFileFactory,
        run_combine: Combiner,
        combine_output: Path,
    ) -> None:
        """Per-file, not per-merge: each file's timeline starts at zero, and
        the spacing inside a file is what survives."""
        f1 = make_jsonl_file("data1.jsonl", [create_jsonl_record(ts_start=2_000_000, ts_stop=3_000_000)])
        f2 = make_jsonl_file(
            "data2.jsonl",
            [
                create_jsonl_record(ts_start=10_000_000, ts_stop=11_000_000),
                create_jsonl_record(ts_start=10_500_000, ts_stop=11_500_000),
            ],
        )
        f3 = make_jsonl_file("data3.jsonl", [create_jsonl_record(ts_start=50_000_000, ts_stop=50_050_000)])

        result = run_combine([f1, f2, f3], output=combine_output, extra_args=["--normalize", "-v"])

        assert result.returncode == 0, result.stderr
        assert "Normalizing timestamps: yes" in result.stderr
        assert pause_timestamps(combine_output) == [0, 0, 500_000, 0]

    def test_without_normalize_the_timestamps_are_the_capture_s(
        self,
        make_jsonl_file: JsonlFileFactory,
        run_combine: Combiner,
        combine_output: Path,
    ) -> None:
        f1 = make_jsonl_file(
            "data.jsonl",
            [
                create_jsonl_record(ts_start=5_000_000, ts_stop=6_000_000),
                create_jsonl_record(ts_start=10_000_000, ts_stop=11_000_000),
            ],
        )

        result = run_combine([f1], output=combine_output)

        assert result.returncode == 0, result.stderr
        assert pause_timestamps(combine_output) == [5_000_000, 10_000_000]

    def test_short_option(
        self,
        make_jsonl_file: JsonlFileFactory,
        run_combine: Combiner,
        combine_output: Path,
    ) -> None:
        f1 = make_jsonl_file("data.jsonl", [create_jsonl_record(ts_start=5_000_000, ts_stop=6_000_000)])

        result = run_combine([f1], output=combine_output, extra_args=["-n"])

        assert result.returncode == 0, result.stderr
        assert pause_timestamps(combine_output) == [0]

    def test_an_empty_file_does_not_move_the_others(
        self,
        make_jsonl_file: JsonlFileFactory,
        run_combine: Combiner,
        combine_output: Path,
    ) -> None:
        f1 = make_jsonl_file("empty.jsonl", [])
        f2 = make_jsonl_file("data.jsonl", [create_jsonl_record(ts_start=5_000_000, ts_stop=6_000_000)])

        result = run_combine([f1, f2], output=combine_output, extra_args=["--normalize"])

        assert result.returncode == 0, result.stderr
        assert pause_timestamps(combine_output) == [0]


# =============================================================================
# Subprocess Tests - JSONL to JSONL
# =============================================================================


class TestCliCombineJsonlToJsonl:
    def test_basic(self, make_jsonl_file: JsonlFileFactory, run_combine: Combiner, tmp_path: Path) -> None:
        f1 = make_jsonl_file("data1.jsonl", [create_jsonl_record()])
        output = tmp_path / "combined.jsonl"

        result = run_combine([f1], output=output, extra_args=["--output-format", "jsonl"])

        assert result.returncode == 0
        records = assert_valid_jsonl(output)
        assert records[0]["pid"] == 123
        assert records[0]["ts_start"] == 1_000_000

    def test_multiple_files(self, make_jsonl_file: JsonlFileFactory, run_combine: Combiner, tmp_path: Path) -> None:
        f1 = make_jsonl_file("data1.jsonl", [create_jsonl_record(pid=123, gen=0)])
        f2 = make_jsonl_file("data2.jsonl", [create_jsonl_record(pid=456, gen=1)])
        output = tmp_path / "combined.jsonl"

        result = run_combine([f1, f2], output=output, extra_args=["--output-format", "jsonl"])

        assert result.returncode == 0
        records = assert_valid_jsonl(output)
        assert len(records) == 2
        assert records[0]["pid"] == 123
        assert records[1]["pid"] == 456

    def test_normalize(self, make_jsonl_file: JsonlFileFactory, run_combine: Combiner, tmp_path: Path) -> None:
        """The JSONL path zeroes each pid across the whole merge, where the
        Perfetto path zeroes each input file. ADR-0021 records why."""
        f1 = make_jsonl_file(
            "data.jsonl",
            [
                create_jsonl_record(ts_start=5_000_000, ts_stop=6_000_000, collections=1, collected=100),
                create_jsonl_record(ts_start=10_000_000, ts_stop=11_000_000, collections=2, collected=200),
            ],
        )
        output = tmp_path / "combined.jsonl"

        result = run_combine([f1], output=output, extra_args=["--output-format", "jsonl", "--normalize"])

        assert result.returncode == 0
        records = assert_valid_jsonl(output)
        assert records[0]["ts_start"] == 0
        assert records[1]["ts_start"] == 5_000_000
        assert records[0]["pid"] == 123
        assert records[1]["pid"] == 123


# =============================================================================
# Subprocess Tests - What `combine` no longer reads
# =============================================================================


class TestTheChromeInputIsGone:
    """JSONL is the only input, and a `.json` file from an earlier release is
    named rather than parsed."""

    def test_the_input_format_flag_is_not_a_spelling_argparse_accepts(
        self,
        make_jsonl_file: JsonlFileFactory,
        run_combine: Combiner,
        combine_output: Path,
    ) -> None:
        """Removed rather than reduced to one choice: a flag with a single
        value is a question with one answer, and leaving it would keep
        `--input-format chrome` a thing an operator could type."""
        f1 = make_jsonl_file("data.jsonl", [create_jsonl_record()])

        result = run_combine([f1], output=combine_output, extra_args=["--input-format", "jsonl"])

        assert result.returncode == 2
        assert "--input-format" in result.stderr

    def test_chrome_is_not_an_output_format_either(
        self,
        make_jsonl_file: JsonlFileFactory,
        run_combine: Combiner,
        combine_output: Path,
    ) -> None:
        f1 = make_jsonl_file("data.jsonl", [create_jsonl_record()])

        result = run_combine([f1], output=combine_output, extra_args=["--output-format", "chrome"])

        assert result.returncode == 2
        assert "perfetto" in result.stderr
        assert "jsonl" in result.stderr

    def test_a_chrome_file_is_named_rather_than_parsed(
        self,
        make_raw_file: RawFileFactory,
        run_combine: Combiner,
        combine_output: Path,
    ) -> None:
        """What an operator with a capture from an earlier release sees. The
        first character of a Chrome trace is the `[` of a JSON array, so
        without the check msgspec would report a malformed line 1 and the file
        would read as corrupt."""
        chrome = make_raw_file("old.json", '[\n{"ph":"B","name":"GC Pause(0)","ts":1,"pid":1,"tid":0,"cat":"gc"}\n]\n')

        result = run_combine([chrome], output=combine_output)

        assert result.returncode == 1
        assert "Chrome Trace" in result.stderr
        assert "Perfetto UI" in result.stderr
        assert "malformed" not in result.stderr.lower()
        assert not combine_output.exists()


class TestCliCombineHelp:
    def test_shows_normalize_option(self, run_combine: Combiner) -> None:
        result = run_combine([], extra_args=["--help"])

        assert "--normalize" in result.stdout or "-n" in result.stdout
        assert "Normalize" in result.stdout

    def test_shows_format_options(self, run_combine: Combiner) -> None:
        result = run_combine([], extra_args=["--help"])

        assert "--output-format" in result.stdout
        assert "--input-format" not in result.stdout
        assert "jsonl" in result.stdout
        assert "perfetto" in result.stdout
        assert "chrome" not in result.stdout


# =============================================================================
# Subprocess Tests - JSONL to Perfetto
# =============================================================================


class TestCliCombineJsonlToPerfetto:
    def test_basic(
        self,
        make_jsonl_file: JsonlFileFactory,
        run_combine: Combiner,
        combine_output: Path,
    ) -> None:
        f1 = make_jsonl_file("data.jsonl", [create_jsonl_record()])

        result = run_combine([f1], output=combine_output, extra_args=["--output-format", "perfetto", "-v"])

        assert result.returncode == 0, result.stderr
        assert "Output format: perfetto" in result.stderr
        assert_valid_perfetto_format(combine_output)
        assert_perfetto_has_track_descriptor_and_event(combine_output)

    def test_normalize(
        self,
        make_jsonl_file: JsonlFileFactory,
        run_combine: Combiner,
        combine_output: Path,
    ) -> None:
        f1 = make_jsonl_file(
            "data.jsonl",
            [
                create_jsonl_record(ts_start=5_000_000, ts_stop=6_000_000, collections=1, collected=100),
                create_jsonl_record(ts_start=10_000_000, ts_stop=11_000_000, collections=2, collected=200),
            ],
        )

        result = run_combine(
            [f1], output=combine_output, extra_args=["--output-format", "perfetto", "--normalize", "-v"]
        )

        assert result.returncode == 0, result.stderr
        assert "Normalizing timestamps: yes" in result.stderr
        packets = assert_valid_perfetto_format(combine_output)
        timestamps = [pkt.timestamp for pkt in packets if pkt.HasField("timestamp")]
        assert min(timestamps) == 0
