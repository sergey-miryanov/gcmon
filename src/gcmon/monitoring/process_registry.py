"""Which process holds a pid now, and which ones held it before."""

import logging
import threading
from collections.abc import Callable, Set

from ..model.process import Process
from ..support.vocabulary import PROGRAM_NAME

__all__ = ["CmdlineProvider", "CmdlineSink", "ProcessRegistry", "read_cmdline"]

logger = logging.getLogger(PROGRAM_NAME)

type CmdlineProvider = Callable[[int], tuple[str, ...] | None]
type CmdlineSink = Callable[[Process, tuple[str, ...] | None], None]


def read_cmdline(pid: int) -> tuple[str, ...] | None:
    """What *pid* is running, off psutil.

    `ProcessRegistry` takes it as a provider and defaults to none (ADR-0025).
    """
    import psutil

    return tuple(psutil.Process(pid).cmdline())


class ProcessRegistry:
    """The monitor's record of who holds each pid (ADR-0025).

    The monitor writes and the control server reads from its own thread,
    so every access takes the lock.
    """

    def __init__(self, cmdline_provider: CmdlineProvider | None = None) -> None:
        self._lock = threading.Lock()
        self._live: dict[int, Process] = {}
        # Per pid, every process that has left and when, oldest first.
        self._retired: dict[int, list[tuple[Process, int]]] = {}
        self._cmdline_provider = cmdline_provider

    def create(self, pid: int, publish: CmdlineSink | None = None) -> Process:
        """Create the process now holding *pid*, and read what it is running.

        The monitor calls this and nothing else does, and it is the one
        place a command line enters gcmon (ADR-0025). The registry keeps
        the read no longer than the call: *publish* is handed the process
        and the command line, and nothing else can ask for it afterwards.

        *publish* runs under the lock, before any other thread can resolve
        the process through :meth:`at`. A message naming the pid that
        arrives in that window waits, and reaches an exporter that already
        has the command line. Two rules follow: *publish* must not call
        back into the registry, and nothing holding a lock *publish* takes
        may go on to take this one.
        """
        cmdline = self._read_cmdline(pid)
        with self._lock:
            assert pid not in self._live, f"PID {pid} is held by {self._live[pid]}; retire it before creating another"
            process = Process(pid, len(self._retired.get(pid, ())) + 1)
            self._live[pid] = process
            if publish is not None:
                publish(process, cmdline)
            return process

    def _read_cmdline(self, pid: int) -> tuple[str, ...] | None:
        """Read *pid*'s command line, outside the lock (ADR-0025)."""
        if self._cmdline_provider is None:
            return None
        try:
            return self._cmdline_provider(pid)
        except Exception as exc:
            logger.warning("Could not collect cmdline for PID %s: %s", pid, exc)
            return None

    def retire(self, pid: int, ts: int) -> None:
        """Note that the process holding *pid* left at *ts*.

        A pid holding no process is not an error.
        """
        with self._lock:
            self._retire_locked(pid, ts)

    def retain(self, pids: Set[int], ts: int) -> None:
        """Retire every live process whose pid is outside *pids*."""
        with self._lock:
            for pid in self._live.keys() - pids:
                self._retire_locked(pid, ts)

    def _retire_locked(self, pid: int, ts: int) -> None:
        process = self._live.pop(pid, None)
        if process is not None:
            self._retired.setdefault(pid, []).append((process, ts))

    def live(self) -> frozenset[Process]:
        """Every process gcmon has not seen leave."""
        with self._lock:
            return frozenset(self._live.values())

    def current(self, pid: int) -> Process | None:
        """The process holding *pid* now, or ``None`` where none does."""
        with self._lock:
            return self._live.get(pid)

    def at(self, pid: int, ts: int) -> Process | None:
        """The process that held *pid* at *ts*, or ``None`` where none did.

        Evidence outlives the process it describes (ADR-0025).
        """
        with self._lock:
            for process, retired_ts in self._retired.get(pid, ()):
                if ts <= retired_ts:
                    return process
            live = self._live.get(pid)
            if live is not None:
                return live
            retired = self._retired.get(pid)
            return retired[-1][0] if retired else None
