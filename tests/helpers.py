from __future__ import annotations

import json
import zlib
from collections.abc import Callable, Iterator, Sequence, Set
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import Final, override

from perfetto.protos.perfetto.trace.perfetto_trace_pb2 import Trace, TracePacket
from perfetto.trace_processor import TraceProcessor, TraceProcessorConfig

from gcmon.exporters.exporter import EventsExporter
from gcmon.exporters.trace_converter import counter_display_name
from gcmon.model.data import GCStatsInfo, GenLoss, LossMsg
from gcmon.model.names import (
    ALIVE_SIZE,
    CANDIDATES,
    CLEAR_WEAKREFS_COUNT,
    COLLECTED,
    COLLECTIONS,
    DELETED_GARBAGE_COUNT,
    DURATION,
    FINALIZED_GARBAGE_COUNT,
    GEN,
    HEAP_SIZE,
    IID,
    INCREMENT_SIZE,
    PID,
    TS_CLEAR_WEAKREFS_STOP,
    TS_DEDUCE_UNREACHABLE_START,
    TS_DEDUCE_UNREACHABLE_STOP,
    TS_DELETE_GARBAGE_START,
    TS_DELETE_GARBAGE_STOP,
    TS_FILL_INCREMENT_START,
    TS_FILL_INCREMENT_STOP,
    TS_FINALIZE_GARBAGE_STOP,
    TS_HANDLE_RESURRECTED_STOP,
    TS_HANDLE_WEAKREF_CALLBACKS_START,
    TS_HANDLE_WEAKREF_CALLBACKS_STOP,
    TS_MARK_ALIVE_START,
    TS_MARK_ALIVE_STOP,
    TS_START,
    TS_STOP,
    TYPE,
    UNCOLLECTABLE,
)
from gcmon.model.process import Process
from gcmon.model.protocol import TGCStatsInfo, TInstantMsg, TLossMsg
from gcmon.model.trace_event import Counter, InterpreterTrack, LossTrack, ProcessTrack, Track
from gcmon.monitoring.events_reader import EventsReader
from gcmon.monitoring.monitor import EventsMonitor
from gcmon.monitoring.process_registry import ProcessRegistry
from gcmon.stats.views import TableFormat
from gcmon.support.vocabulary import ENCODING
from tests.perfetto_prebuilt import trace_processor_bin

zstd: ModuleType | None
try:
    from compression import zstd
except ImportError:
    zstd = None

HAS_LIBZSTD: bool = zstd is not None
"""Whether this interpreter can read a zstd batch (ADR-0022)."""

_JsonValue = int | float | str
JsonlRecord = dict[str, _JsonValue]
DefaultsValue = Path | float | None | int | str | bool | TableFormat

# What one poll of one pid answers. Takes the pid, because a test driving a
# process tree answers differently per child.
ReadFn = Callable[..., Sequence[TGCStatsInfo]]

__all__ = [
    "DefaultsValue",
    "FakeEventsReader",
    "JsonlRecord",
    "MockExporter",
    "ReadFn",
    "assert_is_instant_msg",
    "assert_valid_perfetto_trace",
    "create_jsonl_record",
    "create_mock_incremental_item",
    "create_mock_loss_item",
    "create_mock_stats_item",
    "interpreter_track",
    "loss_track",
    "monitored",
    "open_trace_processor",
    "perfetto_packets",
    "polled",
    "proc",
    "process_track",
]


def proc(pid: int, pid_epoch: int = 1) -> Process:
    """A `Process` for a test that cares about the pid and not the epoch."""
    return Process(pid, pid_epoch)


def process_track(pid: int, pid_epoch: int = 1) -> ProcessTrack:
    """The process's own row, for a test that names a pid rather than a
    process. One place to change when a `Process` gains a field."""
    return ProcessTrack(proc(pid, pid_epoch))


def interpreter_track(pid: int, iid: int, pid_epoch: int = 1) -> InterpreterTrack:
    """Interpreter *iid*'s row on *pid*. See :func:`process_track`."""
    return InterpreterTrack(proc(pid, pid_epoch), iid)


def loss_track(pid: int, iid: int, pid_epoch: int = 1) -> LossTrack:
    """Interpreter *iid*'s loss row on *pid*. See :func:`process_track`."""
    return LossTrack(proc(pid, pid_epoch), iid)


def gen_counter(track: Track, gen: int, metric: str, ts: int, value: float) -> Counter:
    """One generation's counter series, named the way the converter names it.

    The display name is derived rather than passed: it is a function of the
    generation and the metric, and spelling both out let a test assert a name
    the converter would never write.
    """
    return Counter(track, metric, counter_display_name(gen, metric), ts, value)


