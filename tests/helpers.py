from __future__ import annotations

import json
import threading
import zlib
from collections.abc import Callable, Iterator, Sequence, Set
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import override

import msgspec
from perfetto.protos.perfetto.trace.perfetto_trace_pb2 import Trace, TracePacket
from perfetto.trace_processor import TraceProcessor, TraceProcessorConfig

from gcmon.exporters.exporter import EventsExporter
from gcmon.model.data import GCStatsInfo, GenLoss, LossMsg
from gcmon.model.process import Process
from gcmon.model.protocol import TGCStatsInfo, TInstantMsg, TLossMsg
from gcmon.model.trace_event import InterpreterTrack, LossTrack, ProcessTrack
from gcmon.monitoring.events_reader import EventsReader
from gcmon.monitoring.monitor import EventsMonitor
from gcmon.monitoring.process_registry import ProcessRegistry
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
DefaultsValue = Path | float | None | int | str | bool

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
    "create_mock_new_incremental_item",
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
    It supports event-based synchronization for tests.
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
        self._close_called = False
        self._event_added = threading.Event()

    @override
    def add_event(self, process: Process, item: TGCStatsInfo) -> None:
        """Add an event to the exporter.

        Args:
            process: The process the record came from.
            item: The stats item to add.
        """
        self.events.append(item)
        self.events_by_pid.setdefault(process.pid, []).append(item)
        self._event_added.set()  # Signal that event was added

    @override
    def add_loss_event(self, process: Process, item: TLossMsg) -> None:
        """Record a loss window the monitor's arithmetic produced.

        Does not set ``_event_added``: a caller blocking on
        ``wait_for_event`` is waiting for a GC record, and releasing it on a
        loss record would let it wake and assert against an empty ``events``.
        """
        self.loss_events.append((process.pid, item))

    @override
    def add_process_liveness(self, processes: Set[Process], ts_ns: int) -> None:
        """Record one tick's liveness observation, as the pids it named."""
        self.liveness.append(({process.pid for process in processes}, ts_ns))

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
        self._event_added.set()

    @override
    def close(self) -> None:
        """Close the exporter."""
        self._close_called = True

    def wait_for_event(self, timeout: float = 1.0) -> bool:
        """Wait for an event to be added.

        Args:
            timeout: Maximum time to wait in seconds.

        Returns:
            True if an event was added within timeout, False otherwise.
        """
        result = self._event_added.wait(timeout=timeout)
        self._event_added.clear()
        return result


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
) -> GCStatsInfo:
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
    )


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


def create_mock_incremental_item(
    gen: int = 0,
    iid: int = 0,
    ts_start: int = 1_500_000_000,
    ts_stop: int = 1_505_000_000,
    heap_size: int = 52428800,
    collections: int = 50,
    collected: int = 200,
    uncollectable: int = 10,
    candidates: int = 40,
    duration: float = 0.005,
    increment_size: int | None = 1000,
    alive_size: int | None = 800,
    ts_mark_alive_start: int | None = 1_500_000_000,
    ts_mark_alive_stop: int | None = 1_501_000_000,
    ts_fill_increment_start: int | None = 1_501_000_000,
    ts_fill_increment_stop: int | None = 1_502_000_000,
    ts_deduce_unreachable_start: int | None = 1_502_000_000,
    ts_deduce_unreachable_stop: int | None = 1_503_000_000,
    ts_handle_weakref_callbacks_start: int | None = 1_503_000_000,
    ts_handle_weakref_callbacks_stop: int | None = 1_504_000_000,
    ts_finalize_garbage_stop: int | None = 1_505_000_000,
    finalized_garbage_count: int | None = 42,
    ts_handle_resurrected_stop: int | None = 1_506_000_000,
    ts_clear_weakrefs_stop: int | None = 1_507_000_000,
    clear_weakrefs_count: int | None = 7,
    ts_delete_garbage_start: int | None = 1_508_000_000,
    ts_delete_garbage_stop: int | None = 1_509_000_000,
    deleted_garbage_count: int | None = 13,
) -> GCStatsInfo:
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
        increment_size=increment_size,
        alive_size=alive_size,
        ts_mark_alive_start=ts_mark_alive_start,
        ts_mark_alive_stop=ts_mark_alive_stop,
        ts_fill_increment_start=ts_fill_increment_start,
        ts_fill_increment_stop=ts_fill_increment_stop,
        ts_deduce_unreachable_start=ts_deduce_unreachable_start,
        ts_deduce_unreachable_stop=ts_deduce_unreachable_stop,
        ts_handle_weakref_callbacks_start=ts_handle_weakref_callbacks_start,
        ts_handle_weakref_callbacks_stop=ts_handle_weakref_callbacks_stop,
        ts_finalize_garbage_stop=ts_finalize_garbage_stop,
        finalized_garbage_count=finalized_garbage_count,
        ts_handle_resurrected_stop=ts_handle_resurrected_stop,
        ts_clear_weakrefs_stop=ts_clear_weakrefs_stop,
        clear_weakrefs_count=clear_weakrefs_count,
        ts_delete_garbage_start=ts_delete_garbage_start,
        ts_delete_garbage_stop=ts_delete_garbage_stop,
        deleted_garbage_count=deleted_garbage_count,
    )


def create_mock_new_incremental_item(
    gen: int = 0,
    iid: int = 0,
    increment_size: int | None = 1000,
    old_work: int | None = 900,
    auto_collect: int | None = 1,
    aging_threshold: int | None = 4,
    aging_spaces: int | None = 2,
    aging_next: int | None = 3,
    survivor_count: int | None = 250,
    heap_size_stop: int | None = 51_380_224,
) -> GCStatsInfo:
    """A record from a build running the new incremental collector.

    It carries no fill-increment timestamps. The two collectors do not run in
    one interpreter, and no record trips both guards.
    """
    return msgspec.structs.replace(
        create_mock_incremental_item(gen=gen, iid=iid, increment_size=increment_size),
        ts_fill_increment_start=None,
        ts_fill_increment_stop=None,
        old_work=old_work,
        auto_collect=auto_collect,
        aging_threshold=aging_threshold,
        aging_spaces=aging_spaces,
        aging_next=aging_next,
        survivor_count=survivor_count,
        heap_size_stop=heap_size_stop,
    )


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
        "pid": pid,
        "gen": gen,
        "iid": iid,
        "ts_start": ts_start,
        "ts_stop": ts_stop,
        "heap_size": heap_size,
        "collections": collections,
        "collected": collected,
        "uncollectable": uncollectable,
        "candidates": candidates,
        "duration": duration,
    }


def assert_valid_jsonl_format(file_path: Path) -> list[JsonlRecord]:
    """Validate that a file contains valid JSONL format (one JSON object per line).

    Args:
        file_path: Path to the JSONL file to validate.

    Returns:
        List of parsed event dictionaries.

    Raises:
        AssertionError: If the file is not valid JSONL format.
    """
    assert file_path.exists(), f"File {file_path} does not exist"

    data: list[JsonlRecord] = []
    with open(file_path, encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            assert isinstance(obj, dict), f"Line {line_no} in JSONL file should be a JSON object, got {type(obj)}"
            data.append(obj)

    assert len(data) > 0, f"JSONL file {file_path} is empty"
    return data


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
    assert msg["type"] == "i"

    for key, value in expected.items():
        assert msg[key] == value
