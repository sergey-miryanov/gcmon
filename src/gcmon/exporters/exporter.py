"""Base exporter interface for GC monitoring data."""

from abc import ABC, abstractmethod
from collections.abc import Set

from ..model.process import Process
from ..model.protocol import TGCStatsInfo, TInstantMsg, TLossMsg

__all__ = ["EventsExporter"]


class EventsExporter(ABC):
    """Base class for exporters that collect GC events and save them."""

    @abstractmethod
    def add_event(self, process: Process, item: TGCStatsInfo) -> None:
        """Add a GC monitoring event."""

    @abstractmethod
    def add_instant_event(self, process: Process, item: TInstantMsg) -> None:
        """Add a GC monitoring event."""

    @abstractmethod
    def close(self) -> None:
        """Close the exporter and write all events to file."""

    def add_rss_sample(self, process: Process, rss_bytes: int, ts_ns: int) -> None:  # noqa: B027
        """Record an RSS sample for *process*. No-op in the base class."""

    def add_loss_event(self, process: Process, item: TLossMsg) -> None:  # noqa: B027
        """Record a poll interval whose GC records never reached gcmon.

        One call per interpreter, whatever went blind in it. No-op in the
        base class.
        """

    def add_process_cmdline(self, process: Process, cmdline: tuple[str, ...] | None) -> None:  # noqa: B027
        """Record what *process* is running. No-op in the base class.

        The monitor sends this once, as it creates the process: a
        `Process` names a process and carries nothing gcmon read about it
        (ADR-0025).
        """

    def add_process_retired(self, process: Process) -> None:  # noqa: B027
        """Record that gcmon has let go of *process*. No-op in the base class.

        The monitor sends this once, when it stops polling a pid: the process
        left the tree, or the wait policy gave up on it. Only the Perfetto
        path acts on it, drawing that process's own row without waiting for
        the end of the run; see ADR-0028.
        """

    def add_process_liveness(self, processes: Set[Process], ts_ns: int) -> None:  # noqa: B027
        """Record that gcmon read GC state out of every process in
        *processes* at *ts_ns*. No-op in the base class.

        One call per monitor tick carries the whole live set. Only the
        Perfetto path acts on it, widening each process's
        ``Processes``-track span; see ADR-0029.
        """
