"""What the hook does: mark where each benchmark ran, and refuse to run blind.

The marks are driven through a real ``ControlClient`` into a real
``ControlServer``, the highest seam that sees one end to end.
"""

import os
import subprocess
import sys
import time
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import NamedTuple, override
from unittest.mock import patch

import pytest

from gcmon.control.control_server import CONTROL_ADDRESS_ENV, ControlServer, _make_address
from gcmon.exporters.perfetto_process_lifetime import process_track_name
from gcmon.model.marks import Mark, Side, parse_mark
from gcmon.model.names import NAME
from gcmon.model.process import Process
from gcmon.model.protocol import TGCStatsInfo, TInstantMsg
from gcmon.monitoring.events_reader import RemoteEventsReader, TargetUnavailable
from gcmon.pyperf.hook import (
    ENV_PYPERF_HOOK_CONTROL_TIMEOUT,
    GCMonitorHook,
    _get_env_pyperf_hook_control_timeout,
    gcmon_hook,
)
from tests.helpers import MockExporter, monitored, open_trace_processor, proc
from tests.monitoring.test_events_reader import target_executable


class Marked(NamedTuple):
    """One mark as the exporter saw it, with the pid it landed on."""

    pid: int
    mark: Mark
    ts: int


class Sink(NamedTuple):
    server: ControlServer
    exporter: MockExporter

    def marks(self) -> list[Marked]:
        found = []
        for pid, msg in list(self.exporter.instant_events):
            mark = parse_mark(msg.name)
            if mark is not None:
                found.append(Marked(pid, mark, msg.ts))
        return found

    def wait_for(self, count: int, timeout: float = 5.0) -> list[Marked]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = self.marks()
            if len(found) >= count:
                return found
            time.sleep(0.01)
        return self.marks()


@pytest.fixture
def sink(monkeypatch: pytest.MonkeyPatch) -> Generator[Sink]:
    """A listening control plane, reached the way a worker reaches one."""
    exporter = MockExporter()
    server = ControlServer(exporter, monitored(os.getpid()))
    server.start()
    monkeypatch.setenv(CONTROL_ADDRESS_ENV, server.address)
    try:
        yield Sink(server, exporter)
    finally:
        server.close()


def _sides(marks: Sequence[Marked]) -> list[str]:
    return [m.mark.side for m in marks]


class TestAccumulateAndLand:
    def test_no_mark_crosses_while_the_benchmark_runs(self, sink: Sink) -> None:
        """Waited for, since the server reads on a poll and a mark sent at
        the region's end would not have arrived the instant after it."""
        hook = gcmon_hook()

        with hook:
            pass

        assert sink.wait_for(1, timeout=0.5) == [], "a mark crossed a process boundary before teardown"

    def test_two_regions_land_as_four_instants_at_teardown(self, sink: Sink) -> None:
        hook = gcmon_hook()
        with hook:
            pass
        with hook:
            pass

        hook.teardown({NAME: "bm_base64"})

        marks = sink.wait_for(4)
        assert _sides(marks) == [Side.BEGIN, Side.END, Side.BEGIN, Side.END]
        assert [m.mark.bench for m in marks] == ["bm_base64"] * 4
        first, second = marks[0].mark.region, marks[2].mark.region
        assert [m.mark.region for m in marks] == [first, first, second, second]
        assert second == first + 1

    def test_the_marks_carry_the_benchmark_s_own_instants(self, sink: Sink) -> None:
        hook = gcmon_hook()

        before = time.monotonic_ns()
        with hook:
            time.sleep(0.01)
        after = time.monotonic_ns()

        time.sleep(0.05)
        sent_no_earlier_than = time.monotonic_ns()
        hook.teardown({NAME: "bm_base64"})

        begin, end = sink.wait_for(2)
        assert before <= begin.ts < end.ts <= after
        assert end.ts < sent_no_earlier_than, "the mark was stamped at send time, not at the benchmark"

    def test_the_marks_land_on_the_worker_s_pid(self, sink: Sink) -> None:
        hook = gcmon_hook()
        with hook:
            pass
        hook.teardown({NAME: "bm_base64"})

        assert {m.pid for m in sink.wait_for(2)} == {os.getpid()}

    def test_a_name_that_is_not_a_field_is_sanitized(self, sink: Sink) -> None:
        hook = gcmon_hook()
        with hook:
            pass
        hook.teardown({NAME: "bm:odd name"})

        assert {m.mark.bench for m in sink.wait_for(2)} == {"bm_odd_name"}


