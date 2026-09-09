"""The options `monitor` and `run` share."""

import argparse
import logging
import os
from pathlib import Path

from gcmon.cli.monitor._env import (
    ENV_CONTROL_NAME,
    ENV_DURATION,
    ENV_FLUSH_THRESHOLD,
    ENV_FORMAT,
    ENV_OUTPUT,
    ENV_RATE,
    ENV_RSS,
    ENV_RSS_INTERVAL,
    ENV_STATS,
    ENV_TABLE_FORMAT,
    ENV_VERBOSE,
    get_env_control_name,
    get_env_duration,
    get_env_flush_threshold,
    get_env_format,
    get_env_output,
    get_env_rate,
    get_env_rss,
    get_env_rss_interval,
    get_env_stats,
    get_env_table_format,
    get_env_verbose,
    parse_rate,
)
from gcmon.model.schedule import MIN_RATE_NS
from gcmon.stats.views import STATS_OFF_WORDS, StatsView, TableFormat
from gcmon.support.time_units import secs_to_ns

logger = logging.getLogger("gcmon")

# Every format `--format` takes, in the order the help lists them. The parser
# and the GCMON_FORMAT refusal below read the same tuple, so a word one accepts
# is a word the other accepts.
FORMATS = ("perfetto", "jsonl", "stdout")

# Formats whose exporters implement EventsExporter.add_rss_sample. The others
# inherit the no-op base implementation and silently discard RSS samples.
RSS_CAPABLE_FORMATS = ("perfetto",)


def _rate_argument(text: str) -> float:
    """`--rate`, reported by argparse rather than by the validation below."""
    try:
        return parse_rate(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _normalize_table_format(val: str) -> TableFormat:
    val = val.lower()
    if val == "md" or val == "markdown":
        return TableFormat.MARKDOWN
    if val == "plain":
        return TableFormat.PLAIN
    raise argparse.ArgumentTypeError(f"expected 'plain', 'markdown', or 'md', got '{val}'")


def add_monitoring_options(parser: argparse.ArgumentParser) -> None:
    """Add common monitoring options to a command parser."""
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=get_env_output(),
        help=f"Output file path (default: gcmon.pftrace, gcmon.jsonl for jsonl format, or {ENV_OUTPUT} env var). Ignored for --format stdout",
    )
    parser.add_argument(
        "-r",
        "--rate",
        type=_rate_argument,
        default=get_env_rate(),
        help=f"Seconds between poll starts (default: 0.1 or {ENV_RATE} env var)",
    )
    parser.add_argument(
        "-d",
        "--duration",
        type=float,
        default=get_env_duration(),
        help=f"Monitoring duration in seconds (default: run until interrupted or {ENV_DURATION} env var)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=get_env_verbose(),
        help=f"Enable verbose output (use -v for INFO, -vv for DEBUG, or set via {ENV_VERBOSE} env var: 1, 2, true, yes, on)",
    )
    parser.add_argument(
        "--format",
        choices=FORMATS,
        default=get_env_format(),
        help=(
            f"Output format: 'perfetto' for Perfetto binary protobuf, 'jsonl' for JSONL file, "
            f"'stdout' for one-line-per-event JSONL to stdout "
            f"(default: perfetto or {ENV_FORMAT} env var)"
        ),
    )
    parser.add_argument(
        "--flush-threshold",
        type=int,
        default=get_env_flush_threshold(),
        help=f"Number of events to buffer before flushing to file for JSONL format (default: 100 or {ENV_FLUSH_THRESHOLD} env var)",
    )
    # Not `nargs="?"` with a `const`: that eats the pid of
    # `gcmon monitor --stats 12345`. See ADR-0018.
    parser.add_argument(
        "--stats",
        choices=StatsView.words(),
        default=get_env_stats(),
        help=(
            f"Show a statistics table at the end of monitoring: 'total' for the run-wide block, "
            f"'full' for that plus one block per interpreter, 'no'/'off'/'false'/'0' for no "
            f"table. {ENV_STATS} takes the same words. High-accuracy percentiles need the stats "
            f"extra: pip install gcmon[stats]"
        ),
    )
    parser.add_argument(
        "--table-format",
        type=_normalize_table_format,
        default=get_env_table_format(),
        help=f"Table format: 'plain' for standard dashes, 'markdown' or 'md' for blank separators (default: plain or {ENV_TABLE_FORMAT} env var)",
    )
    parser.add_argument(
        "--control-name",
        default=get_env_control_name(),
        help=f"Control plane name. Full address: gcmon-<name> (default: auto, or {ENV_CONTROL_NAME} env var)",
    )
    parser.add_argument(
        "--rss",
        action="store_true",
        default=get_env_rss(),
        help=(
            f"Track RSS (Resident Set Size) of monitored process (supported for --format perfetto; "
            f"requires psutil; or {ENV_RSS}=1 env var)"
        ),
    )
    parser.add_argument(
        "--rss-interval",
        type=float,
        default=get_env_rss_interval(),
        help=f"RSS sampling interval in seconds (default: 1.0 or {ENV_RSS_INTERVAL} env var)",
    )


