from pathlib import Path

# Imported as a module, not by name: `case NAME:` binds a variable where
# `case module.NAME:` compares against its value.
from ..support import vocabulary
from .exporter import EventsExporter
from .jsonl_exporter import JsonlExporter
from .perfetto_exporter import PerfettoExporter
from .stdout_exporter import StdoutExporter


class EventsExporterFactory:
    def __init__(self, output_format: str, output_path: Path, flush_threshold: int):
        self._output_format = output_format
        self._output_path = output_path
        self._flush_threshold = flush_threshold

    def __call__(self) -> EventsExporter:
        match self._output_format:
            case vocabulary.FORMAT_STDOUT:
                return StdoutExporter(flush_threshold=self._flush_threshold)
            case vocabulary.FORMAT_JSONL:
                return JsonlExporter(output_path=self._output_path, flush_threshold=self._flush_threshold)
            case vocabulary.FORMAT_PERFETTO:
                return PerfettoExporter(output_path=self._output_path, flush_threshold=self._flush_threshold)
            case _:
                raise ValueError(f"Unknown output format: {self._output_format}")
