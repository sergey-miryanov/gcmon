"""Shared conversion from GC stats items to TraceEvent objects."""

from collections.abc import Mapping, Sequence

from ..model.process import Process
from ..model.protocol import (
    TGCStatsInfo,
    TGenLoss,
    TItem,
    TLossMsg,
    has_clear_weakrefs,
    has_deduce_unreachable,
    has_delete_garbage,
    has_finalize_garbage,
    has_handle_resurrected,
    has_handle_weakrefs,
    has_incremental,
    has_mark_alive,
    has_new_incremental,
    is_gc_stats,
    is_instant,
    is_loss,
)
from ..model.trace_event import (
    ArgGroup,
    Counter,
    EventArgs,
    Instant,
    InterpreterTrack,
    LossTrack,
    ProcessTrack,
    Slice,
    TraceEvent,
)

__all__ = [
    "GC_PAUSE_CATEGORY",
    "convert_item_to_trace_format",
    "convert_loss_to_trace_format",
    "convert_to_trace_format",
]

# The category of the one slice every record produces, whatever phases it
# ran. It is what lets a reader of the event stream count records: the
# sub-phase slices and the counters are per record too, but conditional.
GC_PAUSE_CATEGORY: str = "gc.pause"


def convert_item_to_trace_format(process: Process, item: TGCStatsInfo) -> list[TraceEvent]:
    gen = item.gen
    iid = item.iid
    track = InterpreterTrack(process, iid)
    ts_start_ns = item.ts_start
    ts_stop_ns = item.ts_stop

    pause_data: EventArgs = {
        "generation": gen,
        "iid": iid,
        "collections": item.collections,
        "heap_size": item.heap_size,
        "collected": item.collected,
        "uncollectable": item.uncollectable,
        "candidates": item.candidates,
    }

    counter_data: dict[str, int | float] = {
        "collected": item.collected,
        "candidates": item.candidates,
        "duration": item.duration,
    }
    if item.uncollectable:
        counter_data["uncollectable"] = item.uncollectable

    if has_incremental(item) and gen < 2:
        pause_data["increment_size"] = item.increment_size

    if has_mark_alive(item) and gen > 0:
        pause_data["alive_size"] = item.alive_size

    if has_finalize_garbage(item):
        pause_data["finalized_garbage_count"] = item.finalized_garbage_count

    if has_delete_garbage(item):
        pause_data["deleted_garbage_count"] = item.deleted_garbage_count

    if has_clear_weakrefs(item):
        pause_data["clear_weakrefs_count"] = item.clear_weakrefs_count

    if has_new_incremental(item):
        pause_data["old_work"] = item.old_work
        pause_data["auto_collect"] = item.auto_collect
        pause_data["aging_threshold"] = item.aging_threshold
        pause_data["aging_spaces"] = item.aging_spaces
        pause_data["aging_next"] = item.aging_next
        pause_data["survivor_count"] = item.survivor_count
        pause_data["heap_size_stop"] = item.heap_size_stop
        if gen == 1:
            pause_data["increment_size"] = item.increment_size

    events: list[TraceEvent] = []
    # Ahead of the sub-phases nested inside it, so its BEGIN wins the tie
    # against a sub-phase starting where the pause does.
    events.append(
        Slice(
            track,
            f"GC Pause({gen})",
            f"{GC_PAUSE_CATEGORY}(gen={gen})",
            ts_start_ns,
            ts_stop_ns,
            pause_data,
        )
    )

    if has_mark_alive(item) and item.ts_mark_alive_stop - item.ts_mark_alive_start > 0:
        inc_data: EventArgs = {"generation": gen, "iid": iid, "alive_size": item.alive_size}
        events.append(
            Slice(
                track,
                f"Mark Alive({gen})",
                f"gc.mark.alive(gen={gen})",
                item.ts_mark_alive_start,
                item.ts_mark_alive_stop,
                inc_data,
            )
        )

    if has_incremental(item) and item.ts_fill_increment_stop - item.ts_fill_increment_start > 0:
        inc_data = {"generation": gen, "iid": iid, "increment_size": item.increment_size}
        events.append(
            Slice(
                track,
                f"Fill increment({gen})",
                f"gc.increment(gen={gen})",
                item.ts_fill_increment_start,
                item.ts_fill_increment_stop,
                inc_data,
            )
        )

    if has_deduce_unreachable(item) and item.ts_deduce_unreachable_stop - item.ts_deduce_unreachable_start > 0:
        inc_data = {"generation": gen, "iid": iid, "candidates": item.candidates}
        events.append(
            Slice(
                track,
                f"Deduce Unreachable({gen})",
                f"gc.deduce(gen={gen})",
                item.ts_deduce_unreachable_start,
                item.ts_deduce_unreachable_stop,
                inc_data,
            )
        )

    if has_handle_weakrefs(item) and item.ts_handle_weakref_callbacks_stop - item.ts_handle_weakref_callbacks_start > 0:
        inc_data = {"generation": gen, "iid": iid}
        events.append(
            Slice(
                track,
                f"Handle Weakrefs Callbacks({gen})",
                f"gc.weakrefs(gen={gen})",
                item.ts_handle_weakref_callbacks_start,
                item.ts_handle_weakref_callbacks_stop,
                inc_data,
            )
        )

    if has_finalize_garbage(item) and item.ts_finalize_garbage_stop - item.ts_handle_weakref_callbacks_stop > 0:
        inc_data = {"generation": gen, "iid": iid, "finalized_garbage_count": item.finalized_garbage_count}
        events.append(
            Slice(
                track,
                f"Finalize Garbage({gen})",
                f"gc.finalize(gen={gen})",
                item.ts_handle_weakref_callbacks_stop,
                item.ts_finalize_garbage_stop,
                inc_data,
            )
        )

    if has_handle_resurrected(item) and item.ts_handle_resurrected_stop - item.ts_finalize_garbage_stop > 0:
        inc_data = {"generation": gen, "iid": iid}
        events.append(
            Slice(
                track,
                f"Handle Resurrected({gen})",
                f"gc.resurrect(gen={gen})",
                item.ts_finalize_garbage_stop,
                item.ts_handle_resurrected_stop,
                inc_data,
            )
        )

    if has_clear_weakrefs(item) and item.ts_clear_weakrefs_stop - item.ts_handle_resurrected_stop > 0:
        inc_data = {"generation": gen, "iid": iid, "clear_weakrefs_count": item.clear_weakrefs_count}
        events.append(
            Slice(
                track,
                f"Clear Weakrefs({gen})",
                f"gc.clear_weakrefs(gen={gen})",
                item.ts_handle_resurrected_stop,
                item.ts_clear_weakrefs_stop,
                inc_data,
            )
        )

    if has_delete_garbage(item) and item.ts_delete_garbage_stop - item.ts_delete_garbage_start > 0:
        inc_data = {"generation": gen, "iid": iid, "deleted_garbage_count": item.deleted_garbage_count}
        events.append(
            Slice(
                track,
                f"Delete Garbage({gen})",
                f"gc.delete(gen={gen})",
                item.ts_delete_garbage_start,
                item.ts_delete_garbage_stop,
                inc_data,
            )
        )

    events.extend(
        Counter(track, metric, f"G{gen} {metric}", ts_start_ns, value) for metric, value in counter_data.items()
    )

    events.append(
        # Qualified unconditionally, interpreter 0 included. gcmon writes a
        # counter descriptor the first time it sees that metric, batch by
        # batch; when interpreter 0's goes out, interpreter 1 may not have
        # produced a record yet, so no rule of the form "qualify only when
        # there is a sibling" is implementable in a streaming writer.
        Counter(
            track,
            "heap_size",
            f"Thread {iid} heap_size",
            ts_start_ns,
            item.heap_size,
        )
    )

    if has_new_incremental(item):
        new_inc_counters = [
            Counter(track, "old_work", f"Thread {iid} old_work", ts_start_ns, item.old_work),
            Counter(track, "survivor_count", f"Thread {iid} survivor_count", ts_start_ns, item.survivor_count),
            Counter(track, "aging_threshold", f"Thread {iid} aging_threshold", ts_start_ns, item.aging_threshold),
            Counter(track, "aging_spaces", f"Thread {iid} aging_spaces", ts_start_ns, item.aging_spaces),
            Counter(track, "aging_next", f"Thread {iid} aging_next", ts_start_ns, item.aging_next),
            Counter(track, "heap_size_stop", f"Thread {iid} heap_size_stop", ts_start_ns, item.heap_size_stop),
        ]
        events.extend(new_inc_counters)
        if gen == 1:
            events.append(
                Counter(
                    track, "new_increment_size", f"Thread {iid} new_increment_size", ts_start_ns, item.increment_size
                )
            )

    return events


