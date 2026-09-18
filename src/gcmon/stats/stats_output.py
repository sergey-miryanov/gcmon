"""Statistics output formatting for GC monitoring."""

from pathlib import Path
from typing import Any, Final

from ..model.process import Process
from ..model.run_report import RunReport
from ..support.time_units import dur_to_ms
from .metrics import METRICS, PAUSE_KEY
from .stats import Stats
from .streaming_stats import PauseTotals, StreamingStats
from .views import StatsView, TableFormat

# The two labels a row carries instead of a ring, spelled once so a test
# that reads the table back names the same thing the table prints.
TOTAL_LABEL: Final = "Total"
READ_TIME_LABEL: Final = "Read Time"

_SEP_GROUP: Any = object()
_SEP_PHASE: Any = object()


def _print_table(rows: list[list[str] | Any], table_format: TableFormat = TableFormat.PLAIN) -> None:
    if not rows:
        return

    headers = ["PID:IID", "Metric", "Count", "Sum", "Avg", "P50", "P90", "P95", "P99", "Cov", "F"]
    data = [r for r in rows if r is not _SEP_GROUP and r is not _SEP_PHASE]
    w_type = max(len(h) for h in [headers[0]] + [r[0] for r in data])
    w_metric = max(len(h) for h in [headers[1]] + [r[1] for r in data])
    w_count = max(len(h) for h in [headers[2]] + [r[2] for r in data])
    w_sum = max(len(h) for h in [headers[3]] + [r[3] for r in data])
    w_pct = max(len(v) for h in headers[4:9] for v in [h] + [r[i] for r in data for i in range(4, 9)])
    w_cov = max(len(v) for h in headers[9:] for v in [h] + [r[i] for r in data for i in range(9, 11)])

    widths = [w_type + 2, w_metric + 2, w_count + 2, w_sum + 2, *[w_pct + 2] * 5, *[w_cov + 2] * 2]
    sep_full = "|" + "|".join("-" * w for w in widths) + "|"
    sep_phase = "|" + " " * widths[0] + "|" + "|".join("-" * w for w in widths[1:]) + "|"
    sep_blank = "|" + "|".join(" " * w for w in widths) + "|"
    use_markdown = table_format == TableFormat.MARKDOWN
    print("")
    print(
        f"| {headers[0]:<{w_type}} | {headers[1]:<{w_metric}} | {headers[2]:>{w_count}} | {headers[3]:>{w_sum}} "
        f"| {headers[4]:>{w_pct}} | {headers[5]:>{w_pct}} | {headers[6]:>{w_pct}} "
        f"| {headers[7]:>{w_pct}} | {headers[8]:>{w_pct}} "
        f"| {headers[9]:>{w_cov}} | {headers[10]:>{w_cov}} |"
    )
    print(sep_full)
    for r in rows:
        if r is _SEP_GROUP:
            print(sep_blank if use_markdown else sep_full)
            continue
        if r is _SEP_PHASE:
            print(sep_blank if use_markdown else sep_phase)
            continue
        print(
            f"| {r[0]:<{w_type}} | {r[1]:<{w_metric}} | {r[2]:>{w_count}} | {r[3]:>{w_sum}} "
            f"| {r[4]:>{w_pct}} | {r[5]:>{w_pct}} | {r[6]:>{w_pct}} "
            f"| {r[7]:>{w_pct}} | {r[8]:>{w_pct}} "
            f"| {r[9]:>{w_cov}} | {r[10]:>{w_cov}} |"
        )


def _dual(sampled: str, whole: str | None, marker: str = "") -> str:
    """``sampled/whole``, or one value when the two agree.

    Nothing was lost is worth saying once, not twice in every cell.
    """
    if whole is None or whole == sampled:
        return sampled
    return f"{sampled}/{marker}{whole}"


def _format_stats(s: Stats, count: str | None = None, total: str | None = None) -> list[str]:
    """Format a Stats of nanosecond durations as table cells in milliseconds.

    *count* and *total* replace the first two cells when given, already
    carrying their exact or estimated companion. The rest stay sampled:
    percentiles cannot be corrected, and an average of a sampled set is a
    sampled average.
    """
    return [
        count if count is not None else str(s.count()),
        total if total is not None else f"{dur_to_ms(s.sum()):.3f}",
        f"{dur_to_ms(s.average()):.3f}",
        f"{dur_to_ms(s.percentile(50)):.3f}",
        f"{dur_to_ms(s.percentile(90)):.3f}",
        f"{dur_to_ms(s.percentile(95)):.3f}",
        f"{dur_to_ms(s.percentile(99)):.3f}",
    ]


