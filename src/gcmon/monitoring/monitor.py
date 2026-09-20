"""Polling a process for GC records and passing them to the stats and the
exporters."""

import logging
import time
from _remote_debugging import get_child_pids
from collections.abc import Callable, Sequence, Set
from itertools import groupby
from typing import NamedTuple, Self

import msgspec

from ..exporters import EventsExporter
from ..model.data import GenLoss, LossMsg
from ..model.loss import (
    RingAccumulator,
    RingKey,
)
from ..model.poll_status import PollStatus
from ..model.process import Process
from ..model.protocol import TGCStatsInfo
from ..stats.streaming_stats import StreamingStats
from ..support.vocabulary import PROGRAM_NAME
from .events_reader import EventsReader, TargetUnavailable
from .process_registry import ProcessRegistry
from .target_process import TargetProcess
from .wait_policy import WaitPolicy, WaitPolicyFactory

logger = logging.getLogger(PROGRAM_NAME)

__all__ = ["EventsMonitor", "PollReport"]


def _is_complete(record: TGCStatsInfo) -> bool:
    """False for a slot holding no finished record: never written, mid-write
    with ``ts_start`` published and ``ts_stop`` not yet, or stamped by a start
    read that failed, which leaves ``0`` in ``ts_start``."""
    return 0 < record.ts_start < record.ts_stop


class PidState(msgspec.Struct):
    """What gcmon carries from one poll of a process to the next."""

    rings: dict[RingKey, RingAccumulator] = msgspec.field(default_factory=dict)
    # None before the first read. Two polls bound a loss record, so one poll
    # bounds nothing.
    ts_last_poll: int | None = None


class PollResult(NamedTuple):
    """What one poll of one pid did.

    *process* is what the records were filed under, and ``None`` exactly where
    the read did not return: a pid enters the registry on a read that returned
    (ADR-0025).
    """

    status: PollStatus
    process: Process | None


class PollReport(msgspec.Struct):
    """What one tick of monitoring found.

    ``live`` answered :attr:`PollStatus.OK`. For a process that never
    collects, a successful read is the only evidence gcmon has that it existed,
    which is what liveness reporting rests on (ADR-0029).

    ``keep_running`` is false once no wait policy wants the run to go on.
    """

    live: frozenset[Process]
    keep_running: bool