_DURATION_UNITS: tuple[tuple[int, str], ...] = (
    (3_600_000_000_000, "h"),
    (60_000_000_000, "m"),
    (1_000_000_000, "s"),
    (1_000_000, "ms"),
    (1_000, "µs"),
    (1, "ns"),
)


def duration_text(ns: int) -> str:
    """*ns* broken into units, the way the Perfetto UI writes a duration.

    ``3316458100`` comes out as ``3s 316ms 458µs 100ns``. Units that
    contribute nothing drop out, and zero is ``0ns``.
    """
    if ns == 0:
        return "0ns"

    sign = "-" if ns < 0 else ""
    rest = abs(ns)
    parts: list[str] = []
    for size, unit in _DURATION_UNITS:
        value, rest = divmod(rest, size)
        if value:
            parts.append(f"{value}{unit}")

    return sign + " ".join(parts)


def seen_text(observed_count: int, lost_count: int) -> str:
    """The share of an interval's records gcmon read.

    ``87.0% (47 of 54)``. The ``--stats`` table's ``Cov`` spans a whole run;
    this one spans a single poll interval.
    """
    total = observed_count + lost_count
    if total == 0:
        return "100.0% (0 of 0)"
    return f"{100.0 * observed_count / total:.1f}% ({observed_count} of {total})"