def _build_rows(
    gen_stats: dict[int, Stats],
    label: str,
    pause_totals: dict[int, PauseTotals],
    exact: bool,
) -> list[list[str]]:
    """One row per generation that recorded anything.

    *pause_totals* covers whichever scope the block is for, read once by the
    caller. All nine metrics lean on the same per-generation numbers, so
    looking them up here would repeat the work per metric.

    *exact* separates the `GC Pause` rows, whose companion numbers come from
    the target's own counters, from the sub-phase rows, whose companions are
    the sampled value scaled by ``F`` and are marked ``~`` for it.
    """
    rows = []
    for gen in sorted(gen_stats.keys()):
        s = gen_stats[gen]
        if s.count() == 0:
            continue

        totals = pause_totals.get(gen, PauseTotals())
        coverage = totals.coverage
        factor = totals.scale_factor
        if exact:
            count = str(totals.exact_count)
            total = f"{dur_to_ms(totals.exact_pause_ns):.3f}"
            marker = ""
        else:
            count = str(round(s.count() / coverage))
            total = f"{dur_to_ms(s.sum() * factor):.3f}"
            marker = "~"

        cells = _format_stats(
            s, _dual(str(s.count()), count, marker), _dual(f"{dur_to_ms(s.sum()):.3f}", total, marker)
        )
        lost = totals.lost_count
        rows.append([f"{label}({gen})", *cells, _coverage_cell(coverage, lost), _factor_cell(factor, lost)])
    return rows


def _plural(count: int, noun: str, plural: str | None = None) -> str:
    """``1 interpreter``, ``3 interpreters``."""
    if count == 1:
        return f"{count} {noun}"
    return f"{count} {plural or noun + 's'}"


def _coverage_cell(coverage: float, lost: int) -> str:
    """``Cov`` must never round to a figure the cells beside it contradict.

    A session that lost eight records in 1771 is at 99.5%, which reads as
    100% at two decimals while ``Count`` plainly shows a gap. Where rounding
    would claim completeness that the numbers deny, say so instead.
    """
    cell = f"{coverage:.1%}"
    return "<100.0%" if lost and cell == "100.0%" else cell


def _factor_cell(factor: float, lost: int) -> str:
    cell = f"{factor:.3f}"
    return ">1.000" if lost and cell == "1.000" else cell


def summary_lines(
    stats: StreamingStats,
    trace_path: Path | None,
    show_stats: bool = False,
    pacing: RunReport | None = None,
) -> list[str]:
    """Summary lines for monitoring commands.

    *pacing* is what the loop did with its schedule. It picks the remedy a
    lossy run is offered, which the mid-run coverage warning cannot; see
    ADR-0019.
    """
    sampled = stats.count()
    lost = sum(totals.lost_count for totals in stats.pause_totals_by_gen().values())

    lines = ["Monitoring complete."]
    if lost:
        observed = _coverage_cell(sampled / (sampled + lost), lost)
        lines.append(f"Total events: {sampled} (+{lost} reconstructed, {observed} observed)")
        if not show_stats:
            lines.append("Run with --stats=total for the per-generation breakdown.")
    else:
        lines.append(f"Total events: {sampled}")

    if pacing is not None:
        lines.append(f"Ticks: {pacing.ticks_run} of {pacing.ticks_scheduled} scheduled")
        if lost:
            lines.append(
                "The poll loop overran, so a smaller --rate will not help."
                if pacing.overran
                else "Polling more often (a smaller --rate) may observe more, unless the target "
                "collects faster than gcmon can poll."
            )

    if trace_path is not None:
        lines.append(f"Trace saved to: {trace_path}")
    return lines


def _ring_label(process: Process, iid: int) -> str:
    """The block's heading: `12345:0`, and `12345:0#2` for the second process
    to hold that pid."""
    return f"{process.pid}:{iid}{process.epoch_suffix}"


