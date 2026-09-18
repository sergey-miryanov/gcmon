"""Tests for the StdoutExporter."""

import json
import sys
from typing import Any

import pytest

from gcmon.exporters import StdoutExporter
from gcmon.model.data import LossMsg
from gcmon.model.names import (
    CANDIDATES,
    COLLECTED,
    COLLECTIONS,
    DURATION,
    GEN,
    GENS,
    HEAP_SIZE,
    IID,
    LOST_COUNT,
    LOST_FROM,
    LOST_PAUSE_NS,
    OBSERVED_COUNT,
    PID,
    TS_START,
    TS_STOP,
    UNCOLLECTABLE,
)
from gcmon.model.protocol import TGCStatsInfo
from tests.conftest import DEFAULT_PID
from tests.helpers import create_mock_loss_item, create_mock_stats_item, proc


class TestStdoutExporter:
    """Tests for StdoutExporter class."""

    def test_init_default_parameters(self) -> None:
        """Test StdoutExporter initialization with default parameters."""
        exporter = StdoutExporter()

        assert exporter._flush_threshold == 100
        assert exporter._output is sys.stdout

    def test_init_custom_parameters(self) -> None:
        """Test StdoutExporter initialization with custom parameters."""
        exporter = StdoutExporter(flush_threshold=50)

        assert exporter._flush_threshold == 50
        assert exporter._output is sys.stdout

    def test_add_event_json_output_format(
        self, mock_stats_item: TGCStatsInfo, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Test that add_event outputs correct JSON format to stdout."""
        exporter = StdoutExporter()

        exporter.add_event(proc(DEFAULT_PID), mock_stats_item)
        exporter.close()

        captured = capsys.readouterr()
        output = captured.out.strip()

        # Should be valid JSON
        data: dict[str, Any] = json.loads(output)

        # Verify all fields are present
        assert data[PID] == 12345
        assert data[IID] == 0
        assert data[GEN] == 0
        assert data[TS_START] == 1_500_000_000
        assert data[COLLECTIONS] == 50
        assert data[COLLECTED] == 200
        assert data[UNCOLLECTABLE] == 10
        assert data[CANDIDATES] == 40
        assert data[HEAP_SIZE] == 52428800
        assert data[DURATION] == 0.005

    def test_add_event_multiple_events(
        self, mock_stats_item_batch: list[TGCStatsInfo], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Test output with multiple events."""
        exporter = StdoutExporter()

        for item in mock_stats_item_batch:
            exporter.add_event(proc(DEFAULT_PID), item)
        exporter.close()

        captured = capsys.readouterr()
        lines = captured.out.strip().split("\n")
        # Should have 3 lines (one per event)
        assert len(lines) == 3

        # Verify each line is valid JSON with correct generation
        for i, line in enumerate(lines):
            data: dict[str, Any] = json.loads(line)
            assert data[GEN] == i

    def test_close_writes_what_was_buffered(
        self, mock_stats_item: TGCStatsInfo, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A threshold the one event stays under, so only `close` writes it."""
        exporter = StdoutExporter(flush_threshold=1000)
        exporter.add_event(proc(DEFAULT_PID), mock_stats_item)

        exporter.close()

        captured = capsys.readouterr()
        assert captured.out != ""

    def test_add_event_output_to_stdout(
        self, mock_stats_item: TGCStatsInfo, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Test that add_event writes to stdout (not stderr)."""
        exporter = StdoutExporter()

        exporter.add_event(proc(DEFAULT_PID), mock_stats_item)
        exporter.close()

        captured = capsys.readouterr()

        # Output should be in stdout
        assert captured.out != ""
        # stderr should be empty
        assert captured.err == ""

    def test_add_event_json_is_single_line(
        self, mock_stats_item: TGCStatsInfo, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Test that each event is written as a single JSON line."""
        exporter = StdoutExporter()

        exporter.add_event(proc(DEFAULT_PID), mock_stats_item)
        exporter.close()

        captured = capsys.readouterr()
        output = captured.out

        # Should be exactly one line (plus newline)
        lines = output.strip().split("\n")
        assert len(lines) == 1

        # Should be valid JSON
        data: dict[str, Any] = json.loads(output.strip())
        assert isinstance(data, dict)

    def test_interpreter_id_in_output(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Test that the interpreter ID appears in output."""
        exporter = StdoutExporter()
        stats_item = create_mock_stats_item(iid=42)

        exporter.add_event(proc(DEFAULT_PID), stats_item)
        exporter.close()

        captured = capsys.readouterr()
        data: dict[str, Any] = json.loads(captured.out.strip())
        assert data[IID] == 42

    def test_pid_in_output(self, mock_stats_item: TGCStatsInfo, capsys: pytest.CaptureFixture[str]) -> None:
        """Test that PID appears in output."""
        exporter = StdoutExporter()

        exporter.add_event(proc(99999), mock_stats_item)
        exporter.close()

        captured = capsys.readouterr()
        data: dict[str, Any] = json.loads(captured.out.strip())
        assert data[PID] == 99999


class TestStdoutLossRecords:
    """A stream is the one output nobody re-reads, so a loss record dropped
    here is a lossy run that reads as a clean one and nothing to check it
    against later."""

    def _loss(self) -> LossMsg:
        return create_mock_loss_item(
            iid=3, gen=1, ts_start=1_000, ts_stop=9_000, observed_count=4, lost_count=76, lost_pause_ns=8_100_000
        )

    def _emit(self, capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
        exporter = StdoutExporter()
        exporter.add_loss_event(proc(DEFAULT_PID), self._loss())
        exporter.close()

        data: dict[str, Any] = json.loads(capsys.readouterr().out.strip())
        return data

    def test_it_writes_one_line(self, capsys: pytest.CaptureFixture[str]) -> None:
        data = self._emit(capsys)

        assert data[PID] == DEFAULT_PID
        assert (data[TS_START], data[TS_STOP]) == (1_000, 9_000)

    def test_it_carries_the_counts_and_the_pause(self, capsys: pytest.CaptureFixture[str]) -> None:
        """One entry per generation the interval touched, counts and all: the
        stream is the whole record, so anything left out of it is gone."""
        data = self._emit(capsys)

        assert data[GENS] == [{GEN: 1, OBSERVED_COUNT: 4, LOST_FROM: 0, LOST_COUNT: 76, LOST_PAUSE_NS: 8_100_000}]

    def test_it_names_the_interpreter_that_lost_the_records(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A stream and a capture of the same run agree on which interpreter
        lost the records, and neither carries a track number."""
        data = self._emit(capsys)

        assert data[IID] == 3
        assert "tid" not in data
