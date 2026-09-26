"""Reading and writing gcmon's JSONL capture format (docs/formats.md)."""

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

import msgspec

from ..exporters.trace_converter import convert_to_trace_format
from ..model.data import GCStatsInfo, from_mapping
from ..model.names import PID
from ..model.protocol import (
    JsonlRecord,
    TItem,
    TMapping,
    is_gc_stats,
    is_instant,
    is_loss,
    to_mapping,
)
from ..model.trace_event import TraceEvent
from ..support.vocabulary import ENCODING

__all__ = [
    "convert_jsonl_to_trace_format",
    "normalize_jsonl_timestamps",
    "read_jsonl",
    "write_jsonl",
]


def json_to_item(data: TMapping) -> tuple[int, TItem]:
    pid = data[PID]
    # A pid gcmon wrote decodes as an int, and every line of a capture carries
    # one. Anything else goes through a lax convert, so a pid written as a
    # string still reads.
    if not isinstance(pid, int):
        pid = msgspec.convert(pid, int, strict=False)
    return pid, from_mapping(data)


def read_jsonl(filename: Path) -> dict[int, list[TItem]]:
    items: dict[int, list[TItem]] = {}
    first = True
    with open(filename, encoding=ENCODING) as f:
        for number, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            if first:
                first = False
                if line.startswith("["):
                    raise ValueError(
                        f"{filename} is a Chrome Trace file, which gcmon no longer reads. "
                        "The Perfetto UI still opens it."
                    )
            try:
                data = msgspec.json.decode(line)
                if not isinstance(data, dict) or PID not in data:
                    raise ValueError(f"not a gcmon record: expected a JSON object with a {PID}")
                pid, item = json_to_item(data)
            except ValueError as e:
                # Every msgspec error is a ValueError, and none of them says where.
                raise ValueError(f"{filename}:{number}: {e}") from e
            if pid not in items:
                items[pid] = [item]
            else:
                items[pid].append(item)

    return items


def convert_jsonl_to_trace_format(path: Path) -> list[TraceEvent]:
    items = read_jsonl(path)
    return convert_to_trace_format(items)


def write_jsonl(filename: Path, items: Mapping[int, Sequence[TItem]]) -> None:
    """Write GC stats items to a JSONL file."""
    with open(filename, "wb") as f:
        for pid, pid_items in items.items():
            for item in pid_items:
                rec: JsonlRecord = {PID: pid}
                rec.update(to_mapping(item))
                f.write(msgspec.json.encode(rec))
                f.write(b"\n")
            f.flush()


# Every field of a GC record that holds a timestamp: the pause's two, and
# the sub-phases'.
_TIMESTAMP_FIELDS: Final = tuple(name for name in GCStatsInfo.__struct_fields__ if name.startswith("ts_"))


def normalize_jsonl_timestamps(items: Mapping[int, Sequence[TItem]]) -> None:
    for pid_items in items.values():
        timestamps: list[int] = []
        for item in pid_items:
            if is_instant(item):
                timestamps.append(item.ts)
            else:
                assert is_loss(item) or is_gc_stats(item)
                timestamps.append(item.ts_start)
        if not timestamps:
            continue

        min_ts = min(timestamps)

        for item in pid_items:
            if is_instant(item):
                item.ts -= min_ts
            elif is_loss(item):
                item.ts_start -= min_ts
                item.ts_stop -= min_ts
            else:
                assert is_gc_stats(item)
                # A record read back from a capture, holding None for every
                # timestamp it lacks.
                for name in _TIMESTAMP_FIELDS:
                    ts = getattr(item, name, None)
                    if ts is not None:
                        setattr(item, name, ts - min_ts)
