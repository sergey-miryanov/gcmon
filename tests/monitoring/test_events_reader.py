"""The reader that attaches to a process once and reads it many times.

Two kinds of test here. The lifetime rules of ADR-0020 -- attach once, never
remember a failed attach, let go on any failed read -- are asserted against a
counting stand-in for ``GCMonitor``, because they are claims about *how many
times* gcmon attaches and a stopwatch answers that question badly. A smaller
set drives the real ``_remote_debugging`` against real subprocesses, because
nothing else proves the adapter matches the API it adapts.
"""

import subprocess
import sys
import time
from _remote_debugging import GCMonitor
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from typing import Any

import pytest

from gcmon.model.protocol import TGCStatsInfo
from gcmon.monitoring.events_reader import RemoteEventsReader, TargetUnavailable
from tests.helpers import create_mock_stats_item

# A pid no process holds. Only used where the reader must fail before it
# reaches the operating system at all.
MISSING_PID = 999_999


class SpyMonitor:
    """Stands in for ``_remote_debugging.GCMonitor``, counting attachments.

    Calling it is attaching, which is what the real class does too. Each call
    records ``(pid, debug)`` and hands back an attachment whose reads land in the
    same ledger, so a test can say how many times gcmon paid to find a process
    rather than how long it took.

    ``attach_errors`` and ``read_errors`` are consumed one per call, so a test
    scripts the failure it wants and the calls after it succeed.
    """

    def __init__(self) -> None:
        self.attaches: list[tuple[int, bool]] = []
        self.reads: list[int] = []
        self.attach_errors: list[BaseException] = []
        self.read_errors: list[BaseException] = []
        self.records: Sequence[TGCStatsInfo] = ()

    def __call__(self, pid: int, *, debug: bool = False) -> _Attachment:
        self.attaches.append((pid, debug))
        if self.attach_errors:
            raise self.attach_errors.pop(0)
        return _Attachment(self, pid)


class _Attachment:
    def __init__(self, spy: SpyMonitor, pid: int) -> None:
        self._spy = spy
        self._pid = pid

    def get_gc_stats(self, *, all_interpreters: bool) -> Sequence[TGCStatsInfo]:
        assert all_interpreters, "gcmon always reads every interpreter"
        self._spy.reads.append(self._pid)
        if self._spy.read_errors:
            raise self._spy.read_errors.pop(0)
        return self._spy.records


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> SpyMonitor:
    spy = SpyMonitor()
    monkeypatch.setattr("gcmon.monitoring.events_reader.GCMonitor", spy)
    return spy


@pytest.fixture
def remote_reader() -> RemoteEventsReader:
    return RemoteEventsReader()


def attached_pids(spy: SpyMonitor) -> list[int]:
    return [pid for pid, _ in spy.attaches]


class TestAttachOncePerPid:
    def test_a_second_read_of_the_same_pid_does_not_attach_again(
        self, remote_reader: RemoteEventsReader, spy: SpyMonitor
    ) -> None:
        remote_reader.read(7)
        remote_reader.read(7)
        remote_reader.read(7)

        assert attached_pids(spy) == [7]
        assert spy.reads == [7, 7, 7]

    def test_each_pid_gets_its_own_attachment(self, remote_reader: RemoteEventsReader, spy: SpyMonitor) -> None:
        remote_reader.read(7)
        remote_reader.read(8)
        remote_reader.read(7)
        remote_reader.read(8)

        assert attached_pids(spy) == [7, 8]

    def test_debug_is_on_and_every_interpreter_is_read(
        self, remote_reader: RemoteEventsReader, spy: SpyMonitor
    ) -> None:
        remote_reader.read(7)

        # debug=True is what the free function this replaced hardcoded, so the
        # exception type gcmon catches did not change with the swap. ADR-0020.
        assert spy.attaches == [(7, True)]

    def test_what_the_target_returned_is_what_the_caller_gets(
        self, remote_reader: RemoteEventsReader, spy: SpyMonitor
    ) -> None:
        spy.records = [create_mock_stats_item(gen=1, collections=3)]

        records = remote_reader.read(7)

        assert [(r.gen, r.collections) for r in records] == [(1, 3)]


class TestFailedAttachIsNeverRemembered:
    def test_a_pid_whose_first_attach_fails_is_attached_again_on_the_next_read(
        self, remote_reader: RemoteEventsReader, spy: SpyMonitor
    ) -> None:
        """A target that has not started yet must be retried, which is the
        whole point of the startup timeout."""
        spy.attach_errors = [RuntimeError("Failed to initialize process handle")]

        with pytest.raises(TargetUnavailable):
            remote_reader.read(7)

        remote_reader.read(7)

        assert attached_pids(spy) == [7, 7]
        assert spy.reads == [7]

    def test_a_failing_attach_is_retried_every_time(self, remote_reader: RemoteEventsReader, spy: SpyMonitor) -> None:
        spy.attach_errors = [RuntimeError("not yet"), RuntimeError("not yet"), RuntimeError("not yet")]

        for _ in range(3):
            with pytest.raises(TargetUnavailable):
                remote_reader.read(7)

        assert attached_pids(spy) == [7, 7, 7]