def lost_collections(lost_from: int, lost_count: int) -> str:
    """The records an interval lost, as one string.

    ``"11"`` for a single record, ``"2..383"`` for a range, both ends
    included either way.
    """
    if lost_count == 1:
        return str(lost_from)
    return f"{lost_from}..{lost_from + lost_count - 1}"


def _gen_loss_args(gen: TGenLoss) -> ArgGroup:
    """One generation's group inside a ``GC Loss`` slice's args.

    A generation that lost nothing gets ``observed_count`` and a zero, so the
    groups still add up to the slice's totals. One that lost something also
    gets ``lost_collections``, naming them on that generation's own counter
    with both ends included, and the pause they came to.
    """
    args: ArgGroup = {"observed_count": gen.observed_count}
    if not gen.lost_count:
        args["lost_count"] = 0
        return args

    args["lost_collections"] = lost_collections(gen.lost_from, gen.lost_count)
    args["lost_count"] = gen.lost_count
    args["lost_pause"] = duration_text(gen.lost_pause_ns)
    args["lost_pause_ns"] = gen.lost_pause_ns
    return args


def convert_loss_to_trace_format(process: Process, item: TLossMsg) -> list[TraceEvent]:
    """One ``GC Loss`` slice covering a poll interval gcmon went into blind.

    Spans the interval end to end, on interpreter *iid*'s loss track rather
    than among its collections. Named ``GC Loss(0,2)`` for the generations that
    lost records, which also gives each combination a colour of its own since
    Perfetto hashes the slice name.

    The args carry the interval's totals and then one group per generation.
    ``lost_pause_ns`` sums the lost collections and is not the slice's
    duration: a 29 s bar can carry 3 s of it. It goes out twice, in nanoseconds
    for SQL and as ``lost_pause`` for reading.

    See ADR-0015 for the width, the track and the grouping.
    """
    track = LossTrack(process, item.iid)
    blind = [gen.gen for gen in item.gens if gen.lost_count]
    name = f"GC Loss({','.join(str(gen) for gen in blind)})" if blind else "GC Loss"
    category = "gc.loss"

    observed_count = sum(gen.observed_count for gen in item.gens)
    lost_count = sum(gen.lost_count for gen in item.gens)
    lost_pause_ns = sum(gen.lost_pause_ns for gen in item.gens)

    args: EventArgs = {
        "iid": item.iid,
        "observed_count": observed_count,
        "lost_count": lost_count,
        "seen": seen_text(observed_count, lost_count),
        "lost_pause": duration_text(lost_pause_ns),
        "lost_pause_ns": lost_pause_ns,
    }
    for gen in item.gens:
        args[f"gen{gen.gen}"] = _gen_loss_args(gen)

    return [Slice(track, name, category, item.ts_start, item.ts_stop, args)]


def _loss_in_time_order(items: Sequence[TItem]) -> Sequence[TItem]:
    """*items* with its loss records in time order, everything else in place.

    Load-bearing rather than tidy. The encoder expands a `Slice` into its
    BEGIN/END pair in list order, and consecutive intervals touch, so one
    span's END shares a timestamp with the next one's BEGIN. A trace
    processor leaves two events sharing a timestamp in the order they were
    emitted, and the wrong way round they read as nested rather than as a
    sequence. `_ingest` emits them in order, but a capture read back from
    JSONL carries that only in its line order.
    """
    spans = [item for item in items if is_loss(item)]
    if len(spans) < 2:
        return items

    at = [index for index, item in enumerate(items) if is_loss(item)]
    ordered = list(items)
    for index, span in zip(at, sorted(spans, key=lambda span: span.ts_start), strict=True):
        ordered[index] = span

    return ordered


def convert_to_trace_format(items: Mapping[int, Sequence[TItem]]) -> list[TraceEvent]:
    """Convert a capture, which names pids and nothing else.

    A capture carries no exit and no discovery instant, so every pid reads as
    a single process on its first epoch. A live trace separates two processes
    that shared a pid where this merges them; ADR-0024 accepts that.
    """
    events: list[TraceEvent] = []
    for pid, pid_items in items.items():
        process = Process(pid, 1)
        pid_events: list[TraceEvent] = []
        # The guards are mutually exclusive, so the order is free to follow the
        # capture: GC records outnumber the other two by orders of magnitude,
        # and a guard that misses pays for the `AttributeError` behind
        # `hasattr`.
        for item in _loss_in_time_order(pid_items):
            if is_gc_stats(item):
                pid_events.extend(convert_item_to_trace_format(process, item))
            elif is_loss(item):
                pid_events.extend(convert_loss_to_trace_format(process, item))
            elif is_instant(item):
                pid_events.append(Instant(ProcessTrack(process), item.name, item.ts))

        events.extend(pid_events)

    return events