def monitored(*pids: int) -> ProcessRegistry:
    """A registry holding a live process per pid, as the monitor's first
    poll leaves it.

    A control-plane message names a process gcmon monitors or has
    monitored, and nothing but the monitor creates one, so a server under
    test is given the processes its clients will name.
    """
    registry = ProcessRegistry()
    for pid in pids:
        registry.create(pid)
    return registry


def polled(monitor: EventsMonitor, pid: int) -> Process:
    """The process *monitor* files *pid*'s records under, created if it has
    none yet.

    `EventsMonitor._poll` creates one on a read that returned (ADR-0025); a
    test driving `_ingest` past the read goes through here, so the registry
    agrees with the process it hands in.
    """
    return monitor._processes.current(pid) or monitor._processes.create(pid)


def no_records(pid: int) -> Sequence[TGCStatsInfo]:
    """What a target that has collected nothing answers."""
    return ()


class FakeEventsReader(EventsReader):
    """An :class:`EventsReader` driven by a callable, recording its prunes.

    *reads* answers one poll of one pid. Raise :class:`TargetUnavailable` from
    it to play a target that has not started or has exited; raise anything else
    to play a failure gcmon does not translate.

    ``attached`` is the set of pids this would be holding an attachment for, so
    a test can assert ADR-0017's rule -- that an attachment and its cursors are
    dropped in the same pass -- without reaching into the monitor. It follows
    the real reader's lifetime: a pid enters on a read that returns, and leaves
    on a read that raises, on ``forget``, or on a ``retain`` that excludes it.
    """

    def __init__(self, reads: ReadFn | None = None) -> None:
        self.reads: ReadFn = reads if reads is not None else no_records
        self.attached: set[int] = set()
        self.read_pids: list[int] = []
        self.forgotten: list[int] = []
        self.retained: list[frozenset[int]] = []

    @override
    def read(self, pid: int) -> Sequence[TGCStatsInfo]:
        self.read_pids.append(pid)
        try:
            records = self.reads(pid)
        except BaseException:
            self.attached.discard(pid)
            raise
        self.attached.add(pid)
        return records

    @override
    def retain(self, pids: Set[int]) -> None:
        self.retained.append(frozenset(pids))
        self.attached &= set(pids)

    @override
    def forget(self, pid: int) -> None:
        self.forgotten.append(pid)
        self.attached.discard(pid)


class MockExporter(EventsExporter):
    """Mock GCMonitorExporter for testing.

    This class simulates an exporter that collects events in memory.
    """

    def __init__(self) -> None:
        """Initialize the mock exporter."""
        super().__init__()
        self.events: list[TGCStatsInfo] = []
        self.instant_events: list[tuple[int, TInstantMsg]] = []
        # Per-pid state surviving a process it does not belong to (ADR-0017)
        # emits no record of its own. It shows up as the wrong pid's cursor
        # answering, or as a loss window for collections that never happened,
        # and `events` shows neither.
        self.events_by_pid: dict[int, list[TGCStatsInfo]] = {}
        self.loss_events: list[tuple[int, TLossMsg]] = []
        # One entry per tick that observed anything (ADR-0011).
        self.liveness: list[tuple[Set[int], int]] = []
        # One entry per process gcmon let go of, in the order it did.
        self.retired: list[Process] = []
        # One entry per process gcmon created, with what it was running.
        self.launched: list[tuple[Process, tuple[str, ...] | None]] = []
        self._close_called = False

    @override
    def add_event(self, process: Process, item: TGCStatsInfo) -> None:
        """Add an event to the exporter.

        Args:
            process: The process the record came from.
            item: The stats item to add.
        """
        self.events.append(item)
        self.events_by_pid.setdefault(process.pid, []).append(item)

    @override
    def add_loss_event(self, process: Process, item: TLossMsg) -> None:
        """Record a loss window the monitor's arithmetic produced."""
        self.loss_events.append((process.pid, item))

    @override
    def add_process_liveness(self, processes: Set[Process], ts_ns: int) -> None:
        """Record one tick's liveness observation, as the pids it named."""
        self.liveness.append(({process.pid for process in processes}, ts_ns))

    @override
    def add_process_cmdline(self, process: Process, cmdline: tuple[str, ...] | None) -> None:
        """Record the command line the monitor read as it created *process*."""
        self.launched.append((process, cmdline))

    @override
    def add_process_retired(self, process: Process) -> None:
        """Record that the monitor let go of *process*."""
        self.retired.append(process)

    @override
    def add_instant_event(self, process: Process, item: TInstantMsg) -> None:
        """Add an instant event to the exporter.

        Args:
            process: The process the mark belongs to.
            item: The instant message to add.
        """
        self.instant_events.append((process.pid, item))

    @override
    def close(self) -> None:
        """Close the exporter."""
        self._close_called = True