def print_stats(stats: StreamingStats, view: StatsView, table_format: TableFormat = TableFormat.PLAIN) -> None:
    """Two levels: the run, then one block per ring under `FULL`.

    Rings sort by pid, then epoch, then interpreter. `Read Time` belongs to no
    ring, so its first cell stays empty and it prints under either view.

    *view* chooses which blocks print and nothing else. Widths follow the rows
    that print, so ring labels wider than the `PID:IID` header pad `FULL`'s
    first column one further than `TOTAL`'s.
    """
    all_rows: list[list[str] | Any] = []

    totals = stats.pause_totals_by_gen()
    first = True
    has_rows = False
    for metric_key, metric in METRICS.items():
        rows = _build_rows(stats.metrics[metric_key], metric.name, totals, metric_key == PAUSE_KEY)
        if rows:
            if has_rows:
                all_rows.append(_SEP_PHASE)
            for row in rows:
                all_rows.append([TOTAL_LABEL if first else "", *row])
                first = False
            has_rows = True

    rings = stats.ring_metrics() if view is StatsView.FULL else []
    for (process, iid), ring_data in rings:
        all_rows.append(_SEP_GROUP)
        ring_totals = {gen: stats.pause_totals(process, iid, gen) for gen in stats.GENS}
        first = True
        has_rows = False
        for metric_key, metric in METRICS.items():
            rows = _build_rows(ring_data.get(metric_key, {}), metric.name, ring_totals, metric_key == PAUSE_KEY)
            if rows:
                if has_rows:
                    all_rows.append(_SEP_PHASE)
                for row in rows:
                    # Both parts on every row, `12345:0` on a
                    # single-interpreter run too: dropping the `:0` would
                    # leave a header naming two fields over cells holding one.
                    all_rows.append([_ring_label(process, iid) if first else "", *row])
                    first = False
                has_rows = True

    rt = stats.read_time
    if rt.count() > 0:
        all_rows.append(_SEP_GROUP)
        all_rows.append(["", READ_TIME_LABEL, *_format_stats(rt), "", ""])

    if not all_rows:
        print("No GC statistics collected.")
        return

    _print_table(all_rows, table_format=table_format)
    _print_footer(stats, view)


def _print_footer(stats: StreamingStats, view: StatsView) -> None:
    """What the table's two number kinds mean.

    Only printed when something was lost or the target collected before gcmon
    attached. A run that saw everything has nothing to explain.

    Which notes appear depends on the run, so the number rather than the order
    separates two that wrap on a narrow terminal. A lone note still reads
    ``1.``. The first two are run-wide; the third is `FULL` only.
    """
    pauses = stats.pause_totals_by_gen()
    cumulative = stats.cumulative_totals_by_gen()
    covered = [(gen, pauses[gen]) for gen in stats.GENS if pauses[gen].lost_count]
    counted = [(gen, cumulative[gen]) for gen in stats.GENS if gen in cumulative and cumulative[gen].collections]

    notes: list[str] = []
    if covered:
        parts = ", ".join(f"Gen{gen} {_coverage_cell(t.coverage, t.lost_count)}" for gen, t in covered)
        notes.append(f"Coverage: {parts}. Count and Sum read sampled/exact; percentiles are sampled and read high.")
    if counted:
        parts = ", ".join(
            f"Gen{gen} {totals.collections} in {dur_to_ms(totals.pause_ns):.3f} ms" for gen, totals in counted
        )
        interpreters, processes = stats.cumulative_scope()
        # One line per generation whatever the size of the tree. The counts
        # say what it summed over: interpreters start at different moments,
        # and a reused pid folds two processes into one figure.
        #
        # "monitored window included" covers that window rather than
        # excluding it, so the note must not read as a figure to add to
        # `Count`. It reports the target's own cumulative counter.
        notes.append(
            f"Since each interpreter started, monitored window included, summed over "
            f"{_plural(interpreters, 'interpreter')} in {_plural(processes, 'process', 'processes')}: {parts}."
        )

    # The note reconciles ring rows against the run, and `TOTAL` prints none.
    untracked = stats.untracked_rings() if view is StatsView.FULL else 0
    if untracked:
        # The rows are short of the run and a reader adding them up would find
        # it, so say the count here rather than leave the gap unexplained.
        notes.append(
            f"{_plural(untracked, 'ring')} got no row: gcmon was already tracking "
            f"{StreamingStats.MAX_ACTIVE_RINGS} interpreters at once. Those records are counted in Total."
        )

    if not notes:
        return

    print("")
    for number, note in enumerate(notes, start=1):
        print(f"{number}. {note}")