class EventsMonitor:
    def __init__(
        self,
        process: TargetProcess,
        exporter: EventsExporter,
        stats: StreamingStats,
        *,
        reader: EventsReader,
        wait_policy_factory: WaitPolicyFactory,
        is_pid_enabled: Callable[[int], bool] | None = None,
        registry: ProcessRegistry | None = None,
    ) -> None:
        """
        *reader* reads a process's records.

        *wait_policy_factory* builds the per-pid policy that decides when a pid
        is finished.

        *is_pid_enabled* is the control plane's per-pid verdict: ``False`` means
        the control server has suppressed that pid and it must not be polled.
        ``None`` means no control plane.

        *registry* creates the `Process` every record is filed under, one per
        run. A caller with none gets one of its own.
        """
        self._process = process
        self._exporter = exporter
        self._enabled = True
        self._pids: dict[int, PidState] = {}
        self._policies: dict[int, WaitPolicy] = {}
        self._reader = reader
        self._wait_policy_factory = wait_policy_factory
        self._is_pid_enabled = is_pid_enabled
        self._stats = stats
        self._processes = registry if registry is not None else ProcessRegistry()
        # The latest clock instant this tick has reached. See :meth:`tick`.
        self._tick_read_ns = 0
        self._coverage_warned = False
        # Per pid, every reason already reported for not reading it. Keyed on
        # the pid because a pid gcmon cannot read has no process to key on.
        self._unreadable: dict[int, set[str]] = {}

    def tick(self, now_ns: int, stop: Callable[[], bool]) -> PollReport:
        """Poll the target and every child once, and report what answered.

        Prunes the state of every pid that has left the process tree first, so
        a reused pid inherits nothing from the process before it.

        *now_ns* stamps the whole tick, liveness included. The caller reads the
        clock once and hands the same instant to the RSS sampler.

        *stop* is asked between pids, so a shutdown does not have to wait out a
        whole process tree.
        """
        child_pids = self._get_child_pids()
        children = [self._process.pid, *(child_pids or [])]

        # A process that exits between two ticks is never polled again, so no
        # policy gives up on it and the branch below never runs. None means the
        # listing failed, so prune only when it worked.
        if child_pids is not None:
            self._retain(set(children), now_ns)

        # Liveness is stamped no earlier than the reads that prove these
        # processes alive, so two alive in one tick share an end
        # (ADR-0029).
        self._tick_read_ns = now_ns

        live: set[Process] = set()
        keep_running = False
        for pid in children:
            if stop():
                break

            if self._is_pid_enabled is not None and not self._is_pid_enabled(pid):
                continue

            policy = self._policies.get(pid)
            if policy is None:
                policy = self._policies[pid] = self._wait_policy_factory()

            status, process = self._poll(pid)
            keep_waiting = policy.wait(status)
            keep_running = keep_running or keep_waiting
            if process is not None:
                live.add(process)
            elif not keep_waiting:
                # The policy stays behind. A fresh one answers True until its
                # own startup timeout expires, holding the run open.
                self._forget(pid, now_ns)

        live_processes = frozenset(live)

        # After the poll phase, one batched call, skipped on an empty set.
        # ADR-0029 argues all three.
        if live_processes:
            self._exporter.add_process_liveness(live_processes, self._tick_read_ns)

        return PollReport(live=live_processes, keep_running=keep_running)

    def _get_child_pids(self) -> list[int] | None:
        """Every descendant of the target, or ``None`` when the read failed.

        An empty list means no children. ``None`` means no answer, so a caller
        pruning state for missing pids skips that tick.
        """
        try:
            return get_child_pids(self._process.pid, recursive=True)
        except Exception as exc:
            logger.warning(
                "Monitor for PID %s encountered error while gathering children PIDs", self._process.pid, exc_info=exc
            )
            return None

    def _poll(self, pid: int) -> PollResult:
        """Read *pid* once and file what it answered.

        A pid enters the registry on a read that returns, not when the listing
        names it and not on the attempt: a pid gcmon cannot read produces no
        records and needs no process (ADR-0025). Creating one per attempt spent
        an epoch and a command-line read on every poll of a pid that never
        became readable, for as long as it was listed.
        """
        if not self._enabled:
            logger.warning(
                "Monitor for PID %s already stopped",
                pid,
            )
            return PollResult(PollStatus.FAIL, None)

        try:
            ts_read_start = time.monotonic_ns()
            records = self._reader.read(pid)
            ts_read_stop = time.monotonic_ns()
            self._tick_read_ns = ts_read_stop
            self._stats.record_read_time(ts_read_stop - ts_read_start)

            process = self._processes.current(pid)
            if process is None:
                # The exporter is handed the command line as part of the
                # creation, see ADR-0025.
                process = self._processes.create(pid, self._exporter.add_process_cmdline)
            self._ingest(process, records, ts_read_start)

            self._unreadable.pop(pid, None)
            return PollResult(PollStatus.OK, process)
        except TargetUnavailable as exc:
            # The ordinary end of a run reaches here, so it stays at debug
            # level: a warning would put a traceback on stderr every time a
            # target exits. ADR-0020.
            #
            # Said once per pid and reason: a target that never becomes readable
            # is polled again every tick, and the repeat carries nothing the
            # first line did not. A reason not seen on that pid is a target that
            # got further, and is reported. A read that returns forgets the pid,
            # so a process that goes quiet after answering is reported too.
            reason = str(exc)
            reported = self._unreadable.setdefault(pid, set())
            if reason not in reported:
                reported.add(reason)
                logger.debug("Error while polling PID %s (child PID=%s): %s", self._process.pid, pid, exc)
            return PollResult(PollStatus.INVALID_PROCESS, None)
        except Exception as exc:
            logger.warning("Monitor for PID %s (child PID=%s) encountered error", self._process.pid, pid, exc_info=exc)
            return PollResult(PollStatus.FAIL, None)

    def _forget(self, pid: int, ts: int) -> None:
        """Drop the cursors and the attachment held for *pid*, so a reused pid
        inherits no counter, no poll instant and no debug offsets from the
        process before it. The policy stays; see :meth:`tick`.
        """
        self._pids.pop(pid, None)
        self._reader.forget(pid)
        process = self._processes.current(pid)
        if process is not None:
            # Settle before retiring: the rings file under the process that
            # earned them, and a retirement takes it out of the registry.
            self._stats.materialize(process)
            self._processes.retire(pid, ts)
            self._exporter.add_process_retired(process)

    def _retain(self, pids: Set[int], ts: int) -> None:
        """Drop the state of every pid outside *pids*, all of it at once.

        A cursor outliving its policy, or the reverse, is the disagreement
        ADR-0017 rules out, and ADR-0020 puts the reader's attachment under the
        same rule. A process that exits between two ticks is never polled again,
        so no policy gives up on it and this drops it instead.
        """
        for pid in self._pids.keys() - pids:
            del self._pids[pid]
        for pid in self._policies.keys() - pids:
            del self._policies[pid]
        self._reader.retain(pids)
        for pid in self._unreadable.keys() - pids:
            del self._unreadable[pid]
        live = self._processes.live()
        self._stats.retain({process for process in live if process.pid in pids})
        self._processes.retain(pids, ts)
        # Sorted so a tick dropping several reports them in one order.
        for process in sorted(process for process in live if process.pid not in pids):
            self._exporter.add_process_retired(process)

    def _ingest(self, process: Process, records: Sequence[TGCStatsInfo], ts_poll: int) -> None:
        """Emit the records in *records* not seen yet.

        Every poll returns the whole ring buffer, so ``collections`` is what
        identifies a record.

        *ts_poll* is when this read began. It closes the interval the previous
        poll opened, see ADR-0015.
        """
        pid = process.pid
        state = self._pids.setdefault(pid, PidState())
        ts_prev_poll = state.ts_last_poll
        state.ts_last_poll = ts_poll

        # Ring buffer records arrive wrapped, with the generations
        # concatenated, so restore each ring's counter order.
        ordered = sorted(
            (record for record in records if _is_complete(record)),
            key=lambda record: (record.iid, record.gen, record.collections),
        )

        fresh: list[TGCStatsInfo] = []
        gens_by_iid: dict[int, list[GenLoss]] = {}
        for (iid, gen), group in groupby(ordered, key=lambda record: (record.iid, record.gen)):
            accumulator = state.rings.setdefault((iid, gen), RingAccumulator())
            unseen = accumulator.unseen(group)
            if not unseen:
                continue

            gen_loss = accumulator.ingest(unseen)
            gens_by_iid.setdefault(iid, []).append(gen_loss)
            self._stats.observe_cumulative(process, iid, gen, accumulator.last_collections, accumulator.last_duration)
            if gen_loss.lost_count:
                self._stats.record_loss(process, iid, gen, gen_loss.lost_count, gen_loss.lost_pause_ns)
            fresh.extend(unseen)

        for iid, gens in gens_by_iid.items():
            if all(gen_loss.no_loss for gen_loss in gens):
                continue

            # A first poll seeds every ring it touches, and seeding opens no
            # gap, so no interval reaches here on one.
            assert ts_prev_poll is not None
            self._exporter.add_loss_event(process, LossMsg(iid=iid, ts_start=ts_prev_poll, ts_stop=ts_poll, gens=gens))

        # We want to keep exported events in the time order
        rereads: dict[Process, int] = {}
        for record in sorted(fresh, key=lambda record: (record.iid, record.ts_start)):
            # A pid pruned from the tree loses its read cursor, so whatever
            # claims it next re-reads records dated inside its predecessor's
            # life. Those went out under the predecessor already, and giving
            # them to the successor would back-date its span to before it
            # existed. The accumulator has still swallowed them, so the cursor
            # sits past them and they are not offered again (ADR-0025).
            dated_under = self._processes.at(pid, record.ts_start)
            if dated_under is not None and dated_under != process:
                rereads[dated_under] = rereads.get(dated_under, 0) + 1
                continue
            self._exporter.add_event(process, record)
            self._stats.update(process, record)

        for predecessor, count in rereads.items():
            logger.debug(
                "PID %s: dropped %s record(s) re-read for %s, already exported under %s",
                pid,
                count,
                process,
                predecessor,
            )

        self._warn_low_coverage(process)

    def _warn_low_coverage(self, process: Process) -> None:
        """Say once per run that gcmon is reading too little of its target.

        What to do about it is not said here: it turns on whether the loop is
        holding its schedule, which this cannot know when it fires. The
        end-of-run summary carries the remedy; see ADR-0019.
        """
        if self._coverage_warned:
            return

        low = self._stats.low_coverage(process)
        if low is None:
            return

        iid, gen, coverage = low
        self._coverage_warned = True
        logger.warning(
            "PID %s interpreter %s generation %s: only %s%% of collections observed so far. Counts "
            "and sums are reconstructed and exact; percentiles cover only what was sampled and read "
            "high.",
            process.pid,
            iid,
            gen,
            # Truncated: 89.6% would read as "90%", the floor this fires
            # below. The inner round absorbs float error, which takes an exact
            # 29 of 100 to 28.999999999999996 and would print it as 28%.
            int(round(coverage * 100, 6)),
        )

    def stop(self) -> None:
        """Close the exporter, let go of every attachment, and stop accepting
        polls.

        The reader is pruned here and not left to garbage collection because an
        attachment is a handle on somebody else's process: on Windows it holds
        the pid reserved for as long as gcmon keeps it (ADR-0020), and a monitor
        that has stopped monitoring should not be doing that.

        Safe to call more than once.
        """
        self._exporter.close()
        self._reader.retain(frozenset())
        self._enabled = False

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    @property
    def pid(self) -> int:
        return self._process.pid

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.stop()
