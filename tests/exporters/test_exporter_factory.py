"""Tests for EventsExporterFactory."""

from pathlib import Path

import pytest

from gcmon.exporters.exporter_factory import EventsExporterFactory
from gcmon.exporters.jsonl_exporter import JsonlExporter
from gcmon.exporters.perfetto_exporter import PerfettoExporter
from gcmon.exporters.stdout_exporter import StdoutExporter
from gcmon.support.vocabulary import FORMAT_JSONL, FORMAT_PERFETTO, FORMAT_STDOUT


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

    def test_factory_forwards_parameters(self, tmp_path: Path) -> None:
        path = tmp_path / "out.jsonl"
        factory = EventsExporterFactory(FORMAT_JSONL, path, 50)
        exporter = factory()
        assert isinstance(exporter, JsonlExporter)
        assert exporter._flush_threshold == 50
        assert exporter._output_path == path
