"""Environment variable helpers for the monitor commands' defaults."""

import math
import os
from pathlib import Path

from gcmon.model.schedule import MIN_RATE_NS
from gcmon.stats.views import TableFormat
from gcmon.support.time_units import secs_to_ns

# Environment variable names for CLI options
ENV_PREFIX = "GCMON"
ENV_OUTPUT = f"{ENV_PREFIX}_OUTPUT"
ENV_RATE = f"{ENV_PREFIX}_RATE"
ENV_DURATION = f"{ENV_PREFIX}_DURATION"
ENV_VERBOSE = f"{ENV_PREFIX}_VERBOSE"
ENV_FORMAT = f"{ENV_PREFIX}_FORMAT"
ENV_THREAD_ID = f"{ENV_PREFIX}_THREAD_ID"
ENV_FLUSH_THRESHOLD = f"{ENV_PREFIX}_FLUSH_THRESHOLD"
ENV_SERVER_HOST = f"{ENV_PREFIX}_SERVER_HOST"
ENV_SERVER_PORT = f"{ENV_PREFIX}_SERVER_PORT"
ENV_STATS = f"{ENV_PREFIX}_STATS"
ENV_TABLE_FORMAT = f"{ENV_PREFIX}_TABLE_FORMAT"
ENV_CONTROL_NAME = f"{ENV_PREFIX}_CONTROL_NAME"
ENV_RSS = f"{ENV_PREFIX}_RSS"
ENV_RSS_INTERVAL = f"{ENV_PREFIX}_RSS_INTERVAL"

__all__ = [
    "ENV_CONTROL_NAME",
    "ENV_DURATION",
    "ENV_FLUSH_THRESHOLD",
    "ENV_FORMAT",
    "ENV_OUTPUT",
    "ENV_PREFIX",
    "ENV_RATE",
    "ENV_RSS",
    "ENV_RSS_INTERVAL",
    "ENV_SERVER_HOST",
    "ENV_SERVER_PORT",
    "ENV_STATS",
    "ENV_TABLE_FORMAT",
    "ENV_THREAD_ID",
    "ENV_VERBOSE",
    "get_env_control_name",
    "get_env_duration",
    "get_env_flush_threshold",
    "get_env_format",
    "get_env_output",
    "get_env_rate",
    "get_env_rss",
    "get_env_rss_interval",
    "get_env_server_host",
    "get_env_server_port",
    "get_env_stats",
    "get_env_table_format",
    "get_env_thread_id",
    "get_env_verbose",
    "parse_rate",
]


def get_env_output() -> Path:
    """Get output path from environment variable.

    Returns:
        Path from GCMON_OUTPUT env var, or default Path("gcmon.pftrace").
    """
    output_str = os.environ.get(ENV_OUTPUT)
    if output_str:
        return Path(output_str)
    if get_env_format() == "jsonl":
        return Path("gcmon.jsonl")
    return Path("gcmon.pftrace")


def parse_rate(text: str) -> float:
    """One `--rate` or GCMON_RATE spelling, as seconds.

    A plain decimal only: scientific notation hides how small a value is (ADR-0019).

    Raises:
        ValueError: on any spelling that is not a rate gcmon can hold.
    """
    if "e" in text.lower():
        raise ValueError(f"must be a plain decimal number of seconds, not scientific notation, got '{text}'")

    value = float(text)
    if not math.isfinite(value) or secs_to_ns(value) < MIN_RATE_NS:
        raise ValueError(f"must be at least {MIN_RATE_NS / 1e9} seconds, got '{text}'")

    return value


def get_env_rate() -> float | None:
    """Get polling rate from environment variable.

    Returns:
        Rate from GCMON_RATE env var, default 0.1 when it is unset, or None
        when it holds something that is not a rate, which stops the run.
    """
    rate_str = os.environ.get(ENV_RATE)
    if rate_str:
        try:
            return parse_rate(rate_str)
        except ValueError:
            return None
    return 0.1


def get_env_duration() -> float | None:
    """Get monitoring duration from environment variable.

    Returns:
        Duration from GCMON_DURATION env var, or None (run until interrupted).
    """
    duration_str = os.environ.get(ENV_DURATION)
    if duration_str:
        try:
            return float(duration_str)
        except ValueError:
            pass
    return None


