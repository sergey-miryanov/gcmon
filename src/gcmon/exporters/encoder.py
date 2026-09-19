"""The encoder that turns batches of ``TraceEvent`` into a Perfetto trace.

``combine`` drives it with no exporter around it (ADR-0008).
"""

from __future__ import annotations

import zlib
from collections.abc import Callable, Sequence, Set
from functools import partial
from pathlib import Path
from typing import NamedTuple

from ..model.process import Process
from ..model.trace_event import TraceEvent
from .perfetto_format import (
    PerfettoTrackState,
    TraceField,
    TracePacketField,
    convert_trace_events_to_perfetto,
    emit_retired_process_row,
    finalize_perfetto_packets,
)
from .protobuf_encoder import encode_bytes_field

_DEFLATE_LEVEL = 6
_ZSTD_LEVEL = 3


class Codec(NamedTuple):
    """A compressed-batch field and the compressor that fills it."""

    field: TracePacketField
    compress: Callable[[bytes], bytes]


_DEFLATE = Codec(TracePacketField.COMPRESSED_PACKETS, partial(zlib.compress, level=_DEFLATE_LEVEL))


def _resolve_codec() -> Codec:
    """The codec this interpreter can write (ADR-0022)."""
    try:
        from compression import zstd
    except ImportError:
        return _DEFLATE
    return Codec(TracePacketField.ZSTD_COMPRESSED_PACKETS, partial(zstd.compress, level=_ZSTD_LEVEL))


_CODEC = _resolve_codec()

__all__ = [
    "ProtobufEventEncoder",
    "convert_trace_events_to_perfetto",
]


class ProtobufEventEncoder:
    """Encoder for Perfetto binary protobuf format."""

    def __init__(
        self,
        sequence_id: int | None = None,
        codec: Codec | None = None,
    ) -> None:
        self._path: Path | None = None
        self._track_state = PerfettoTrackState()
        self._sequence_id: int = sequence_id if sequence_id is not None else id(self) & 0x7FFFFFFF
        self._has_written: bool = False
        self._retired: list[Process] = []
        self._codec: Codec = codec if codec is not None else _CODEC

    def open(self, path: Path) -> None:
        """Bind this encoder to *path*. One encoder writes one trace.

        The track state is per-trace -- uuid allocation, descriptor dedup,
        the ``Processes`` once-per-trace flag -- so a second trace would
        come out missing its descriptors and its whole ``Processes``
        track, with nothing raised. Construct a new encoder per file.
        """
        assert self._path is None, "one encoder writes one trace; construct a new encoder per file"
        self._path = path
        self._has_written = False

    def record_process_cmdline(self, process: Process, cmdline: tuple[str, ...] | None) -> None:
        """Keep what *process* is running, for its descriptor and its
        ``Processes``-track span to name.
        """
        self._track_state.set_cmdline(process, cmdline)

    def record_process_liveness(self, processes: Set[Process], ts_ns: int) -> None:
        """Fold a whole tick's liveness observations into the
        ``Processes``-track span accumulator: *processes* are the ones
        gcmon read GC state out of at *ts_ns*.

        See ADR-0029. Writes nothing; the observations reach the file at
        ``close()``.
        """
        for process in processes:
            self._track_state.update_process_lifetime(process, ts_ns)

    def record_process_retired(self, process: Process) -> None:
        """Note that gcmon has let go of *process*, so its own row can be
        drawn without waiting for the end of the run.

        Writes nothing here: the row goes out with the next batch, once the
        events queued ahead of it have reached the span accumulator. See
        ADR-0028 for what that buys a run killed mid-flight.
        """
        self._retired.append(process)

    def _drain_retired(self) -> list[bytes]:
        """The rows of every process retired since the last batch."""
        packets: list[bytes] = []
        for process in self._retired:
            packets.extend(emit_retired_process_row(process, self._track_state, self._sequence_id))
        self._retired.clear()
        return packets

    def _write_batch(self, descriptors: Sequence[bytes], packets: Sequence[bytes]) -> None:
        """Append one batch to the trace as a single compressed packet."""
        assert self._path is not None, "open() must be called before writing"
        batch = b"".join(encode_bytes_field(TraceField.PACKET, entry) for entry in (*descriptors, *packets))
        compressed = encode_bytes_field(self._codec.field, self._codec.compress(batch))
        mode = "wb" if not self._has_written else "ab"
        self._has_written = True
        with open(self._path, mode) as f:
            f.write(encode_bytes_field(TraceField.PACKET, compressed))
            f.flush()

    def write_events(self, events: Sequence[TraceEvent]) -> None:
        if not events:
            return
        assert self._path is not None, "open() must be called before write_events()"
        descriptors, packets = convert_trace_events_to_perfetto(
            list(events),
            self._track_state,
            self._sequence_id,
        )
        # After the convert pass, so a queued event has reached the span
        # accumulator before the bar is drawn over it.
        retired = self._drain_retired()
        if not descriptors and not packets and not retired:
            return
        self._write_batch(descriptors, [*packets, *retired])

    def close(self) -> None:
        """Emit the ``Processes`` track and finish the file.

        The guard is on having packets, not on having written earlier:
        liveness reaches ``_track_state`` without going through
        ``write_events``, so a run in which nothing ever collected has a
        track to emit and no bytes on disk yet. A trace with nothing at
        all still produces no file.
        """
        if self._path is None:
            return
        packets = finalize_perfetto_packets(self._track_state, self._sequence_id)
        if not packets:
            return
        self._write_batch((), packets)
