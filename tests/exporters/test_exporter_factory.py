"""Tests for EventsExporterFactory."""

from pathlib import Path

import pytest

from gcmon.exporters.exporter_factory import EventsExporterFactory
from gcmon.exporters.jsonl_exporter import JsonlExporter
from gcmon.exporters.perfetto_exporter import PerfettoExporter
from gcmon.exporters.stdout_exporter import StdoutExporter
from gcmon.model.data import GCStatsInfo
from gcmon.support.vocabulary import FORMAT_JSONL, FORMAT_PERFETTO, FORMAT_STDOUT
from tests.conftest import DEFAULT_PID
from tests.helpers import proc


class TestEventsExporterFactory:
    def test_stdout_format(self, tmp_path: Path) -> None:
        factory = EventsExporterFactory(FORMAT_STDOUT, tmp_path / "out", 100)
        exporter = factory()
        assert isinstance(exporter, StdoutExporter)

    def test_jsonl_format(self, tmp_path: Path) -> None:
        factory = EventsExporterFactory(FORMAT_JSONL, tmp_path / "out.jsonl", 100)
        exporter = factory()
        assert isinstance(exporter, JsonlExporter)

    def test_perfetto_format(self, tmp_path: Path) -> None:
        factory = EventsExporterFactory(FORMAT_PERFETTO, tmp_path / "out.pb", 100)
        exporter = factory()
        assert isinstance(exporter, PerfettoExporter)

    def test_unknown_format_raises_value_error(self, tmp_path: Path) -> None:
        factory = EventsExporterFactory("unknown", tmp_path / "out", 100)
        with pytest.raises(ValueError, match="Unknown output format: unknown"):
            factory()

    @pytest.mark.parametrize("output_format", [FORMAT_JSONL, FORMAT_PERFETTO])
    def test_the_exporter_writes_where_and_when_the_factory_was_told(
        self, tmp_path: Path, mock_stats_item: GCStatsInfo, output_format: str
    ) -> None:
        """Two events against a threshold of two: the file is there before
        any close, and it is the file the factory was given."""
        path = tmp_path / "out"
        exporter = EventsExporterFactory(output_format, path, 2)()

        exporter.add_event(proc(DEFAULT_PID), mock_stats_item)
        exporter.add_event(proc(DEFAULT_PID), mock_stats_item)

        assert path.exists()

    def test_the_stdout_exporter_prints_when_the_factory_was_told(
        self, tmp_path: Path, mock_stats_item: GCStatsInfo, capsys: pytest.CaptureFixture[str]
    ) -> None:
        exporter = EventsExporterFactory(FORMAT_STDOUT, tmp_path / "unused", 2)()

        exporter.add_event(proc(DEFAULT_PID), mock_stats_item)
        exporter.add_event(proc(DEFAULT_PID), mock_stats_item)

        assert len(capsys.readouterr().out.splitlines()) == 2