class TestFailedReadDropsTheAttachment:
    def test_the_next_read_after_a_failure_attaches_again(
        self, remote_reader: RemoteEventsReader, spy: SpyMonitor
    ) -> None:
        remote_reader.read(7)
        spy.read_errors = [RuntimeError("Failed to read interpreter state address")]

        with pytest.raises(TargetUnavailable):
            remote_reader.read(7)

        remote_reader.read(7)

        assert attached_pids(spy) == [7, 7]

    def test_a_failure_gcmon_does_not_translate_still_drops_the_attachment(
        self, remote_reader: RemoteEventsReader, spy: SpyMonitor
    ) -> None:
        """Any failed read, not only an unavailable target. An attachment holds
        offsets derived from a process gcmon can no longer vouch for."""
        remote_reader.read(7)
        spy.read_errors = [ValueError("something else entirely")]

        with pytest.raises(ValueError):
            remote_reader.read(7)

        remote_reader.read(7)

        assert attached_pids(spy) == [7, 7]

    def test_one_pid_failing_leaves_the_others_attached(
        self, remote_reader: RemoteEventsReader, spy: SpyMonitor
    ) -> None:
        remote_reader.read(7)
        remote_reader.read(8)
        spy.read_errors = [RuntimeError("gone")]

        with pytest.raises(TargetUnavailable):
            remote_reader.read(7)

        remote_reader.read(7)
        remote_reader.read(8)

        assert attached_pids(spy) == [7, 8, 7]


class TestTheExceptionTaxonomy:
    @pytest.mark.parametrize(
        "error",
        [
            RuntimeError("Failed to read interpreter state address"),
            ProcessLookupError(3, "No such process"),
            PermissionError(13, "Permission denied"),
        ],
        ids=["runtime", "no-such-process", "permission"],
    )
    def test_a_target_gcmon_cannot_read_is_unavailable(
        self, remote_reader: RemoteEventsReader, spy: SpyMonitor, error: BaseException
    ) -> None:
        spy.read_errors = [error]

        with pytest.raises(TargetUnavailable) as caught:
            remote_reader.read(7)

        assert caught.value.__cause__ is error

    def test_the_message_carries_the_pid_and_the_cause(
        self, remote_reader: RemoteEventsReader, spy: SpyMonitor
    ) -> None:
        """The monitor logs this string at debug level, so it has to say which
        pid and what went wrong."""
        spy.read_errors = [RuntimeError("Failed to read interpreter state address")]

        with pytest.raises(TargetUnavailable) as caught:
            remote_reader.read(7)

        assert "7" in str(caught.value)
        assert "Failed to read interpreter state address" in str(caught.value)

    def test_an_attach_failure_is_unavailable_too(self, remote_reader: RemoteEventsReader, spy: SpyMonitor) -> None:
        error = RuntimeError("Failed to initialize process handle")
        spy.attach_errors = [error]

        with pytest.raises(TargetUnavailable) as caught:
            remote_reader.read(7)

        assert caught.value.__cause__ is error

    @pytest.mark.parametrize(
        "error",
        [MemoryError("out of memory"), OSError(5, "Input/output error"), ValueError("bad address")],
        ids=["memory", "oserror", "value"],
    )
    def test_anything_else_propagates_untouched(
        self, remote_reader: RemoteEventsReader, spy: SpyMonitor, error: BaseException
    ) -> None:
        spy.read_errors = [error]

        with pytest.raises(type(error)):
            remote_reader.read(7)


class TestPruning:
    def test_forget_drops_the_attachment(self, remote_reader: RemoteEventsReader, spy: SpyMonitor) -> None:
        remote_reader.read(7)
        remote_reader.forget(7)
        remote_reader.read(7)

        assert attached_pids(spy) == [7, 7]

    def test_forgetting_a_pid_that_was_never_read_is_a_no_op(
        self, remote_reader: RemoteEventsReader, spy: SpyMonitor
    ) -> None:
        remote_reader.forget(7)

        assert spy.attaches == []

    def test_retain_drops_every_pid_outside_the_set(self, remote_reader: RemoteEventsReader, spy: SpyMonitor) -> None:
        remote_reader.read(7)
        remote_reader.read(8)
        remote_reader.read(9)

        remote_reader.retain({7, 9})

        remote_reader.read(7)
        remote_reader.read(8)
        remote_reader.read(9)

        assert attached_pids(spy) == [7, 8, 9, 8]

    def test_retain_keeps_a_pid_it_has_never_seen(self, remote_reader: RemoteEventsReader, spy: SpyMonitor) -> None:
        remote_reader.read(7)

        remote_reader.retain({7, 8})
        remote_reader.read(7)

        assert attached_pids(spy) == [7]

    def test_retaining_nothing_drops_everything(self, remote_reader: RemoteEventsReader, spy: SpyMonitor) -> None:
        remote_reader.read(7)
        remote_reader.read(8)

        remote_reader.retain(set())

        remote_reader.read(7)
        remote_reader.read(8)

        assert attached_pids(spy) == [7, 8, 7, 8]


