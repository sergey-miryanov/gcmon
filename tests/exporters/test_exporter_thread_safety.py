"""Thread-safety stress tests for exporters."""

from __future__ import annotations

import io
import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, override

import pytest
from perfetto.protos.perfetto.trace.perfetto_trace_pb2 import (
    TracePacket,
    TrackEvent,
)

from gcmon.exporters import (
    EventsExporter,
    JsonlExporter,
    PerfettoExporter,
    StdoutExporter,
)
from gcmon.exporters.perfetto_format import (
    TrackEventType,
)
from gcmon.exporters.perfetto_process_lifetime import _PROCESS_ROW_SLICE_NAME
from gcmon.model.names import GC_PAUSE_NAME, LOST_COUNT, SAMPLED_COUNT, TYPE
from gcmon.model.protocol import TGCStatsInfo, TInstantMsg
from gcmon.support.vocabulary import ENCODING, FORMAT_JSONL, FORMAT_PERFETTO, FORMAT_STDOUT
from tests.data_helpers import create_instant_msg
from tests.helpers import (
    JsonlRecord,
    create_mock_loss_item,
    create_mock_stats_item,
    perfetto_packets,
    proc,
)

N_GC = 100
N_INSTANT = 100
PRE_FILL = 50
THREAD_JOIN_TIMEOUT = 30.0

MAIN_PID = 12345
CTRL_PID = 67890


class OutputCapture(Protocol):
    """Per-exporter output reader.

    Subclasses implement ``count_completes()`` and ``count_instants()``,
    counting "complete" (= one GC event) and "instant" (= one instant
    event) records in the exporter's output. The counting strategy is
    exporter-specific because the formats differ:

    * JSONL/Stdout  -- one record per line
    * Perfetto      -- protobuf packets with TYPE_SLICE_BEGIN /
                       TYPE_INSTANT track events
    """

    def count_completes(self) -> int: ...
    def count_instants(self) -> int: ...


class ExporterFactory(Protocol):
    name: str

    def build(self, tmp_path: Path, threshold: int) -> tuple[EventsExporter, OutputCapture]: ...


def _run_two_threads(workers: list[Callable[[], None]]) -> list[BaseException]:
    """Run all workers behind a Barrier, return any captured exceptions.

    Each worker is launched on its own thread. They block on a Barrier
    sized to the number of workers; once the last thread arrives all
    workers proceed simultaneously.
    """
    barrier = threading.Barrier(len(workers))
    captured: list[BaseException] = []
    captured_lock = threading.Lock()

    def _wrap(fn: Callable[[], None]) -> None:
        try:
            barrier.wait(timeout=THREAD_JOIN_TIMEOUT)
            fn()
        except BaseException as exc:
            with captured_lock:
                captured.append(exc)

    threads = [threading.Thread(target=_wrap, args=(w,), daemon=True) for w in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=THREAD_JOIN_TIMEOUT)
        assert not t.is_alive(), f"thread {t.name} did not finish within {THREAD_JOIN_TIMEOUT}s"

    return captured


def _make_gc_events(n: int, ts_base: int) -> list[TGCStatsInfo]:
    """N GC events with unique ``ts_start`` and ``iid``."""
    return [
        create_mock_stats_item(
            gen=0,
            iid=1000 + i,
            ts_start=ts_base + i * 1_000_000,
            ts_stop=ts_base + i * 1_000_000 + 500_000,
            collections=1,
            collected=1,
            uncollectable=0,
            candidates=1,
            heap_size=1024,
            duration=0.001,
        )
        for i in range(n)
    ]


def _make_instant_events(n: int, ts_base: int) -> list[TInstantMsg]:
    """N instant events with unique ts / name."""
    return [create_instant_msg(name=f"inst-{i}", ts=ts_base + i) for i in range(n)]


