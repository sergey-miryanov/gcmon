"""The `gcmon combine` command: many captures in, one trace out."""

from pathlib import Path

from ..exporters.encoder import ProtobufEventEncoder
from ..exporters.jsonl_io import normalize_jsonl_timestamps, read_jsonl, write_jsonl
from ..exporters.trace_converter import convert_to_trace_format
from ..model.protocol import TItem
from ..model.trace_event import Slice, TraceEvent

__all__ = [
    "combine_files",
]


def _starts_at(event: TraceEvent) -> int:
    """When *event* happens, whichever kind it is.

    A `Slice` spells it `ts_start`, because it also has an end.
    """
    return event.ts_start if isinstance(event, Slice) else event.ts


def _normalize_trace_timestamps(events: list[TraceEvent]) -> None:
    """Shift each pid's events so its earliest lands at zero.

    Every timestamp on an event moves, not only the one it starts at: a
    `Slice` carries an absolute end, so leaving `ts_stop` behind would
    stretch every span back to the old origin without failing anything.
    """
    by_pid: dict[int, list[TraceEvent]] = {}
    for event in events:
        by_pid.setdefault(event.track.process.pid, []).append(event)

    for timed in by_pid.values():
        min_ts = min(_starts_at(e) for e in timed)
        for e in timed:
            if isinstance(e, Slice):
                e.ts_start = e.ts_start - min_ts
                e.ts_stop = e.ts_stop - min_ts
            else:
                e.ts = e.ts - min_ts


def combine_files(
    input_paths: list[Path],
    output_path: Path,
    normalize: bool = False,
    output_format: str = "perfetto",
) -> None:
    """Merge JSONL captures into one JSONL capture or one Perfetto trace.

    The two paths normalize over different scopes; ADR-0021 records why.
    """
    if output_format == "jsonl":
        all_items: dict[int, list[TItem]] = {}

        for input_path in input_paths:
            items = read_jsonl(input_path)
            for pid, pid_items in items.items():
                if pid not in all_items:
                    all_items[pid] = pid_items
                else:
                    all_items[pid].extend(pid_items)

        if normalize:
            normalize_jsonl_timestamps(all_items)

        write_jsonl(output_path, all_items)
        return

    if output_format != "perfetto":
        raise ValueError(f"Unsupported output format: {output_format}")

    trace_events: list[TraceEvent] = []

    for input_path in input_paths:
        file_events = convert_to_trace_format(read_jsonl(input_path))

        if normalize:
            _normalize_trace_timestamps(file_events)

        trace_events.extend(file_events)

    perfetto_encoder = ProtobufEventEncoder()
    perfetto_encoder.open(output_path)
    try:
        perfetto_encoder.write_events(trace_events)
    finally:
        perfetto_encoder.close()