def create_mock_stats_item(
    gen: int = 0,
    ts_start: int = 1_500_000_000,
    ts_stop: int = 1_505_000_000,
    iid: int = 0,
    collections: int = 50,
    collected: int = 200,
    uncollectable: int = 10,
    candidates: int = 40,
    heap_size: int = 52428800,
    duration: float = 0.005,
    **sub_phase: int | None,
) -> GCStatsInfo:
    """A pause record. *sub_phase* sets one sub-phase field and leaves the
    rest unset, which is the shape a `has_*` guard is asked about; the
    builder beside this one sets them all."""
    return GCStatsInfo(
        gen=gen,
        iid=iid,
        ts_start=ts_start,
        ts_stop=ts_stop,
        heap_size=heap_size,
        collections=collections,
        collected=collected,
        uncollectable=uncollectable,
        candidates=candidates,
        duration=duration,
        **sub_phase,
    )


TS0 = 1_000_000_000
"""When a synthetic run's first collection starts."""

SPACING_NS = 1_150_000
"""Measured gap between gen-0 collections."""


def varied_pause(n: int) -> int:
    """Pause lengths that do not all match, so a sum cannot pass by accident."""
    return 100_000 + (n % 7) * 13_000


def build_run(
    count: int,
    gen: int = 0,
    iid: int = 0,
    pause_ns: Callable[[int], int] = varied_pause,
    spacing_ns: int = SPACING_NS,
    ts0: int = TS0,
    first_collection: int = 1,
) -> list[GCStatsInfo]:
    """Every collection a target performs, with ``duration`` accumulating."""
    events: list[GCStatsInfo] = []
    cumulative_ns = 0
    ts = ts0

    for nth in range(count):
        collections = first_collection + nth
        pause = pause_ns(collections)
        cumulative_ns += pause
        events.append(
            create_mock_stats_item(
                gen=gen,
                iid=iid,
                collections=collections,
                ts_start=ts,
                ts_stop=ts + pause,
                duration=cumulative_ns / 1e9,
            )
        )
        ts += spacing_ns

    return events


def true_pause_ns(events: Sequence[GCStatsInfo], first: int, last: int) -> int:
    """Ground truth: the pause sum over collections *first* through *last*."""
    return sum(e.ts_stop - e.ts_start for e in events if first <= e.collections <= last)


def create_mock_loss_item(
    iid: int = 0,
    ts_start: int = 1_000,
    ts_stop: int = 2_000,
    gen: int = 0,
    observed_count: int = 0,
    lost_count: int = 1,
    lost_pause_ns: int = 0,
    lost_from: int = 0,
) -> LossMsg:
    """A loss record naming one generation, for tests about everything else.

    A real record carries an entry per generation active in the interval;
    tests that care about that build their own ``gens``.
    """
    return LossMsg(
        iid=iid,
        ts_start=ts_start,
        ts_stop=ts_stop,
        gens=[
            GenLoss(
                gen=gen,
                observed_count=observed_count,
                lost_count=lost_count,
                lost_pause_ns=lost_pause_ns,
                lost_from=lost_from,
            )
        ],
    )


SUB_PHASES: Final[dict[str, int]] = {
    INCREMENT_SIZE: 1000,
    ALIVE_SIZE: 800,
    TS_MARK_ALIVE_START: 1_500_000_000,
    TS_MARK_ALIVE_STOP: 1_501_000_000,
    TS_FILL_INCREMENT_START: 1_501_000_000,
    TS_FILL_INCREMENT_STOP: 1_502_000_000,
    TS_DEDUCE_UNREACHABLE_START: 1_502_000_000,
    TS_DEDUCE_UNREACHABLE_STOP: 1_503_000_000,
    TS_HANDLE_WEAKREF_CALLBACKS_START: 1_503_000_000,
    TS_HANDLE_WEAKREF_CALLBACKS_STOP: 1_504_000_000,
    TS_FINALIZE_GARBAGE_STOP: 1_505_000_000,
    FINALIZED_GARBAGE_COUNT: 42,
    TS_HANDLE_RESURRECTED_STOP: 1_506_000_000,
    TS_CLEAR_WEAKREFS_STOP: 1_507_000_000,
    CLEAR_WEAKREFS_COUNT: 7,
    TS_DELETE_GARBAGE_START: 1_508_000_000,
    TS_DELETE_GARBAGE_STOP: 1_509_000_000,
    DELETED_GARBAGE_COUNT: 13,
}
"""Every sub-phase a record can carry, in collector order."""