class JsonlFileCapture(OutputCapture):
    """Captures JSONL output from a file exporter."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def _lines(self) -> list[JsonlRecord]:
        if not self._path.exists():
            return []
        out: list[JsonlRecord] = []
        for ln in self._path.read_text(encoding=ENCODING).splitlines():
            if not ln:
                continue
            out.append(json.loads(ln))
        return out

    @override
    def count_completes(self) -> int:
        # GC events: no 'type' field. Instant events: type == 'i'.
        return sum(1 for e in self._lines() if e.get(TYPE) != "i")

    @override
    def count_instants(self) -> int:
        return sum(1 for e in self._lines() if e.get(TYPE) == "i")


def _get_track_event(packet: TracePacket) -> TrackEvent | None:
    if packet.HasField("track_event"):
        return packet.track_event
    return None


class PerfettoFileCapture(OutputCapture):
    """Captures Perfetto protobuf output."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def _packets(self) -> list[TracePacket]:
        if not self._path.exists():
            return []
        return perfetto_packets(self._path.read_bytes())

    def _count_event_type(self, event_type: int) -> int:
        n = 0
        for pf in self._packets():
            track_event = _get_track_event(pf)
            if track_event is not None and track_event.type == event_type:
                n += 1
        return n

    @override
    def count_completes(self) -> int:
        return self._count_event_type(TrackEventType.SLICE_BEGIN)

    @override
    def count_instants(self) -> int:
        return self._count_event_type(TrackEventType.INSTANT)

    def count_process_descriptors(self) -> int:
        """Count TRACK_DESCRIPTOR packets whose body contains a
        ``PROCESS`` sub-field (i.e., process-level track descriptors,
        not thread or counter). Useful for asserting no duplicates
        when two threads race on the same new PID."""
        n = 0
        for pf in self._packets():
            if pf.HasField("track_descriptor") and pf.track_descriptor.HasField("process"):
                n += 1
        return n


class _LockingStringIO(io.StringIO):
    """StringIO whose ``write()`` holds a lock: an interleaved line came
    from the exporter, not from the buffer."""

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()

    @override
    def write(self, s: str) -> int:
        with self._lock:
            return super().write(s)


class StdoutCapture(OutputCapture):
    """Captures StdoutExporter output via a locked StringIO buffer."""

    def __init__(self, buffer: _LockingStringIO) -> None:
        self._buffer = buffer

    def _lines(self) -> list[JsonlRecord]:
        out: list[JsonlRecord] = []
        for ln in self._buffer.getvalue().splitlines():
            if not ln:
                continue
            out.append(json.loads(ln))
        return out

    @override
    def count_completes(self) -> int:
        return sum(1 for e in self._lines() if e.get(TYPE) != "i")

    @override
    def count_instants(self) -> int:
        return sum(1 for e in self._lines() if e.get(TYPE) == "i")


class _JsonlExporterFactory:
    name = FORMAT_JSONL

    def build(self, tmp_path: Path, threshold: int) -> tuple[EventsExporter, OutputCapture]:
        path = tmp_path / f"out_{self.name}.dat"
        exporter = JsonlExporter(output_path=path, flush_threshold=threshold)
        return exporter, JsonlFileCapture(path)


class _PerfettoFactory:
    name = FORMAT_PERFETTO

    def build(self, tmp_path: Path, threshold: int) -> tuple[EventsExporter, OutputCapture]:
        path = tmp_path / f"out_{self.name}.pb"
        exporter = PerfettoExporter(output_path=path, flush_threshold=threshold)
        return exporter, PerfettoFileCapture(path)


class _StdoutFactory:
    name = FORMAT_STDOUT

    def build(
        self, tmp_path: Path, threshold: int
    ) -> tuple[EventsExporter, OutputCapture]:  # tmp_path unused but kept for symmetry
        buffer = _LockingStringIO()
        exporter = StdoutExporter(flush_threshold=threshold, output=buffer)
        return exporter, StdoutCapture(buffer)


def _all_factories() -> list[ExporterFactory]:
    return [
        _JsonlExporterFactory(),
        _PerfettoFactory(),
        _StdoutFactory(),
    ]


@pytest.fixture(params=_all_factories(), ids=lambda f: f.name)
def exporter_factory(request: pytest.FixtureRequest) -> ExporterFactory:
    param: ExporterFactory = request.param
    return param