class MonitoringOptions:
    """Monitoring configuration."""

    def __init__(
        self,
        output_path: Path,
        rate: float,
        duration: float | None,
        output_format: str,
        flush_threshold: int,
        duration_label: str,
        stats_view: StatsView | None = None,
        table_format: TableFormat = TableFormat.PLAIN,
        rss_enabled: bool = False,
        rss_interval: float = 1.0,
    ) -> None:
        self.output_path = output_path
        self.rate = rate
        self.duration = duration
        self.output_format = output_format
        self.flush_threshold = flush_threshold
        self.duration_label = duration_label
        # `None` is no table.
        self.stats_view = stats_view
        self.table_format = table_format
        self.rss_enabled = rss_enabled
        self.rss_interval = rss_interval


def get_monitoring_options(
    args: argparse.Namespace,
    duration_label: str = "until interrupted",
) -> MonitoringOptions | None:
    """Extract and validate monitoring options from parsed args.

    Returns None if validation fails.
    """
    output_path: Path = args.output
    rate = args.rate
    if rate is None:
        logger.error("%s must be a rate, got '%s'", ENV_RATE, os.environ.get(ENV_RATE, ""))
        return None
    duration = args.duration
    output_format = args.format
    # argparse takes a string default as given rather than checking it against
    # `choices`, so the environment is the one way an unknown word reaches here
    # (ADR-0018).
    if output_format not in FORMATS:
        logger.error(
            "%s must be one of %s, got '%s'",
            ENV_FORMAT,
            ", ".join(f"'{name}'" for name in FORMATS),
            os.environ.get(ENV_FORMAT, ""),
        )
        return None
    flush_threshold = args.flush_threshold
    table_format = args.table_format
    rss_enabled = args.rss
    rss_interval = args.rss_interval

    if output_format != "stdout":
        logger.info("Output: %s", output_path)
    logger.info("Format: %s", output_format)
    logger.info("Rate: %ss", rate)
    if duration is not None:
        logger.info("Duration: %ss", duration)
    else:
        logger.info("Duration: %s", duration_label)
    if rss_enabled:
        logger.info("RSS tracking: enabled (interval: %ss)", rss_interval)
        if output_format not in RSS_CAPABLE_FORMATS:
            logger.warning(
                "RSS tracking is not supported for --format %s; RSS samples will be discarded.",
                output_format,
            )
        if rss_interval < rate:
            logger.warning(
                "RSS interval (%ss) is shorter than poll rate (%ss); "
                "RSS will be sampled at the poll rate, not the RSS interval.",
                rss_interval,
                rate,
            )

    stats_view: StatsView | None = None
    if args.stats is not None:
        try:
            stats_view = StatsView.parse(args.stats)
        except ValueError:
            logger.error(
                "%s must be 'total', 'full', or one of %s to ask for no table, got '%s'",
                ENV_STATS,
                ", ".join(f"'{off}'" for off in STATS_OFF_WORDS),
                args.stats,
            )
            return None

    if secs_to_ns(rate) < MIN_RATE_NS:
        logger.error("Rate must be at least %s seconds, got %s", MIN_RATE_NS / 1e9, rate)
        return None
    if duration is not None and duration <= 0:
        logger.error("Duration must be positive, got %s", duration)
        return None
    if flush_threshold <= 0:
        logger.error("Flush threshold must be positive, got %s", flush_threshold)
        return None
    if rss_enabled and rss_interval <= 0:
        logger.error("RSS interval must be positive, got %s", rss_interval)
        return None

    if output_format != "stdout":
        resolved = output_path.resolve()
        if not resolved.parent.is_dir():
            logger.error("Output directory does not exist: %s", resolved.parent)
            return None

    return MonitoringOptions(
        output_path=output_path,
        rate=rate,
        duration=duration,
        output_format=output_format,
        flush_threshold=flush_threshold,
        duration_label=duration_label,
        stats_view=stats_view,
        table_format=table_format,
        rss_enabled=rss_enabled,
        rss_interval=rss_interval,
    )