def get_env_verbose() -> int:
    """Get verbose count from environment variable.

    Returns:
        Verbose count: 0 for no verbose, 1 for INFO, 2+ for DEBUG.
        GCMON_VERBOSE can be set to a number (e.g., "2") or
        truthy value ("1", "true", "yes", "on" -> 1).
    """
    verbose_str = os.environ.get(ENV_VERBOSE, "").lower()
    if not verbose_str:
        return 0
    # Try to parse as integer first
    try:
        return int(verbose_str)
    except ValueError:
        pass
    # Fall back to boolean interpretation
    if verbose_str in ("1", "true", "yes", "on"):
        return 1
    return 0


def get_env_format() -> str:
    """Get the output format from the environment.

    The value is handed on as written. ``get_monitoring_options`` refuses a word
    ``--format`` would refuse, once logging is configured (ADR-0018). A blank
    value reads as unset.

    Returns:
        The GCMON_FORMAT value in lower case, or "perfetto" if it is unset or
        blank.
    """
    format_str = os.environ.get(ENV_FORMAT)
    if format_str and format_str.strip():
        return format_str.strip().lower()
    return "perfetto"


def get_env_thread_id() -> int:
    """Get thread ID from environment variable.

    Returns:
        Thread ID from GCMON_THREAD_ID env var, or default 0.
    """
    thread_id_str = os.environ.get(ENV_THREAD_ID)
    if thread_id_str:
        try:
            return int(thread_id_str)
        except ValueError:
            pass
    return 0


def get_env_flush_threshold() -> int:
    """Get flush threshold from environment variable.

    Returns:
        Flush threshold from GCMON_FLUSH_THRESHOLD env var, or default 100.
    """
    threshold_str = os.environ.get(ENV_FLUSH_THRESHOLD)
    if threshold_str:
        try:
            return int(threshold_str)
        except ValueError:
            pass
    return 100


def get_env_server_host() -> str:
    """Get server host from environment variable.

    Returns:
        Host from GCMON_SERVER_HOST env var, or default "localhost".
    """
    host_str = os.environ.get(ENV_SERVER_HOST)
    if host_str:
        return host_str
    return "localhost"


def get_env_server_port() -> int:
    """Get server port from environment variable.

    Returns:
        Port from GCMON_SERVER_PORT env var, or default 9999.
    """
    port_str = os.environ.get(ENV_SERVER_PORT)
    if port_str:
        try:
            return int(port_str)
        except ValueError:
            pass
    return 9999


def get_env_stats() -> str | None:
    """Get the statistics view, or a word asking for no table, from the environment.

    The value is handed on as written. ``get_monitoring_options`` refuses an
    unknown one, once logging is configured. A blank value reads as unset.

    Returns:
        The raw GCMON_STATS value, or None if it is unset or blank.
    """
    value = os.environ.get(ENV_STATS)
    return value if value and value.strip() else None


def get_env_rss() -> bool:
    """Get RSS tracking flag from environment variable.

    Returns:
        True if GCMON_RSS is set to a truthy value ("1", "true", "yes", "on").
    """
    rss_str = os.environ.get(ENV_RSS, "").lower()
    if not rss_str:
        return False
    return rss_str in ("1", "true", "yes", "on")


def get_env_rss_interval() -> float:
    """Get RSS sampling interval from environment variable.

    Returns:
        Interval from GCMON_RSS_INTERVAL env var, or default 1.0.
    """
    interval_str = os.environ.get(ENV_RSS_INTERVAL)
    if interval_str:
        try:
            return float(interval_str)
        except ValueError:
            pass
    return 1.0


def get_env_control_name() -> str | None:
    """Get control plane name from environment variable.

    Returns:
        Name from GCMON_CONTROL_NAME env var, or None.
    """
    return os.environ.get(ENV_CONTROL_NAME) or None


def get_env_table_format() -> TableFormat:
    """Get table format from environment variable.

    Returns:
        TableFormat from GCMON_TABLE_FORMAT env var, or TableFormat.PLAIN.
    """
    val = os.environ.get(ENV_TABLE_FORMAT)
    if val:
        val = val.lower()
        if val == "md" or val == "markdown":
            return TableFormat.MARKDOWN
    return TableFormat.PLAIN
