"""Accumulating a run's records into rings (ADR-0016)."""

import logging
from collections.abc import Iterator, Set
from itertools import chain

import msgspec

from ..model.process import Process
from ..model.protocol import TGCStatsInfo
from ..support.time_units import secs_to_ns
from .metrics import METRICS, PAUSE_KEY
from .stats import Stats, get_quantile_value

logger = logging.getLogger(__name__)


TStatsData = dict[str, dict[int, Stats]]

# (process, iid). One interpreter's sampled metrics, a generation dict per
# metric. One ring's durations are one of those generations. Keyed on the
# process, so a successor on a reused pid opens rings of its own. Named for
# the process and not for a ring index, which in CPython is the write cursor
# into the ring.
type RingKey = tuple[Process, int]


class LossTotals(msgspec.Struct):
    """Records gcmon never read, and the pause time they held.

    `StreamingStats` accumulates into one of these per key and hands readers
    a `PauseTotals` instead.
    """

    count: int = 0
    pause_ns: int = 0

    def add(self, count: int, pause_ns: int) -> None:
        self.count += count
        self.pause_ns += pause_ns


class PauseTotals(msgspec.Struct, frozen=True, gc=False):
    """One generation's pauses, for one process or for all of them.

    `sampled_*` is what gcmon measured, `lost_*` what the target's counters
    say it missed. ADR-0015 covers why adding them is exact.

    Frozen because both reads build one from four scalars. A write to what
    you got back would land on a snapshot gcmon never reads again. Those
    scalars cannot hold a cycle either, so the collector need not track one.
    """

    sampled_count: int = 0
    sampled_pause_ns: float = 0.0
    lost_count: int = 0
    lost_pause_ns: int = 0

    @property
    def exact_count(self) -> int:
        """Collections gcmon accounts for, seen and unseen alike."""
        return self.sampled_count + self.lost_count

    @property
    def exact_pause_ns(self) -> float:
        """Pause time over those same collections: sampled plus lost."""
        return self.sampled_pause_ns + self.lost_pause_ns

    @property
    def coverage(self) -> float:
        """Observed share of those collections, in ``[0, 1]``.

        An empty generation reports 1.0, so no call site needs a guard.
        """
        # Summed here rather than read off `exact_count`, which costs a
        # property call to do the same addition.
        exact = self.sampled_count + self.lost_count
        if exact == 0:
            return 1.0
        return self.sampled_count / exact

    @property
    def scale_factor(self) -> float:
        """Multiplier taking a sampled pause sum to the exact one.

        Sub-phases have no exact counterpart but partition the pause, so
        scaling a measured phase sum estimates it. It cannot correct a
        percentile (ADR-0015).
        """
        sampled = self.sampled_pause_ns
        if sampled == 0:
            return 1.0
        return (sampled + self.lost_pause_ns) / sampled


class CumulativeCounters(msgspec.Struct):
    """One ring's counters, counted as the target counts them: from the moment
    its interpreter started, not from the moment gcmon attached.

    A poll overwrites the slot, and a fold sums slots into a fresh one. Both
    reads return that fresh one, never a slot, so this side needs no
    freezing.
    """

    collections: int = 0
    duration_s: float = 0.0

    def add(self, collections: int, duration_s: float) -> None:
        self.collections += collections
        self.duration_s += duration_s

    @property
    def pause_ns(self) -> int:
        """The same history in nanoseconds.

        The target counts seconds here and nanoseconds everywhere else.
        """
        return secs_to_ns(self.duration_s)


class RingStats(msgspec.Struct):
    """Everything one interpreter of one process accumulates.

    One entry per key, so a ring's three kinds of number settle together on
    the exit that ends them and are read together afterwards.

    `metrics` is ``None`` until the ring is admitted, and stays ``None`` if
    the bound declined it. The bound caps sample buffers alone: they hold a
    thousand values per generation per metric, where `loss` and `cumulative`
    hold two numbers per generation each. A declined ring goes on counting,
    so the run totals and the coverage figures stay whole.
    """

    metrics: TStatsData | None = None
    declined: bool = False
    loss: dict[int, LossTotals] = msgspec.field(default_factory=dict)
    cumulative: dict[int, CumulativeCounters] = msgspec.field(default_factory=dict)

    def settle(self) -> None:
        """Fix the percentiles and give the sample buffers back."""
        if self.metrics is None:
            return
        for phase_stats in self.metrics.values():
            for stats in phase_stats.values():
                stats.materialize()

    def sampled(self, gen: int) -> Stats:
        """The pause durations gcmon read for one generation of this ring."""
        if self.metrics is None:
            return Stats()
        return self.metrics[PAUSE_KEY][gen]

    def pause_totals(self, gen: int) -> PauseTotals:
        """One generation, sampled and lost together."""
        sampled = self.sampled(gen)
        lost = self.loss.get(gen, LossTotals())
        return PauseTotals(sampled.count(), sampled.sum(), lost.count, lost.pause_ns)