# --------------------------------------------------------------------------
# Against the real _remote_debugging
# --------------------------------------------------------------------------

# A target that collects, so its rings are not empty.
_TARGET = "\n".join(
    [
        "import time",
        "keep = []",
        "while True:",
        "    keep.append([object() for _ in range(500)])",
        "    keep = keep[-20:]",
        "    time.sleep(0.001)",
    ]
)


def target_executable() -> str:
    """The interpreter a target is spawned with.

    On Windows a virtual environment's ``python.exe`` is a launcher that runs
    the real interpreter as a *child*, so the pid it hands back holds no Python
    runtime and cannot be attached to. ``sys._base_executable`` is the
    interpreter itself, on every platform.
    """
    base: Any = getattr(sys, "_base_executable", None)
    return str(base) if base else sys.executable


class Target:
    """A live Python process, attachable by the time ``pid`` is read."""

    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        self._proc = proc

    @property
    def pid(self) -> int:
        return self._proc.pid

    def kill(self) -> None:
        self._proc.kill()
        self._proc.wait()
        # The pid stays pinned while anything holds a handle to it, so a reader
        # under test still resolves it; what changes is that the reads fail.
        time.sleep(0.2)


@contextmanager
def running_target(timeout: float = 20.0) -> Generator[Target]:
    proc = subprocess.Popen(
        [target_executable(), "-c", _TARGET],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    target = Target(proc)
    try:
        probe = RemoteEventsReader()
        deadline = time.monotonic() + timeout
        while True:
            try:
                probe.read(target.pid)
                break
            except TargetUnavailable:
                if time.monotonic() >= deadline:
                    raise AssertionError(f"target {target.pid} never became readable") from None
                time.sleep(0.05)
        yield target
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


class TestAgainstARealProcess:
    def test_a_real_target_yields_records_for_every_generation(self, remote_reader: RemoteEventsReader) -> None:
        with running_target() as target:
            records = remote_reader.read(target.pid)

        assert records, "a collecting target has rings to read"
        assert {r.gen for r in records} == {0, 1, 2}
        assert all(r.iid >= 0 for r in records)

    def test_a_dead_target_raises_a_type_the_reader_translates(self) -> None:
        """Evidence for the narrowed catch, gathered on each platform CI runs.

        Windows reads through a process handle, macOS through a Mach task port
        and Linux through ``process_vm_readv`` on the raw pid, so a dead target
        reaches gcmon by three different routes. Anything outside this tuple
        would reach the monitor as ``PollStatus.FAIL`` with a traceback, and a
        target exiting is the ordinary end of a run.
        """
        with running_target() as target:
            monitor = GCMonitor(target.pid, debug=True)
            monitor.get_gc_stats(all_interpreters=True)
            target.kill()

            with pytest.raises(BaseException) as caught:
                monitor.get_gc_stats(all_interpreters=True)

        translated = (RuntimeError, ProcessLookupError, PermissionError)
        cause = caught.value.__cause__
        assert isinstance(caught.value, translated), (
            f"a dead target raised {type(caught.value).__name__} "
            f"(cause {type(cause).__name__ if cause else None}), which the reader does not translate"
        )

    def test_a_target_that_exits_becomes_unavailable(self, remote_reader: RemoteEventsReader) -> None:
        with running_target() as target:
            remote_reader.read(target.pid)
            target.kill()

            with pytest.raises(TargetUnavailable) as caught:
                remote_reader.read(target.pid)

        assert caught.value.__cause__ is not None

    def test_a_target_that_exits_stays_unavailable_on_every_later_read(self, remote_reader: RemoteEventsReader) -> None:
        """The attachment is rebuilt after the failure, and rebuilding it fails
        too. What must not happen is a read succeeding against a dead pid."""
        with running_target() as target:
            remote_reader.read(target.pid)
            target.kill()

            for _ in range(3):
                with pytest.raises(TargetUnavailable):
                    remote_reader.read(target.pid)

    def test_a_pid_no_process_holds_is_unavailable(self, remote_reader: RemoteEventsReader) -> None:
        with pytest.raises(TargetUnavailable):
            remote_reader.read(MISSING_PID)

    def test_the_reader_satisfies_its_own_protocol(self) -> None:
        from gcmon.monitoring.events_reader import EventsReader

        assert isinstance(RemoteEventsReader(), EventsReader)


class TestIndependence:
    def test_two_readers_hold_independent_attachments(self, spy: SpyMonitor) -> None:
        first = RemoteEventsReader()
        second = RemoteEventsReader()

        first.read(7)
        second.read(7)

        assert attached_pids(spy) == [7, 7]
