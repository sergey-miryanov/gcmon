"""Stdout exporter for GC monitoring data.

Exports GC events to stdout in a one-line-per-item format (JSONL/NDJSON).
"""

import contextlib
import sys
from contextlib import AbstractContextManager
from typing import TextIO, override

from .jsonl_exporter import JsonlExporter

__all__ = ["StdoutExporter"]


class StdoutExporter(JsonlExporter):
    """
    Exporter that writes GC events to stdout, one JSON object per line.

    Each event is written as a single line of JSON (JSONL/NDJSON format),
    making it easy to pipe to log aggregators or processing tools.

    Events are buffered in memory and flushed to stdout when the buffer
    reaches flush_threshold events.
    """

    def __init__(
        self,
        flush_threshold: int = 100,
        output: TextIO | None = None,
    ) -> None:
        super().__init__(flush_threshold=flush_threshold)
        self._output = output or sys.stdout

    @override
    def _open_writer(self) -> AbstractContextManager[TextIO]:
        return contextlib.nullcontext(self._output)
