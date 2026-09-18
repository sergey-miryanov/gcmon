"""Tests for the no-op defaults on the `EventsExporter` base class."""

from __future__ import annotations

from pathlib import Path

import pytest

from gcmon.exporters.exporter import EventsExporter
from gcmon.exporters.jsonl_exporter import JsonlExporter
from gcmon.exporters.stdout_exporter import StdoutExporter
from tests.data_helpers import create_instant_msg
from tests.helpers import create_mock_stats_item, proc


class TestAddProcessLivenessIsPerfettoOnly:
    """Liveness is a ``Processes``-track concern, so every format but
    Perfetto reaches the base no-op on ``EventsExporter`` and comes out
    byte-identical to a run that never reported any. See ADR-0011."""

    def _write(self, path: Path, exporter: EventsExporter, *, with_liveness: bool) -> bytes:
        exporter.add_event(proc(100), create_mock_stats_item())
        if with_liveness:
            exporter.add_process_liveness({proc(100), proc(200)}, 1_400_000_000)
        exporter.add_instant_event(proc(100), create_instant_msg(name="marker", ts=1_600_000_000))
        if with_liveness:
            exporter.add_process_liveness({proc(100), proc(200)}, 1_800_000_000)
        exporter.close()
        return path.read_bytes()

    def test_jsonl_output_is_unchanged(self, tmp_path: Path) -> None:
        quiet = tmp_path / "quiet.jsonl"
        loud = tmp_path / "loud.jsonl"

        assert self._write(quiet, JsonlExporter(quiet, flush_threshold=1000), with_liveness=False) == self._write(
            loud, JsonlExporter(loud, flush_threshold=1000), with_liveness=True
        )

    def test_stdout_output_is_unchanged(self, capsys: pytest.CaptureFixture[str]) -> None:
        exporter = StdoutExporter(flush_threshold=1000)
        exporter.add_event(proc(100), create_mock_stats_item())
        exporter.close()
        without = capsys.readouterr().out

        exporter = StdoutExporter(flush_threshold=1000)
        exporter.add_event(proc(100), create_mock_stats_item())
        exporter.add_process_liveness({proc(100), proc(200)}, 1_400_000_000)
        exporter.close()

        assert capsys.readouterr().out == without