def _record(stats: TStatsData, item: TGCStatsInfo, metric_name: str) -> None:
    """Record a phase duration in nanoseconds, the unit every metric keeps."""
    metric = METRICS[metric_name]
    ts_start, ts_stop = metric.get_values(item)
    gen = item.gen

    if ts_start != ts_stop:
        stats[metric_name][gen].update(ts_stop - ts_start)


class StreamingStats:
    # How many interpreters may hold sample buffers at once, one set per
    # (process, iid) covering that interpreter's three generations. A set costs
    # what it did when the bound counted processes, so the footprint of the
    # processes bounded then buys several interpreters each now. A process
    # that exits settles its buffers and hands the slots back.
    MAX_ACTIVE_RINGS = 256
    GENS = (0, 1, 2)
    # Under this, the sampled percentiles cover too little of the run to leave
    # a reader working it out from the coverage figure, so gcmon says so once.
    COVERAGE_ADVISORY = 0.9

    def __init__(self) -> None:
        self._count: int = 0
        # Phase durations in nanoseconds, per metric and generation.
        self.metrics: TStatsData = {metric: {gen: Stats() for gen in self.GENS} for metric in METRICS}
        # The rings of the processes running now. An entry leaves on the exit
        # that settles it.
        self._running_rings: dict[RingKey, RingStats] = {}
        # The rings of the processes that have exited, settled and kept to
        # the end of the run. Two dicts rather than one and a flag, because
        # `low_coverage` reads only the running ones and the bound counts only
        # those.
        self._settled_rings: dict[RingKey, RingStats] = {}
        # Running rings holding sample buffers, which is what the bound counts.
        # A ring with only its counters costs too little to bound.
        self._admitted_rings = 0
        # The processes gcmon has records from and has not seen exit. A
        # record reaches gcmon only from a process that is running, so its
        # arrival is what opens one and `materialize` closes it again.
        self._open_processes: set[Process] = set()
        self._bound_warned = False
        # Process-wide, with no generation and no interpreter affinity
        # (ADR-0004).
        self._heap_size: dict[Process, int] = {}
        self._read_time: Stats = Stats()

    def update(self, process: Process, item: TGCStatsInfo) -> None:
        if (process, item.iid) in self._settled_rings:
            # A record reaching a settled ring is one a successor re-read and
            # the caller attributed back here. Folding it in twice would put
            # the run totals and the percentiles out by a duplicate
            # (ADR-0016).
            return

        self._count += 1

        for metric in METRICS:
            _record(self.metrics, item, metric)

        self._open_processes.add(process)
        # Process-wide and one integer per process, so it is kept whether or
        # not the ring behind the record was admitted.
        self._heap_size[process] = max(self._heap_size.get(process, 0), item.heap_size)

        ring = self._open_ring(process, item.iid)
        metrics = ring.metrics or self._admit(ring, (process, item.iid))
        if metrics is None:
            return

        for metric in METRICS:
            _record(metrics, item, metric)

    def _open_ring(self, process: Process, iid: int) -> RingStats:
        """The ring the records arriving now belong to, opened if new.

        Every ring gets one, since loss and cumulative totals are due from a
        ring the bound turned away as much as from one it admitted.
        """
        key = (process, iid)
        ring = self._running_rings.get(key)
        if ring is None:
            ring = RingStats()
            self._running_rings[key] = ring
        return ring

    def _admit(self, ring: RingStats, key: RingKey) -> TStatsData | None:
        """Give *ring* its sample buffers, or ``None`` where none are free.

        A ring gets them on its first record and keeps them until its process
        exits. ``None`` means this record and every later one is measured into
        the run totals and the ring's own counters alone, which happens when
        `MAX_ACTIVE_RINGS` interpreters were already running with buffers of
        their own at the moment this ring appeared.

        Either way what a ring samples covers one process's interpreter over
        one unbroken stretch, so its sampled count and its percentiles always
        describe the same records.
        """
        if ring.declined:
            # Declined once, declined for as long as this entry stands. A slot
            # freed by another process's exit would otherwise start sampling
            # this ring midway through its life, and nothing in what it kept
            # would say where the sampling began.
            return None

        if self._admitted_rings >= self.MAX_ACTIVE_RINGS:
            self._decline(ring, key)
            return None

        ring.metrics = {metric: {gen: Stats() for gen in self.GENS} for metric in METRICS}
        self._admitted_rings += 1
        return ring.metrics

    def _decline(self, ring: RingStats, key: RingKey) -> None:
        """Note that this ring keeps no sampled metrics, saying why the first
        time."""
        ring.declined = True
        if self._bound_warned:
            return

        self._bound_warned = True
        logger.warning(
            "PID %s interpreter %s: gcmon already holds detailed statistics for %s running "
            "interpreters, the most it keeps at once. Records read from any further interpreter are "
            "counted in the run totals, and gcmon keeps no detailed statistics of its own for it.",
            *key,
            self.MAX_ACTIVE_RINGS,
        )

    def materialize(self, process: Process) -> None:
        """Settle every ring of *process*, which has exited.

        Whatever claims the pid next is a different `Process` and starts
        clean, with sample buffers and totals of its own.
        """
        if process not in self._open_processes:
            return

        self._settle(process, [key for key in self._running_rings if key[0] == process])

    def _settle(self, process: Process, keys: list[RingKey]) -> None:
        """Close *process*, which is open, and settle *keys*, which are its
        rings and no other process's.
        """
        self._open_processes.discard(process)

        for key in keys:
            settled = self._running_rings.pop(key)
            if settled.metrics is not None:
                self._admitted_rings -= 1
            settled.settle()
            self._settled_rings[key] = settled

    def retain(self, processes: Set[Process]) -> None:
        """Settle every ring whose process is not in *processes*.

        A process missing from the caller's per-tick listing of the target's
        children has gone.
        """
        departed = self._open_processes - set(processes)
        if not departed:
            return

        process_keys: dict[Process, list[RingKey]] = {process: [] for process in departed}
        for key in self._running_rings:
            keys = process_keys.get(key[0])
            if keys is not None:
                keys.append(key)

        for process, keys in process_keys.items():
            self._settle(process, keys)

    def record_read_time(self, duration_ns: int) -> None:
        self._read_time.update(duration_ns)

    def record_loss(self, process: Process, iid: int, gen: int, lost_count: int, lost_pause_ns: int) -> None:
        """Record one interval's worth of records gcmon did not read.

        `record_loss` hands over one poll's gap at a time, so these sum.
        Sampled plus lost is the exact total ADR-0015 defines, so the rings
        themselves stay in the monitor.
        """
        self._open_processes.add(process)
        ring = self._open_ring(process, iid)
        ring.loss.setdefault(gen, LossTotals()).add(lost_count, lost_pause_ns)

    def low_coverage(self, process: Process) -> tuple[int, int, float] | None:
        """The least covered ring of *process* when it sits under
        `COVERAGE_ADVISORY`, as its interpreter, its generation and its
        coverage. ``None`` on a healthy run.

        Reads the rings running now and not the settled ones, whose
        processes have exited.
        """
        worst: tuple[int, int, float] | None = None
        for (ring_process, iid), ring in self._running_rings.items():
            if ring_process != process or ring.declined:
                # A declined ring has a sampled count of zero here, so the
                # advisory has nothing to say about it.
                continue
            for gen, lost in ring.loss.items():
                if not lost.count:
                    continue

                sampled = ring.sampled(gen).count()
                coverage = sampled / (sampled + lost.count)
                if coverage < self.COVERAGE_ADVISORY and (worst is None or coverage < worst[2]):
                    worst = (iid, gen, coverage)
        return worst

    def observe_cumulative(self, process: Process, iid: int, gen: int, collections: int, duration_s: float) -> None:
        """Take one ring's totals since its interpreter started.

        The target counts both of them cumulatively, so the newest values
        replace the previous ones; this observes a counter rather than
        appending to one, which is what separates it from `record_loss`. A
        successor on a reused pid writes into an entry of its own
        (ADR-0016).
        """
        self._open_processes.add(process)
        self._open_ring(process, iid).cumulative[gen] = CumulativeCounters(collections, duration_s)

    def pause_totals(self, process: Process, iid: int, gen: int) -> PauseTotals:
        """One ring, read once.

        Every ring at once is :meth:`pause_totals_by_gen`, which costs a pass
        instead.
        """
        ring = self._find_ring(process, iid)
        if ring is None:
            return PauseTotals()
        return ring.pause_totals(gen)

    def _all_rings(self) -> Iterator[RingStats]:
        """Every ring of the run, running and settled alike."""
        return chain(self._running_rings.values(), self._settled_rings.values())

    def pause_totals_by_gen(self) -> dict[int, PauseTotals]:
        """Every generation's pause totals over every ring."""
        # Folded here rather than behind a helper, which had this one caller.
        lost: dict[int, LossTotals] = {}
        for ring in self._all_rings():
            for gen, loss in ring.loss.items():
                lost.setdefault(gen, LossTotals()).add(loss.count, loss.pause_ns)

        pause = self.metrics[PAUSE_KEY]
        by_gen = {}
        for gen in self.GENS:
            sampled = pause[gen]
            gen_lost = lost.get(gen)
            by_gen[gen] = PauseTotals(
                sampled.count(),
                sampled.sum(),
                gen_lost.count if gen_lost is not None else 0,
                gen_lost.pause_ns if gen_lost is not None else 0,
            )
        return by_gen

    def cumulative_scope(self) -> tuple[int, int]:
        """How many interpreters, in how many processes, the cumulative fold
        covers.

        A caller states both alongside the fold, so a reader can tell one
        interpreter's history from a sum over five that started at different
        moments.

        A pid gcmon never saw exit still counts as one.
        """
        interpreters = {
            key for key, ring in self._keyed_rings() if any(totals.collections for totals in ring.cumulative.values())
        }
        return len(interpreters), len({process for process, _iid in interpreters})

    def cumulative_totals_by_gen(self) -> dict[int, CumulativeCounters]:
        """Fold every ring's cumulative counters into a per-gen total, single
        pass."""
        by_gen: dict[int, CumulativeCounters] = {}
        for ring in self._all_rings():
            for gen, totals in ring.cumulative.items():
                by_gen.setdefault(gen, CumulativeCounters()).add(totals.collections, totals.duration_s)
        return by_gen

    @property
    def read_time(self) -> Stats:
        """Read durations in nanoseconds, over every polled pid."""
        return self._read_time

    def _find_ring(self, process: Process, iid: int) -> RingStats | None:
        """One ring, running or settled, or ``None`` where the run has none."""
        key = (process, iid)
        ring = self._running_rings.get(key)
        return ring if ring is not None else self._settled_rings.get(key)

    def _keyed_rings(self) -> Iterator[tuple[RingKey, RingStats]]:
        """Every ring of the run under the key a caller names it by."""
        yield from self._running_rings.items()
        yield from self._settled_rings.items()

    def get_ring_stats(self, process: Process, iid: int) -> TStatsData | None:
        """One interpreter's sampled metrics, still filling or settled.

        ``None`` where the ring has none, which is a key gcmon never read or a
        ring the bound declined.
        """
        ring = self._find_ring(process, iid)
        return ring.metrics if ring is not None else None

    def rings(self) -> list[RingKey]:
        """Every ring holding sampled metrics, by pid, then epoch, then
        interpreter.

        A ring the bound declined holds none and is absent;
        :meth:`untracked_rings` counts those.
        """
        return [key for key, _ in self.ring_metrics()]

    def ring_metrics(self) -> list[tuple[RingKey, TStatsData]]:
        """The same rings in the same order, each with the metrics it holds."""
        return sorted(
            ((key, ring.metrics) for key, ring in self._keyed_rings() if ring.metrics is not None),
            key=lambda entry: (entry[0][0].pid, entry[0][0].pid_epoch, entry[0][1]),
        )

    def untracked_rings(self) -> int:
        """How many rings reached `update` with no slot to take.

        Their records are in the run totals and in the coverage figures, so a
        caller can state the count rather than leave a reader adding the rings
        up and finding them short.
        """
        return sum(1 for ring in self._all_rings() if ring.declined)

    def count(self) -> int:
        return self._count

    def heap_size_p99(self) -> float | None:
        """The 99th percentile of the per-process high-water heap sizes.

        ``None`` when no record carried one, so a caller leaves the metric
        out rather than publishing a zero.
        """
        sizes = self._heap_size.values()
        if not sizes:
            return None
        if len(sizes) == 1:
            # Every percentile of one mark is that mark, and one monitored pid
            # is the usual case.
            return float(next(iter(sizes)))
        return get_quantile_value(sorted(sizes), 99)
