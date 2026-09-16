"""RSS (Resident Set Size) sampling for monitored processes."""

import logging
from collections.abc import Callable, Set

from ..exporters.exporter import EventsExporter
from ..model.process import Process
from ..support.vocabulary import PROGRAM_NAME

logger = logging.getLogger(PROGRAM_NAME)

__all__ = ["RssSampler"]


class RssSampler:
    """Samples RSS for live processes at a configurable interval.

    Parameters
    ----------
    exporter
        The exporter to emit RSS samples through.
    interval
        Minimum time (seconds) between RSS sampling rounds.
    rss_provider
        Optional callable returning RSS in bytes for a PID (0 if
        unreachable). When ``None``, defaults to ``_default_rss_sampler``
        (psutil-based) if psutil is available, otherwise RSS tracking
        is silently disabled.
    """

    def __init__(
        self,
        exporter: EventsExporter,
        interval: float = 1.0,
        rss_provider: Callable[[int], int] | None = None,
    ) -> None:
        self._exporter = exporter
        self._interval_ns = round(interval * 1e9)
        self._last_sample_ns = 0
        self._enabled = True

        if rss_provider is not None:
            self._provider = rss_provider
        else:
            try:
                import psutil  # noqa: F401
            except ImportError:
                logger.info("psutil not available; RSS tracking disabled.")
                self._enabled = False
                self._provider = _noop_rss_sampler
            else:
                self._provider = _default_rss_sampler

    def tick(self, now_ns: int, live: Set[Process]) -> None:
        """Sample RSS for every process in *live* if the sampling interval
        has elapsed.

        *now_ns* both paces the round and stamps every sample in it, so one
        round lands on one instant.
        """
        if not self._enabled or not live:
            return
        if now_ns - self._last_sample_ns < self._interval_ns:
            return
        self._last_sample_ns = now_ns
        for process in live:
            self._sample(process, now_ns)

    def _sample(self, process: Process, ts_ns: int) -> None:
        try:
            rss = self._provider(process.pid)
        except Exception as exc:
            logger.debug("Could not sample RSS for PID %s: %s", process.pid, exc)
            return
        if rss:
            self._exporter.add_rss_sample(process, rss, ts_ns)


def _noop_rss_sampler(pid: int) -> int:
    return 0


def _default_rss_sampler(pid: int) -> int:
    import psutil

    try:
        return psutil.Process(pid).memory_info().rss
    except psutil.NoSuchProcess, psutil.AccessDenied:
        return 0