@pytest.mark.stress
class TestExporterThreadSafety:
    def test_concurrent_add_event_and_add_instant_event_arrive(
        self, exporter_factory: ExporterFactory, tmp_path: Path
    ) -> None:
        exporter, capture = exporter_factory.build(tmp_path, threshold=10)
        gc_events = _make_gc_events(N_GC, 1_500_000_000)
        inst_events = _make_instant_events(N_INSTANT, 2_000_000_000)

        def writer_gc() -> None:
            for ev in gc_events:
                exporter.add_event(proc(MAIN_PID), ev)

        def writer_inst() -> None:
            for ev in inst_events:
                exporter.add_instant_event(proc(CTRL_PID), ev)

        captured = _run_two_threads([writer_gc, writer_inst])
        exporter.close()
        for exc in captured:
            raise exc

        assert capture.count_completes() == N_GC + (4 if isinstance(capture, PerfettoFileCapture) else 0), (
            f"[{exporter_factory.name}] expected {N_GC} complete events "
            f"(plus 2 span begins per pid for Perfetto, one on the Processes "
            f"track and one on the process's own row), "
            f"got {capture.count_completes()}"
        )
        assert capture.count_instants() == N_INSTANT, (
            f"[{exporter_factory.name}] expected {N_INSTANT} instant events, got {capture.count_instants()}"
        )

    def test_concurrent_add_event_and_close_no_data_loss(
        self, exporter_factory: ExporterFactory, tmp_path: Path
    ) -> None:
        exporter, capture = exporter_factory.build(tmp_path, threshold=5)
        for ev in _make_gc_events(PRE_FILL, 1_500_000_000):
            exporter.add_event(proc(MAIN_PID), ev)
        more = _make_gc_events(N_GC, 1_500_000_000 + 100_000_000)

        def writer() -> None:
            for ev in more:
                exporter.add_event(proc(MAIN_PID), ev)

        def closer() -> None:
            exporter.close()

        captured = _run_two_threads([writer, closer])
        for exc in captured:
            raise exc

        completes = capture.count_completes()
        # Pre-fill must always arrive. The remaining events may or may
        # not depending on whether the close beat the writer or vice
        # versa, but the total must be in [PRE_FILL, PRE_FILL + N_GC].
        # For Perfetto, add the pid's two span begins, one on the
        # Processes track and one on its own row.
        lifetime_extra = 2 if isinstance(capture, PerfettoFileCapture) else 0
        assert PRE_FILL <= completes <= PRE_FILL + N_GC + lifetime_extra, (
            f"[{exporter_factory.name}] expected between {PRE_FILL} and "
            f"{PRE_FILL + N_GC + lifetime_extra} complete events, "
            f"got {completes}"
        )

    def test_double_close_safe(self, exporter_factory: ExporterFactory, tmp_path: Path) -> None:
        """A threshold the five events stay under, so they are still in the
        buffer for the two closes to race over."""
        exporter, capture = exporter_factory.build(tmp_path, threshold=100)
        for ev in _make_gc_events(5, 1_500_000_000):
            exporter.add_event(proc(MAIN_PID), ev)

        def closer_a() -> None:
            exporter.close()

        def closer_b() -> None:
            exporter.close()

        captured = _run_two_threads([closer_a, closer_b])
        for exc in captured:
            raise exc

        assert capture.count_completes() == 5 + (2 if isinstance(capture, PerfettoFileCapture) else 0), (
            f"[{exporter_factory.name}] expected 5 complete events "
            f"(plus the pid's two span begins for Perfetto), "
            f"got {capture.count_completes()}"
        )

    def test_concurrent_add_event_same_pid(self, exporter_factory: ExporterFactory, tmp_path: Path) -> None:
        """Both threads write to the same new PID concurrently.

        JSONL and stdout have no per-pid descriptor, so the event count
        is the whole assertion for them.
        """
        exporter, capture = exporter_factory.build(tmp_path, threshold=10)
        events_a = _make_gc_events(N_GC, 1_500_000_000)
        events_b = _make_gc_events(N_GC, 1_600_000_000)  # same pid, distinct ts

        def writer_a() -> None:
            for ev in events_a:
                exporter.add_event(proc(MAIN_PID), ev)

        def writer_b() -> None:
            for ev in events_b:
                exporter.add_event(proc(MAIN_PID), ev)

        captured = _run_two_threads([writer_a, writer_b])
        exporter.close()
        for exc in captured:
            raise exc

        assert capture.count_completes() == 2 * N_GC + (2 if isinstance(capture, PerfettoFileCapture) else 0), (
            f"[{exporter_factory.name}] expected {2 * N_GC} complete events "
            f"(plus the pid's two span begins for Perfetto), "
            f"got {capture.count_completes()}"
        )
        if isinstance(capture, PerfettoFileCapture):
            proc_descs = capture.count_process_descriptors()
            assert proc_descs == 1, f"[perfetto] expected exactly 1 process descriptor, got {proc_descs}"

    def test_concurrent_add_event_and_add_instant_event_same_new_pid(
        self, exporter_factory: ExporterFactory, tmp_path: Path
    ) -> None:
        """One thread calls ``add_event``, the other
        ``add_instant_event``, both for the same brand-new PID.

        The claim ``test_concurrent_add_event_same_pid`` makes, reached
        through two methods rather than one.
        """
        exporter, capture = exporter_factory.build(tmp_path, threshold=10)
        gc_events = _make_gc_events(N_GC, 1_500_000_000)
        inst_events = _make_instant_events(N_INSTANT, 2_000_000_000)

        def writer_gc() -> None:
            for ev in gc_events:
                exporter.add_event(proc(MAIN_PID), ev)

        def writer_inst() -> None:
            for ev in inst_events:
                exporter.add_instant_event(proc(MAIN_PID), ev)

        captured = _run_two_threads([writer_gc, writer_inst])
        exporter.close()
        for exc in captured:
            raise exc

        assert capture.count_completes() == N_GC + (2 if isinstance(capture, PerfettoFileCapture) else 0), (
            f"[{exporter_factory.name}] expected {N_GC} complete events "
            f"(plus the pid's two span begins for Perfetto), "
            f"got {capture.count_completes()}"
        )
        assert capture.count_instants() == N_INSTANT, (
            f"[{exporter_factory.name}] expected {N_INSTANT} instant events, got {capture.count_instants()}"
        )
        if isinstance(capture, PerfettoFileCapture):
            proc_descs = capture.count_process_descriptors()
            assert proc_descs == 1, f"[perfetto] expected exactly 1 process descriptor, got {proc_descs}"

    def test_post_close_add_event_does_not_crash(self, exporter_factory: ExporterFactory, tmp_path: Path) -> None:
        """Calling ``add_event`` after ``close()`` must not raise.

        A dropped event, and not an exception out of a monitoring
        callback during shutdown. See ADR-0008.
        """
        exporter, _capture = exporter_factory.build(tmp_path, threshold=10)
        exporter.close()

        exporter.add_event(proc(MAIN_PID), create_mock_stats_item(iid=1000))
        exporter.add_instant_event(proc(MAIN_PID), create_instant_msg(name="post-close", ts=999_999))


