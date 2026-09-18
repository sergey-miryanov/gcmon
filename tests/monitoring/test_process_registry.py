"""Who holds a pid, who held it before, and who may say so."""

import os

import pytest

from gcmon.model.process import Process
from gcmon.monitoring.process_registry import CmdlineSink as Sink
from gcmon.monitoring.process_registry import ProcessRegistry, read_cmdline


class TestAPidHandedOn:
    def test_the_first_process_is_epoch_one(self) -> None:
        assert ProcessRegistry().create(100).pid_epoch == 1

    def test_a_successor_counts_from_its_predecessor(self) -> None:
        registry = ProcessRegistry()
        registry.create(100)
        registry.retire(100, 10)

        assert registry.create(100).pid_epoch == 2

    def test_the_epoch_advances_only_on_a_departure(self) -> None:
        """A tick that finds the same pid running finds the same process."""
        registry = ProcessRegistry()
        first = registry.create(100)

        assert registry.current(100) is first

    def test_one_pid_holds_one_process_at_a_time(self) -> None:
        registry = ProcessRegistry()
        registry.create(100)

        with pytest.raises(AssertionError):
            registry.create(100)

    def test_retiring_a_pid_that_holds_nothing_is_no_error(self) -> None:
        """The monitor prunes against a whole child listing, most of which it
        never created one for."""
        ProcessRegistry().retire(999, 10)


class TestRetain:
    def _tree(self) -> ProcessRegistry:
        registry = ProcessRegistry()
        for pid in (100, 200, 300):
            registry.create(pid)
        return registry

    def test_it_retires_everything_outside_the_listing(self) -> None:
        registry = self._tree()

        registry.retain({100}, 10)

        assert {process.pid for process in registry.live()} == {100}

    def test_the_survivors_keep_the_process_they_had(self) -> None:
        registry = self._tree()
        before = registry.current(100)

        registry.retain({100}, 10)

        assert registry.current(100) is before

    def test_a_listing_that_names_them_all_retires_nobody(self) -> None:
        registry = self._tree()

        registry.retain({100, 200, 300}, 10)

        assert len(registry.live()) == 3


class TestEvidenceThatOutlivesItsProcess:
    """A control-plane instant is stamped on arrival when its sender named no
    time, so it can reach gcmon after the pid was retired and still belong to
    the process that has gone."""

    def _handed_on(self) -> tuple[ProcessRegistry, Process, Process]:
        """Pid 100 runs from 0 to 50, then a successor from 60."""
        registry = ProcessRegistry()
        first = registry.create(100)
        registry.retire(100, 50)
        second = registry.create(100)
        return registry, first, second

    def test_evidence_inside_a_closed_life_belongs_to_it(self) -> None:
        registry, first, _ = self._handed_on()

        assert registry.at(100, 30) is first

    def test_evidence_after_the_last_departure_belongs_to_the_one_running(self) -> None:
        registry, _, second = self._handed_on()

        assert registry.at(100, 90) is second

    def test_evidence_racing_a_departure_belongs_to_the_process_that_left(self) -> None:
        """Nothing is running, and the instant is stamped later than the
        departure it raced."""
        registry = ProcessRegistry()
        first = registry.create(100)
        registry.retire(100, 50)

        assert registry.at(100, 70) is first

    def test_evidence_older_than_the_first_process_belongs_to_it(self) -> None:
        """A poll returns collections that already happened, so a record can
        predate gcmon discovering the process that produced it."""
        registry = ProcessRegistry()
        first = registry.create(100)

        assert registry.at(100, 100) is first

    def test_a_pid_nobody_created_belongs_to_nobody(self) -> None:
        """The one rule that keeps a stray event from opening a process that
        was never monitored: only the monitor creates."""
        assert ProcessRegistry().at(999, 10) is None


class TestTheCommandLine:
    """`create` reads it and hands it to the sink the caller passed. The
    registry keeps nothing: a `Process` holds its identity alone, and the
    exporter holds what was read (ADR-0025)."""

    def _sink(self) -> tuple[Sink, list[tuple[Process, tuple[str, ...] | None]]]:
        published: list[tuple[Process, tuple[str, ...] | None]] = []

        def _publish(process: Process, cmdline: tuple[str, ...] | None) -> None:
            published.append((process, cmdline))

        return _publish, published

    def test_it_is_read_when_the_process_is_created(self) -> None:
        publish, published = self._sink()
        registry = ProcessRegistry(cmdline_provider=lambda pid: (f"worker-{pid}",))

        registry.create(100, publish)

        assert published == [(Process(100, 1), ("worker-100",))]

    def test_each_process_on_a_reused_pid_reads_its_own(self) -> None:
        """The defect this fixes: one read per pid names the first process's
        program on every later one."""
        programs = iter([("first",), ("second",)])
        publish, published = self._sink()
        registry = ProcessRegistry(cmdline_provider=lambda pid: next(programs))
        registry.create(100, publish)
        registry.retire(100, 10)
        registry.create(100, publish)

        assert published == [(Process(100, 1), ("first",)), (Process(100, 2), ("second",))]

    def test_it_is_published_before_the_process_can_be_resolved(self) -> None:
        """The lock is still held. A control message naming the pid waits in
        `at` and reaches an exporter that already has the command line, where
        publishing after `create` returned leaves a window it can be described
        in without one."""
        registry = ProcessRegistry()
        locked: list[bool] = []

        registry.create(100, lambda process, cmdline: locked.append(registry._lock.locked()))

        assert locked == [True]

    def test_no_provider_publishes_no_command_line(self) -> None:
        publish, published = self._sink()

        ProcessRegistry().create(100, publish)

        assert published == [(Process(100, 1), None)]

    def test_a_provider_that_raises_publishes_no_command_line(self) -> None:
        """The pid has gone between the poll and the read, or psutil is
        missing. It costs that process a command line and nothing else."""

        def _raises(pid: int) -> tuple[str, ...] | None:
            raise RuntimeError(pid)

        publish, published = self._sink()

        ProcessRegistry(cmdline_provider=_raises).create(100, publish)

        assert published == [(Process(100, 1), None)]

    def test_psutil_reads_the_running_interpreter(self) -> None:
        """The provider the CLI wires in, against the one process this
        test knows is alive."""
        assert read_cmdline(os.getpid())
