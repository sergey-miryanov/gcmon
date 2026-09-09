"""Shared fixtures for monitoring command tests."""

from __future__ import annotations

import subprocess
import sys
from argparse import Namespace
from collections.abc import Callable, Generator, Mapping
from contextlib import ExitStack
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import MagicMock, patch

import pytest

from gcmon.cli.monitor.monitoring_options import MonitoringOptions
from gcmon.stats.streaming_stats import PauseTotals
from gcmon.stats.views import TableFormat
from tests.helpers import DefaultsValue


class MonitorArgsFactory:
    """Factory for creating monitor command Namespace objects."""

    _defaults: ClassVar[dict[str, DefaultsValue]] = {
        "pid": 12345,
        "output": Path("test.pftrace"),
        "rate": 0.1,
        "duration": 0.05,
        "verbose": 1,
        "format": "perfetto",
        "thread_id": 0,
        "flush_threshold": 100,
        "stats": None,
        "table_format": None,
        "control_name": None,
        "rss": False,
        "rss_interval": 1.0,
    }

    def __call__(self, **overrides: object) -> Namespace:
        kwargs = {**self._defaults, **overrides}
        return Namespace(**kwargs)


@pytest.fixture
def monitor_args() -> MonitorArgsFactory:
    """Factory fixture for creating monitor command Namespace objects.

    Usage:
        args = monitor_args(pid=999, format="jsonl")
    """
    return MonitorArgsFactory()


@pytest.fixture
def mock_monitor() -> MagicMock:
    """Pre-configured MagicMock standing in for a constructed EventsMonitor."""
    return MagicMock()


@pytest.fixture
def mock_exporter() -> MagicMock:
    """Pre-configured MagicMock for exporter."""
    return MagicMock()


@pytest.fixture
def mock_thread() -> MagicMock:
    """Pre-configured MagicMock for monitor thread."""
    thread = MagicMock()
    thread.is_running = True
    return thread


@pytest.fixture
def gcmon_cmd() -> list[str]:
    return [sys.executable, "-m", "gcmon"]


@pytest.fixture
def run_monitor(gcmon_cmd: list[str]) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Run gcmon monitor subprocess with common defaults.

    Usage:
        result = run_monitor(["-d", "0.1"], timeout=5)
    """

    def _run(
        extra_args: list[str] | None = None,
        *,
        timeout: float | None = None,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        cmd = gcmon_cmd + ["monitor", "12345"] + (extra_args or [])
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd, env=env)

    return _run


@pytest.fixture
def run_monitor_self(gcmon_cmd: list[str]) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Run gcmon monitor subprocess with common defaults.

    Usage:
        result = run_monitor(["-d", "0.1"], timeout=5)
    """

    def _run(
        extra_args: list[str] | None = None,
        *,
        timeout: float | None = None,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        cmd = gcmon_cmd + ["monitor", "-1"] + (extra_args or [])
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd, env=env)

    return _run


@pytest.fixture
def monitoring_options() -> Callable[..., MonitoringOptions]:
    """Factory fixture for MonitoringOptions objects.

    Usage:
        opts = monitoring_options(output_format="stdout")
    """

    def _make(**overrides: Any) -> MonitoringOptions:
        defaults: dict[str, Any] = {
            "output_path": Path("test.pftrace"),
            "rate": 0.1,
            "duration": 0.05,
            "output_format": "perfetto",
            "flush_threshold": 100,
            "duration_label": "until interrupted",
            "stats_view": None,
            "table_format": TableFormat.PLAIN,
            "rss_enabled": False,
            "rss_interval": 1.0,
        }
        return MonitoringOptions(**{**defaults, **overrides})

    return _make


@pytest.fixture
def mock_loop_runner_deps() -> Generator[dict[str, MagicMock]]:
    """Patch all dependencies for run_monitoring_loop.

    Returns dict of mocks that tests can customize (e.g. deps["StreamingStats"].return_value.count.return_value = 0).
    """
    patch_targets = [
        "ControlServer",
        "RunnerFactory",
        "EventsExporterFactory",
        "StreamingStats",
        # The command path constructs the monitor itself now, so intercepting
        # construction means patching the class where it is looked up.
        "EventsMonitor",
        "MonitorLoop",
        "replace_signals",
        "print_stats",
    ]
    with ExitStack() as stack:
        deps: dict[str, Any] = {}
        for name in patch_targets:
            deps[name] = stack.enter_context(patch(f"gcmon.cli.monitor.loop_runner.{name}"))
        mock_control_instance = MagicMock()
        mock_control_instance.address = "/tmp/test-address"
        deps["ControlServer"].return_value = mock_control_instance
        deps["StreamingStats"].return_value.count.return_value = 5
        # A lossless run. Tests about a lossy one set their own totals.
        lossless: dict[int, PauseTotals] = {}
        deps["StreamingStats"].return_value.pause_totals_by_gen.return_value = lossless
        deps["MonitorLoop"].return_value = MagicMock()
        deps["RunnerFactory"].return_value = MagicMock()
        yield deps