def create_mock_incremental_item(**overrides: int | float | None) -> GCStatsInfo:
    """A record with every sub-phase set.

    The same builder as :func:`create_mock_stats_item`, which sets none of
    them; pass ``field=None`` to drop one back off.
    """
    # The merged mapping is `int | float | None`; each keyword it lands on
    # is narrower than that, and only the call site knows which.
    return create_mock_stats_item(**{**SUB_PHASES, **overrides})  # type: ignore[arg-type]


def create_jsonl_record(
    pid: int = 123,
    gen: int = 0,
    iid: int = 1,
    ts_start: int = 1_000_000,
    ts_stop: int = 2_000_000,
    heap_size: int = 1000,
    collections: int = 1,
    collected: int = 100,
    uncollectable: int = 0,
    candidates: int = 0,
    duration: float = 1.0,
) -> dict[str, int | float]:
    return {
        PID: pid,
        GEN: gen,
        IID: iid,
        TS_START: ts_start,
        TS_STOP: ts_stop,
        HEAP_SIZE: heap_size,
        COLLECTIONS: collections,
        COLLECTED: collected,
        UNCOLLECTABLE: uncollectable,
        CANDIDATES: candidates,
        DURATION: duration,
    }


def read_jsonl_file(path: Path) -> list[JsonlRecord]:
    """Every record in a JSONL file, blank lines skipped.

    An empty file gives an empty list, which is what a flush-threshold test
    asserts on.
    """
    assert path.exists(), f"File {path} does not exist"

    data: list[JsonlRecord] = []
    with open(path, encoding=ENCODING) as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            assert isinstance(obj, dict), f"Line {line_no} in JSONL file should be a JSON object, got {type(obj)}"
            data.append(obj)
    return data


# A gcmon launched by a test ends in a second or two. The bound is a watchdog:
# a run that ignores its duration fails the test where it would hang the suite.
SUBPROCESS_WATCHDOG: float = 60.0

# Loading a trace pays for a process launch and a parse, so a test that
# queries one waits on the slowest leg, not the median.
_TRACE_PROCESSOR_TIMEOUT: int = 300


@contextmanager
def open_trace_processor(path: Path | str) -> Iterator[TraceProcessor]:
    """Load *path* into a trace processor, closed when the caller is done.

    The one place the suite says which processor it drives, pinned by
    `tests.perfetto_prebuilt` rather than taken from the `perfetto` package.
    """
    config = TraceProcessorConfig(bin_path=trace_processor_bin(), load_timeout=_TRACE_PROCESSOR_TIMEOUT)
    tp = TraceProcessor(trace=str(path), config=config)
    try:
        yield tp
    finally:
        tp.close()


def misplaced_end_events(tp: TraceProcessor) -> int:
    """How many slice ENDs the trace processor threw away for want of a
    BEGIN to close: its own ``misplaced_end_event`` counter, of severity
    ``data_loss``.

    Exactly one row, so a processor that renames the counter fails every
    caller here. A missing row read as zero would pass them all.
    """
    [row] = tp.query("SELECT value FROM stats WHERE name = 'misplaced_end_event'")
    return int(row.value)


def perfetto_packets(content: bytes) -> list[TracePacket]:
    """Every ``TracePacket`` in a serialized trace, in file order.

    A batch carries its packets compressed, and a file may mix either encoding
    with plain packets. Read through Perfetto's own generated schema rather
    than gcmon's constants, so a wrong field number fails here (ADR-0001).
    """
    trace = Trace()
    trace.ParseFromString(content)
    packets: list[TracePacket] = []
    for packet in trace.packet:
        if packet.HasField("zstd_compressed_packets"):
            assert zstd is not None, "a zstd batch needs a CPython built with libzstd"
            packets.extend(perfetto_packets(zstd.decompress(packet.zstd_compressed_packets)))
        elif packet.HasField("compressed_packets"):
            packets.extend(perfetto_packets(zlib.decompress(packet.compressed_packets)))
        else:
            packets.append(packet)
    return packets


def assert_valid_perfetto_trace(file_path: Path) -> list[TracePacket]:
    """Validate that a file is a Perfetto trace, and return its packets.

    Args:
        file_path: Path to the ``.pftrace`` file to validate.

    Returns:
        Every ``TracePacket`` in the file, in file order.

    Raises:
        AssertionError: If the file is missing, empty, or carries no packets.
    """
    assert file_path.exists(), f"File {file_path} does not exist"

    content = file_path.read_bytes()
    assert content, f"Perfetto trace {file_path} is empty"

    packets = perfetto_packets(content)
    assert packets, f"Perfetto trace {file_path} carries no packets"
    return packets


def assert_is_instant_msg(msg: JsonlRecord, **expected: str | int) -> None:
    assert msg[TYPE] == "i"

    for key, value in expected.items():
        assert msg[key] == value
