"""Pyperf hook that marks where each benchmark ran."""

import logging
import os
import time
from functools import partial
from typing import Any

from ..control.control_client import ControlClient, connect_with_retry
from ..control.control_server import CONTROL_ADDRESS_ENV
from ..model.marks import Side, format_mark
from ..model.names import NAME
from ..support.vocabulary import PROGRAM_NAME

ENV_PYPERF_HOOK_VERBOSE = "GCMON_PYPERF_HOOK_VERBOSE"
ENV_PYPERF_HOOK_CONTROL_TIMEOUT = "GCMON_PYPERF_HOOK_CONTROL_TIMEOUT"

logger = logging.getLogger(PROGRAM_NAME)

_regions = 0
"""Regions counted across the process, not within one hook."""


def _next_region() -> int:
    global _regions
    _regions += 1
    return _regions


NO_MONITOR = (
    f"gcmon: no monitor is listening on {CONTROL_ADDRESS_ENV}. Start one over "
    f"the whole run: `gcmon run -o suite.pftrace -s my_benchmark.py "
    f"--hook=gcmon --inherit-environ={CONTROL_ADDRESS_ENV}`, or -m for a "
    "module. pyperf carries the address through to its workers from there."
)


def _hook_error() -> type[Exception]:
    """The exception pyperf's loader catches to print one line and exit 1.

    ``pyperf.__all__`` does not carry ``HookError``. Reaching into the private
    module is the only way to it, and a move of that module leaves the run
    failing on ``RuntimeError`` instead.
    """
    try:
        from pyperf._hooks import HookError
    except Exception:
        return RuntimeError
    refusal: type[Exception] = HookError
    return refusal


def _get_env_pyperf_hook_verbose() -> bool:
    value = os.environ.get(ENV_PYPERF_HOOK_VERBOSE, "").lower()
    return value in ("1", "yes", "on", "true")


def _get_env_pyperf_hook_control_timeout() -> float:
    value = os.environ.get(ENV_PYPERF_HOOK_CONTROL_TIMEOUT, "")
    if value:
        try:
            return float(value)
        except ValueError:
            pass
    return 10.0


class GCMonitorHook:
    """Pyperf hook that marks the benchmark in a running monitor's trace."""

    def __init__(self) -> None:
        self._marks: list[tuple[int, int, int, int]] = []
        self._phase_regions = 0
        self._enter_ts: int | None = None
        self._control_client = ControlClient(
            connection_factory=partial(
                connect_with_retry,
                timeout=_get_env_pyperf_hook_control_timeout(),
            ),
        )
        # Before a benchmark is running, and outside anything pyperf times.
        if self._control_client._ensure_connected() is None:
            raise _hook_error()(NO_MONITOR)

    def __enter__(self) -> GCMonitorHook:
        """Open a region, immediately before the benchmark runs."""
        self._enter_ts = time.monotonic_ns()
        return self

    def __exit__(self, *args: object) -> None:
        """Close it, immediately after."""
        enter_ts = self._enter_ts
        if enter_ts is None:
            return

        self._enter_ts = None
        self._phase_regions += 1
        self._marks.append((_next_region(), self._phase_regions, enter_ts, time.monotonic_ns()))

    def _send_marks(self, bench_name: str) -> None:
        """Land every region that finished."""
        for region, phase_region, enter_ts, exit_ts in self._marks:
            self._control_client.instant_msg(format_mark(bench_name, region, phase_region, Side.BEGIN), ts=enter_ts)
            self._control_client.instant_msg(format_mark(bench_name, region, phase_region, Side.END), ts=exit_ts)
        self._marks.clear()

    def teardown(self, metadata: dict[str, Any]) -> None:
        """Land the marks.

        Pyperf calls this once it is done with a process, and it is the first
        point at which ``metadata['name']`` names the benchmark.
        """
        self._send_marks(metadata.get(NAME, ""))
        self._control_client.close()


def gcmon_hook() -> GCMonitorHook:
    """The entry point, called by pyperf with no arguments."""
    _setup_logging()
    return GCMonitorHook()


def _setup_logging() -> None:
    """Configure the `gcmon` logger for the pyperf hook entry point."""
    level = logging.DEBUG if _get_env_pyperf_hook_verbose() else logging.WARNING
    logger.setLevel(level)

    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setLevel(level)
        formatter = logging.Formatter("[%(name)s] %(levelname)s: %(message)s")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    else:
        for handler in logger.handlers:  # type: ignore[assignment]
            handler.setLevel(level)