@pytest.mark.stress
class TestPerfettoExporterCmdlinePath:
    """A process the registry read no command line for."""

    def test_a_process_with_no_cmdline_still_gets_a_descriptor(self, tmp_path: Path) -> None:
        """A process carrying no command line still gets a process
        descriptor, with no cmdline entries on it.
        """
        path = tmp_path / "trace.pb"
        exporter = PerfettoExporter(output_path=path, flush_threshold=1)
        exporter.add_event(proc(MAIN_PID), create_mock_stats_item())
        exporter.close()

        capture = PerfettoFileCapture(path)
        # 1 GC pause slice begin on the thread track + the pid's two span
        # begins, one on the Processes track and one on its own row.
        assert capture.count_completes() == 3
        assert capture.count_process_descriptors() == 1

        # The process track carries no joined description either.
        for packet in capture._packets():
            if not packet.HasField("track_descriptor"):
                continue
            td = packet.track_descriptor
            if not td.HasField("process"):
                continue
            assert len(td.process.cmdline) == 0, f"expected no cmdline entries, got {len(td.process.cmdline)}"


@pytest.mark.stress
class TestMetaDedupRaceClosed:
    """Two threads at a brand-new pid put exactly one process descriptor in
    the file.

    The race this closes was a TOCTOU in the producer, between
    ``pid not in self._pids`` and ``self._pids.add(pid)``, which could put two
    process descriptors in a trace under load. It closed by deletion
    rather than by locking: no producer decides what a batch's descriptors
    are any more. The dedup lives in ``PerfettoTrackState``, reached only
    through ``write_events`` and ``record_process_liveness``, both already
    under ``_io_lock``.
    """

    def test_perfetto_two_threads_same_new_pid_produces_single_descriptor(self, tmp_path: Path) -> None:
        path = tmp_path / "out_perfetto.pb"
        exporter = PerfettoExporter(output_path=path, flush_threshold=10)
        events_a = _make_gc_events(N_GC, 1_500_000_000)
        events_b = _make_gc_events(N_GC, 1_600_000_000)

        def writer_a() -> None:
            for ev in events_a:
                exporter.add_event(proc(MAIN_PID), ev)

        def writer_b() -> None:
            for ev in events_b:
                exporter.add_event(proc(MAIN_PID), ev)

        captured = _run_two_threads([writer_a, writer_b])
        exporter.close()
        for exc in captured:
            raise exc

        capture = PerfettoFileCapture(path)
        proc_descs = capture.count_process_descriptors()
        assert proc_descs == 1, f"expected exactly 1 process descriptor, got {proc_descs}"


