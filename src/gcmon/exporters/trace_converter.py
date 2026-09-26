"""Shared conversion from GC stats items to TraceEvent objects."""

from collections.abc import Mapping, Sequence

from ..model.names import (
    GC_LOSS_CATEGORY,
    GENERATION,
    IID,
    LOST_COUNT,
    LOST_PAUSE,
    LOST_PAUSE_NS,
    OBSERVED_COUNT,
    gc_loss_slice_name,
)
from ..model.phases import PAUSE_ROW, sub_phase_rows
from ..model.process import Process
from ..model.protocol import (
    TGCStatsInfo,
    TGenLoss,
    TItem,
    TLossMsg,
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
    "convert_item_to_trace_format",
    "convert_loss_to_trace_format",
    "convert_to_trace_format",
]


def convert_item_to_trace_format(process: Process, item: TGCStatsInfo) -> list[TraceEvent]:
    gen = item.gen
    iid = item.iid
    track = InterpreterTrack(process, iid)
    ts_start_ns = item.ts_start
    ts_stop_ns = item.ts_stop

    pause_data = PAUSE_ROW.args(gen, item)

    events: list[TraceEvent] = []
    # Ahead of the sub-phases nested inside it, so its BEGIN wins the tie
    # against a sub-phase starting where the pause does. The slice holds
    # `pause_data` itself, so a sub-phase adding to it below still reaches it.
    events.append(
        Slice(
            track,
            PAUSE_ROW.phase.slice_names[gen],
            PAUSE_ROW.phase.categories[gen],
            ts_start_ns,
            ts_stop_ns,
            pause_data,
        )
    )

    # Every sub-phase runs inside the pause, so a pause of no length holds none.
    if ts_stop_ns > ts_start_ns:
        for row in sub_phase_rows(item):
            ts_start, ts_stop = row.bounds(item)
            if ts_stop > ts_start:
                args = row.args(gen, item)
                pause_data.update(args)
                events.append(
                    Slice(
                        track,
                        row.phase.slice_names[gen],
                        row.phase.categories[gen],
                        ts_start,
                        ts_stop,
                        {GENERATION: gen, IID: iid, **args},
                    )
                )

    events.extend(
        Counter(track, metric, name, ts_start_ns, value) for metric, name, value in PAUSE_ROW.counters(gen, item)
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
    args: ArgGroup = {OBSERVED_COUNT: gen.observed_count}
    if not gen.lost_count:
        args[LOST_COUNT] = 0
        return args

    args["lost_collections"] = lost_collections(gen.lost_from, gen.lost_count)
    args[LOST_COUNT] = gen.lost_count
    args[LOST_PAUSE] = duration_text(gen.lost_pause_ns)
    args[LOST_PAUSE_NS] = gen.lost_pause_ns
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
    name = gc_loss_slice_name(blind)
    category = GC_LOSS_CATEGORY

    observed_count = sum(gen.observed_count for gen in item.gens)
    lost_count = sum(gen.lost_count for gen in item.gens)
    lost_pause_ns = sum(gen.lost_pause_ns for gen in item.gens)

    args: EventArgs = {
        IID: item.iid,
        OBSERVED_COUNT: observed_count,
        LOST_COUNT: lost_count,
        "seen": seen_text(observed_count, lost_count),
        LOST_PAUSE: duration_text(lost_pause_ns),
        LOST_PAUSE_NS: lost_pause_ns,
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
            else:
                raise NotImplementedError(f"Unknown item type: {type(item)}")

        events.extend(pid_events)

    return events
