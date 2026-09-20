"""Perfetto binary protobuf exporter for GC monitoring data."""

import threading
from collections.abc import Set
from pathlib import Path
from typing import override

from ..model.names import RSS
from ..model.process import Process
from ..model.protocol import TGCStatsInfo, TInstantMsg, TLossMsg
from ..model.trace_event import Counter, Instant, ProcessTrack, TraceEvent
from .encoder import Codec, ProtobufEventEncoder
from .exporter import EventsExporter
from .trace_converter import convert_item_to_trace_format, convert_loss_to_trace_format

__all__ = [
    "PerfettoExporter",
]


class PerfettoExporter(EventsExporter):
    """Buffer what the monitor reports as `TraceEvent`s, and write them as
    a Perfetto trace.

    One class rather than a buffering base and a subclass on top; see
    ADR-0008.

    **One lock, `_io_lock`, guards the buffer and every touch of encoder
    state.** It is held across both deciding what to write and writing it,
    which is what makes the order those touches happen in the order the
    calls arrived in. A second lock for the buffer alone left a window
    between the two: a flush holding events it had taken out of the buffer
    and nothing serializing it against a retirement, which then drew a span
    from an accumulator those events had not reached and came out short at
    whichever end they would have moved (ADR-0011). The price of the one
    lock is that an append waits for a write already in progress.
    """

    def __init__(
        self,
        output_path: Path,
        flush_threshold: int = 1000,
        sequence_id: int | None = None,
        codec: Codec | None = None,
    ) -> None:
        super().__init__()
        self._io_lock = threading.Lock()
        self._buffer: list[TraceEvent] = []
        self._flush_threshold = flush_threshold
        self._output_path = output_path
        self._encoder = ProtobufEventEncoder(sequence_id=sequence_id, codec=codec)
        self._closed = False
        self._encoder.open(output_path)

    def _enqueue(self, events: list[TraceEvent]) -> None:
        with self._io_lock:
            if self._closed:
                return
            self._buffer.extend(events)
            if len(self._buffer) < self._flush_threshold:
                return
            to_write = self._buffer[:]
            self._buffer.clear()
            self._encoder.write_events(to_write)

    @override
    def add_event(self, process: Process, item: TGCStatsInfo) -> None:
        self._enqueue(convert_item_to_trace_format(process, item))

    @override
    def add_instant_event(self, process: Process, item: TInstantMsg) -> None:
        self._enqueue([Instant(ProcessTrack(process), item.name, item.ts)])

    @override
    def add_rss_sample(self, process: Process, rss_bytes: int, ts_ns: int) -> None:
        self._enqueue([Counter(ProcessTrack(process), RSS, RSS, ts_ns, rss_bytes)])

    @override
    def add_loss_event(self, process: Process, item: TLossMsg) -> None:
        self._enqueue(convert_loss_to_trace_format(process, item))

    @override
    def add_process_cmdline(self, process: Process, cmdline: tuple[str, ...] | None) -> None:
        """Hand the encoder what *process* is running.

        Under ``_io_lock`` for the reason ``add_process_liveness`` gives.
        """
        with self._io_lock:
            self._encoder.record_process_cmdline(process, cmdline)

    @override
    def add_process_retired(self, process: Process) -> None:
        """Let the encoder draw *process*'s row from the next flush on.

        Under ``_io_lock`` for the reason ``add_process_liveness`` gives.
        """
        with self._io_lock:
            self._encoder.record_process_retired(process)

    @override
    def add_process_liveness(self, processes: Set[Process], ts_ns: int) -> None:
        """Fold one tick's liveness observations into the encoder's span
        accumulator.

        ``_io_lock`` is not optional: it guards every other touch of the
        encoder, and both a flush and ``close()`` can run on another
        thread. Without it a concurrent read-modify-write can drop a
        min/max update, and a new pid arriving mid-``close()`` can raise
        ``RuntimeError: dictionary changed size during iteration`` out of
        ``get_process_lifetimes``.
        """
        with self._io_lock:
            self._encoder.record_process_liveness(processes, ts_ns)

    @override
    def close(self) -> None:
        """Drain the buffer and close the encoder.

        Under the lock from the first statement, for the reason the class
        gives: a flush that read the buffer outside it could reach the
        encoder after the closeout had gone out.
        """
        with self._io_lock:
            if self._closed:
                return
            self._closed = True
            remaining = self._buffer[:]
            self._buffer.clear()
            if remaining:
                self._encoder.write_events(remaining)
            self._encoder.close()
