"""Reading and writing gcmon's JSONL capture format (docs/formats.md)."""

from collections.abc import Mapping, Sequence
from pathlib import Path

import msgspec

from ..exporters.trace_converter import convert_to_trace_format
from ..model.data import from_mapping
from ..model.protocol import (
    JsonlRecord,
    TItem,
    TMapping,
    has_clear_weakrefs,
    has_deduce_unreachable,
    has_delete_garbage,
    has_finalize_garbage,
    has_handle_resurrected,
    has_handle_weakrefs,
    has_incremental,
    has_mark_alive,
    is_gc_stats,
    is_instant,
    is_loss,
    to_mapping,
)
from ..model.trace_event import TraceEvent

__all__ = [
    "convert_jsonl_to_trace_format",
    "normalize_jsonl_timestamps",
    "read_jsonl",
    "write_jsonl",
]


def json_to_item(data: TMapping) -> tuple[int, TItem]:
    pid = data["pid"]
    # A pid gcmon wrote decodes as an int, and every line of a capture carries
    # one. Anything else goes through a lax convert, so a pid written as a
    # string still reads.
    if not isinstance(pid, int):
        pid = msgspec.convert(pid, int, strict=False)
    return pid, from_mapping(data)


def read_jsonl(filename: Path) -> dict[int, list[TItem]]:
    items: dict[int, list[TItem]] = {}
    first = True
    with open(filename, encoding="utf-8") as f:
        for line in f:
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
            pid, item = json_to_item(msgspec.json.decode(line))
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
                rec: JsonlRecord = {"pid": pid}
                rec.update(to_mapping(item))
                f.write(msgspec.json.encode(rec))
                f.write(b"\n")
            f.flush()


def normalize_jsonl_timestamps(items: Mapping[int, Sequence[TItem]]) -> None:
    for pid_items in items.values():
        timestamps: list[int] = []
        for item in pid_items:
            if is_instant(item):
                timestamps.append(item.ts)
            elif is_loss(item) or is_gc_stats(item):
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
            elif is_gc_stats(item):
                item.ts_start -= min_ts
                item.ts_stop -= min_ts
                if has_mark_alive(item):
                    item.ts_mark_alive_start -= min_ts
                    item.ts_mark_alive_stop -= min_ts
                if has_incremental(item):
                    item.ts_fill_increment_start -= min_ts
                    item.ts_fill_increment_stop -= min_ts
                if has_deduce_unreachable(item):
                    item.ts_deduce_unreachable_start -= min_ts
                    item.ts_deduce_unreachable_stop -= min_ts
                if has_handle_weakrefs(item):
                    item.ts_handle_weakref_callbacks_start -= min_ts
                    item.ts_handle_weakref_callbacks_stop -= min_ts
                if has_finalize_garbage(item):
                    item.ts_finalize_garbage_stop -= min_ts
                if has_handle_resurrected(item):
                    item.ts_handle_resurrected_stop -= min_ts
                if has_clear_weakrefs(item):
                    item.ts_clear_weakrefs_stop -= min_ts
                if has_delete_garbage(item):
                    item.ts_delete_garbage_start -= min_ts
                    item.ts_delete_garbage_stop -= min_ts