@pytest.mark.stress
class TestCaptureTotalsUnderLoad:
    """``sampled_count`` and ``lost_count`` on the ``Lifetime`` bar count
    what reached the trace, whatever thread put it there.

    Both are folded in during the convert pass, which runs under
    ``_io_lock``, so a record and a loss interval arriving on two threads
    take the same route as the events they came in with.
    """

    _LOST_PER_INTERVAL = 3

    def _bar_totals(self, path: Path) -> tuple[int, int]:
        """``(sampled_count, lost_count)`` off the one ``Lifetime`` BEGIN."""
        totals: list[tuple[int, int]] = []
        for packet in perfetto_packets(path.read_bytes()):
            track_event = _get_track_event(packet)
            if track_event is None or track_event.name != _PROCESS_ROW_SLICE_NAME:
                continue
            annotations = {ann.name: ann.int_value for ann in track_event.debug_annotations}
            totals.append((annotations[SAMPLED_COUNT], annotations[LOST_COUNT]))
        assert len(totals) == 1, f"expected one Lifetime bar, got {len(totals)}"
        return totals[0]

    def _count_pauses_before_the_bar(self, path: Path) -> int:
        """The ``GC Pause`` BEGINs written ahead of the ``Lifetime`` bar.

        One per record, and every batch is appended under ``_io_lock``, so
        file order is the order the encoder converted them in. A writer that
        outran ``close()`` puts its records past the bar, where they are in
        the trace and outside the count by construction.
        """
        pauses = 0
        for packet in perfetto_packets(path.read_bytes()):
            track_event = _get_track_event(packet)
            if track_event is None:
                continue
            if track_event.name == _PROCESS_ROW_SLICE_NAME:
                return pauses
            if track_event.type == TrackEventType.SLICE_BEGIN and track_event.name.startswith(GC_PAUSE_NAME):
                pauses += 1
        raise AssertionError("the trace carries no Lifetime bar")

    def test_records_and_losses_on_two_threads_lose_no_increment(self, tmp_path: Path) -> None:
        path = tmp_path / "totals.pb"
        exporter = PerfettoExporter(output_path=path, flush_threshold=5)
        events = _make_gc_events(N_GC, 1_500_000_000)
        losses = [
            create_mock_loss_item(ts_start=ts, ts_stop=ts + 1_000, lost_count=self._LOST_PER_INTERVAL)
            for ts in range(1_500_000_000, 1_500_000_000 + N_GC * 1_000, 1_000)
        ]

        def writer() -> None:
            for ev in events:
                exporter.add_event(proc(MAIN_PID), ev)

        def loser() -> None:
            for loss in losses:
                exporter.add_loss_event(proc(MAIN_PID), loss)

        captured = _run_two_threads([writer, loser])
        exporter.close()
        for exc in captured:
            raise exc

        assert self._bar_totals(path) == (N_GC, N_GC * self._LOST_PER_INTERVAL)

    def test_a_close_racing_the_writer_counts_every_record_it_drew_over(self, tmp_path: Path) -> None:
        """Whatever the close beat the writer to, the bar counts exactly the
        records converted ahead of it: no increment dropped and none counted
        twice."""
        path = tmp_path / "raced.pb"
        exporter = PerfettoExporter(output_path=path, flush_threshold=5)
        for ev in _make_gc_events(PRE_FILL, 1_500_000_000):
            exporter.add_event(proc(MAIN_PID), ev)
        more = _make_gc_events(N_GC, 1_500_000_000 + 100_000_000)

        def writer() -> None:
            for ev in more:
                exporter.add_event(proc(MAIN_PID), ev)

        def closer() -> None:
            exporter.close()

        captured = _run_two_threads([writer, closer])
        for exc in captured:
            raise exc

        sampled, _lost = self._bar_totals(path)
        assert sampled == self._count_pauses_before_the_bar(path)
        assert PRE_FILL <= sampled <= PRE_FILL + N_GC
