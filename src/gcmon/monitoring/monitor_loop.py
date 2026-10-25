import threading
import time
from typing import Self

from ..model.process import Process
from ..model.run_report import RunReport
from ..model.schedule import idle_to_next_position, position_of
from ..support import set_on_exit
from ..support.time_units import secs_to_ns
from .monitor import EventsMonitor
from .rss_sampler import RssSampler
from .run_policy import Runner

__all__ = ["MonitorLoop"]


class MonitorLoop:
    """Timing and shutdown around a monitor that owns the rest.

    One tick is one call on the monitor, which answers who was alive and
    whether any wait policy still wants the run to go on. Left here: the clock,
    the stop event a signal handler sets, the rate and the RSS sampler.

    Tick starts land on a schedule, `t0 + k * rate` for whole `k`, whatever a
    tick costs (ADR-0019).
    """

    def __init__(
        self,
        monitor: EventsMonitor,
        runner: Runner,
        rate: float = 0.1,
        rss_sampler: RssSampler | None = None,
    ) -> None:
        self._monitor = monitor
        self._runner = runner
        self._rate_ns = secs_to_ns(rate)
        assert self._rate_ns > 0, "a schedule needs a rate"
        self._stop_event = threading.Event()
        self._rss_sampler = rss_sampler

    def close(self) -> None:
        self._stop_event.set()

    def run(self) -> RunReport:
        start_ns = time.monotonic_ns()
        # How far the run got, whichever way it ended: a stop mid-tick leaves a
        # tick start here, everything else a tick end.
        last_ns = start_ns
        ticks_run = 0
        live: frozenset[Process] = frozenset()

        with set_on_exit(self._stop_event):
            for _ in self._runner.run(self._stop_event.is_set):
                last_ns = time.monotonic_ns()
                ticks_run += 1

                # Ahead of the poll, so the pauses it reads carry this tick's
                # sample (ADR-0013).
                if self._rss_sampler:
                    self._rss_sampler.tick(last_ns, live)

                report = self._monitor.tick(last_ns, self._stop_event.is_set)
                live = report.live

                if not report.keep_running:
                    break

                last_ns = time.monotonic_ns()
                self._stop_event.wait(timeout=idle_to_next_position(last_ns, start_ns, self._rate_ns) / 1e9)

        positions = position_of(last_ns, start_ns, self._rate_ns) + 1 if ticks_run else 0

        return RunReport(ticks_run=ticks_run, ticks_scheduled=positions)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