class TestRegionNumbering:
    def test_a_second_hook_instance_continues_the_numbering(self, sink: Sink) -> None:
        first = gcmon_hook()
        with first:
            pass
        first.teardown({NAME: "bm_base64"})
        assert sink.wait_for(2)

        second = gcmon_hook()
        with second:
            pass
        second.teardown({NAME: "bm_base64"})

        regions = [m.mark.region for m in sink.wait_for(4)]
        assert regions[2] == regions[0] + 1, "the second instance restarted the count and reused a mark name"

    def test_each_hook_counts_its_own_regions_from_one(self, sink: Sink) -> None:
        """Where the phase count restarts is where pyperf started a new phase."""
        warmups = gcmon_hook()
        with warmups:
            pass
        warmups.teardown({NAME: "bm_base64"})

        values = gcmon_hook()
        for _ in range(3):
            with values:
                pass
        values.teardown({NAME: "bm_base64"})

        # Each hook has its own connection, and the server reads one message
        # per connection per pass, so arrival order interleaves the two. A
        # reader sorts by timestamp (ADR-0011) and so does this.
        marks = sorted(sink.wait_for(8), key=lambda m: m.ts)

        assert [m.mark.phase_region for m in marks] == [1, 1, 1, 1, 2, 2, 3, 3]

        # Absolute values depend on what the process counted before this.
        regions = [m.mark.region for m in marks]
        opened = regions[0]
        assert regions == [opened + n // 2 for n in range(8)]

    def test_a_region_that_never_closed_takes_no_number(self, sink: Sink) -> None:
        """The landed regions have no gaps in their numbering."""
        first = gcmon_hook()
        with first:
            pass
        first.teardown({NAME: "bm_base64"})
        started_at = sink.wait_for(2)[0].mark.region

        abandoned = gcmon_hook()
        abandoned.__enter__()
        abandoned.teardown({NAME: "bm_base64"})

        third = gcmon_hook()
        with third:
            pass
        third.teardown({NAME: "bm_base64"})

        regions = [m.mark.region for m in sink.wait_for(4)]
        assert regions[2] == started_at + 1

    def test_regions_of_one_instance_are_numbered_in_order(self, sink: Sink) -> None:
        hook = gcmon_hook()
        for _ in range(3):
            with hook:
                pass
        hook.teardown({NAME: "bm_base64"})

        regions = [m.mark.region for m in sink.wait_for(6)]
        assert regions == sorted(regions)
        assert regions[0] == regions[1] < regions[2] == regions[3] < regions[4] == regions[5]


class TestTheMarksInATrace:
    """What the operator opens: marks as instants on the worker's own process."""

    def test_the_marks_reach_a_perfetto_trace_on_the_worker_s_process(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:

        from gcmon.exporters.perfetto_exporter import PerfettoExporter

        class CountingExporter(PerfettoExporter):
            """The trace the operator opens, plus a way to wait for it."""

            instants = 0

            @override
            def add_instant_event(self, process: Process, item: TInstantMsg) -> None:
                super().add_instant_event(process, item)
                self.instants += 1

        path = tmp_path / "marks.pftrace"
        exporter = CountingExporter(output_path=path, flush_threshold=1000)
        server = ControlServer(exporter, monitored(os.getpid()))
        server.start()
        try:
            monkeypatch.setenv(CONTROL_ADDRESS_ENV, server.address)
            hook = gcmon_hook()
            with hook:
                pass
            hook.teardown({NAME: "bm_base64"})
            deadline = time.monotonic() + 5
            while exporter.instants < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
        finally:
            server.close()
            exporter.close()

        with open_trace_processor(path) as tp:
            rows = list(
                tp.query(
                    "SELECT s.name AS name FROM slice s "
                    "JOIN process_track pt ON s.track_id = pt.id "
                    "JOIN process p ON pt.upid = p.upid "
                    f"WHERE p.name = '{process_track_name(proc(os.getpid()))}' AND s.dur = 0 AND s.name LIKE 'gcmon:%' "
                    "ORDER BY s.ts"
                )
            )

        marks = [parse_mark(row.name) for row in rows]
        assert len(marks) == 2, f"expected one region's pair of marks, got {[row.name for row in rows]}"
        assert marks[0] is not None and marks[1] is not None
        assert marks[0].side == Side.BEGIN
        assert marks[1].side == Side.END
        assert marks[0].bench == marks[1].bench == "bm_base64"
        assert marks[0].region == marks[1].region


class TestAnUnfinishedRegion:
    def test_a_region_whose_exit_never_ran_lands_nothing(self, sink: Sink) -> None:
        hook = gcmon_hook()

        hook.__enter__()
        hook.teardown({NAME: "bm_base64"})

        assert sink.wait_for(1, timeout=0.5) == [], "half a region reached the trace"

    def test_a_finished_region_before_an_unfinished_one_still_lands(self, sink: Sink) -> None:
        hook = gcmon_hook()

        with hook:
            pass
        hook.__enter__()
        hook.teardown({NAME: "bm_base64"})

        assert _sides(sink.wait_for(2)) == [Side.BEGIN, Side.END]


class TestTheHookDoesNothingElse:
    def test_the_hook_spawns_no_process(self, sink: Sink, monkeypatch: pytest.MonkeyPatch) -> None:
        """The monitor is the operator's, started once over the whole suite."""
        import subprocess

        def refuse(*args: object, **kwargs: object) -> None:
            raise AssertionError("the hook spawned a process")

        monkeypatch.setattr(subprocess, "Popen", refuse)

        hook = gcmon_hook()
        with hook:
            pass
        hook.teardown({NAME: "bm_base64"})

        assert len(sink.wait_for(2)) == 2

    def test_the_hook_writes_no_file(self, sink: Sink, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)

        hook = gcmon_hook()
        with hook:
            pass
        hook.teardown({NAME: "bm_base64"})
        assert len(sink.wait_for(2)) == 2

        assert list(tmp_path.iterdir()) == []

    def test_teardown_adds_no_key_to_the_metadata(self, sink: Sink) -> None:
        hook = gcmon_hook()
        with hook:
            pass

        metadata: dict[str, object] = {NAME: "bm_base64", "loops": 4}
        hook.teardown(metadata)

        assert metadata == {NAME: "bm_base64", "loops": 4}


# A target that collects on demand. The one in ``tests/test_events_reader``
# fills its rings while the interpreter starts and then goes quiet, which
# proves a ring can be read but says nothing about when a record was stamped.
_COLLECTING_TARGET = """
import gc, time

while True:
    a = {}
    b = {"a": a}
    a["b"] = b
    del a, b
    gc.collect()
    time.sleep(0.01)
"""


@contextmanager
def _collecting_target(timeout: float = 20.0) -> Generator[tuple[RemoteEventsReader, int]]:
    """A live process writing GC records, and a reader already attached."""
    proc = subprocess.Popen(
        [target_executable(), "-c", _COLLECTING_TARGET],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    reader = RemoteEventsReader()
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                reader.read(proc.pid)
                break
            except TargetUnavailable:
                if time.monotonic() >= deadline:
                    raise AssertionError(f"target {proc.pid} never became readable") from None
                time.sleep(0.05)
        yield reader, proc.pid
    finally:
        proc.kill()
        proc.wait()


def _read_a_record_stamped_after(
    reader: RemoteEventsReader, pid: int, instant: int, timeout: float = 5.0
) -> Sequence[TGCStatsInfo]:
    """Poll until the target has finished a collection later than *instant*.

    Past the deadline it returns what it read, and the caller's assertion
    says what is wrong with it.
    """
    deadline = time.monotonic() + timeout
    while True:
        records = reader.read(pid)
        if any(record.ts_stop > instant for record in records) or time.monotonic() >= deadline:
            return records
        time.sleep(0.01)


class TestTheClockBehindTheMarks:
    """A mark and a GC record have to land on one timeline.

    The hook stamps a mark with ``time.monotonic_ns()``; CPython stamps a
    record from its own clock. Nothing downstream notices if those two stop
    being the same clock, and the marks land in the wrong place.
    """

    def test_a_record_carries_the_clock_a_mark_is_stamped_from(self) -> None:
        with _collecting_target() as (reader, pid):
            before = time.monotonic_ns()
            records = _read_a_record_stamped_after(reader, pid, before)
            after = time.monotonic_ns()

        newest = max(record.ts_stop for record in records)
        assert before < newest < after, (
            "a GC record is not stamped from the clock a mark is stamped from: "
            f"{newest} is outside the window [{before}, {after}] it was read in"
        )


class TestNoMonitorIsARefusal:
    """A hook that only annotates has nothing to do without a monitor.

    pyperf catches its own ``HookError``, so the run stops on the first worker
    rather than finishing a suite that recorded nothing.
    """

    def test_no_control_address_refuses_the_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(CONTROL_ADDRESS_ENV, raising=False)

        with pytest.raises(Exception) as caught:
            gcmon_hook()

        assert "gcmon run" in str(caught.value)

    def test_an_address_nobody_is_listening_on_refuses_the_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(CONTROL_ADDRESS_ENV, _make_address("nobody-is-listening"))
        monkeypatch.setenv(ENV_PYPERF_HOOK_CONTROL_TIMEOUT, "0.2")

        with pytest.raises(Exception) as caught:
            gcmon_hook()

        assert "gcmon run" in str(caught.value)

    def test_the_refusal_is_the_type_pyperf_catches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pyperf._hooks import HookError

        monkeypatch.delenv(CONTROL_ADDRESS_ENV, raising=False)

        with pytest.raises(HookError):
            gcmon_hook()

    def test_the_run_still_fails_if_pyperf_moves_the_type(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Losing the refusal would lose the run instead."""
        monkeypatch.delenv(CONTROL_ADDRESS_ENV, raising=False)
        monkeypatch.setitem(sys.modules, "pyperf._hooks", None)

        with pytest.raises(Exception) as caught:
            gcmon_hook()

        assert "gcmon run" in str(caught.value)

    def test_a_hook_is_built_with_pyperf_absent(self, sink: Sink, monkeypatch: pytest.MonkeyPatch) -> None:
        """Nothing imports pyperf on the path a working hook takes."""
        monkeypatch.setitem(sys.modules, "pyperf", None)
        monkeypatch.setitem(sys.modules, "pyperf._hooks", None)

        assert isinstance(gcmon_hook(), GCMonitorHook)


class TestGetEnvControlTimeout:
    def test_default_value(self) -> None:
        with patch.dict(os.environ, clear=True):
            assert _get_env_pyperf_hook_control_timeout() == 10.0

    def test_custom_value(self) -> None:
        with patch.dict(os.environ, {ENV_PYPERF_HOOK_CONTROL_TIMEOUT: "30"}):
            assert _get_env_pyperf_hook_control_timeout() == 30.0

    def test_invalid_value_returns_default(self) -> None:
        with patch.dict(os.environ, {ENV_PYPERF_HOOK_CONTROL_TIMEOUT: "not-a-number"}):
            assert _get_env_pyperf_hook_control_timeout() == 10.0
