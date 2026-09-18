import logging
from collections.abc import Callable, Generator, Mapping, Sequence, Set
from typing import override
from unittest.mock import MagicMock, Mock, patch

import pytest

from gcmon.model.data import GCStatsInfo
from gcmon.model.poll_status import PollStatus
from gcmon.model.process import Process
from gcmon.model.protocol import TGCStatsInfo
from gcmon.monitoring.events_reader import TargetUnavailable
from gcmon.monitoring.monitor import EventsMonitor, PollReport
from gcmon.monitoring.process_registry import ProcessRegistry
from gcmon.monitoring.target_process import ExternalProcess
from gcmon.monitoring.wait_policy import WaitPolicy, WaitPolicyFactory, no_wait_policy
from gcmon.stats.streaming_stats import StreamingStats
from gcmon.support.vocabulary import PROGRAM_NAME
from tests.conftest import DEFAULT_PID
from tests.helpers import FakeEventsReader, MockExporter, create_mock_stats_item, proc

NO_RECORDS: list[TGCStatsInfo] = []


def _reads(records: Sequence[TGCStatsInfo]) -> Callable[[int], Sequence[TGCStatsInfo]]:
    """A reader that answers the same ring for every pid."""
    return lambda pid: records


def _polling_errors(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Every line `_poll` wrote about a read it could not make."""
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("Error while polling")]


@pytest.fixture
def one_record(reader: FakeEventsReader) -> GCStatsInfo:
    """One finished record, answered to every poll of every pid."""
    item = create_mock_stats_item(ts_start=1_000_000_000, ts_stop=1_005_000_000)
    reader.reads = _reads([item])
    return item


@pytest.fixture
def mock_stats_update(monitor: EventsMonitor) -> Generator[MagicMock]:
    with patch.object(monitor._stats, "update") as mock:
        yield mock


@pytest.fixture
def mock_read(reader: FakeEventsReader) -> MagicMock:
    """What the injected reader answers, with ``Mock``'s semantics.

    Set ``return_value`` for a fixed answer, or ``side_effect`` for a scripted
    sequence or a failure. Raise :class:`TargetUnavailable` to play a target
    gcmon cannot read: the platform's own exception types stop at the reader, so
    a double impersonating them would be testing the wrong seam.
    """
    mock = MagicMock(name="read")
    reader.reads = mock
    return mock


@pytest.fixture
def mock_monotonic() -> Generator[MagicMock]:
    """Patch the clock used to measure read time, in nanoseconds."""
    with patch("gcmon.monitoring.monitor.time.monotonic_ns") as mock:
        yield mock


class TestEventsMonitorExtra:
    def test_get_child_pids(self, monitor: EventsMonitor) -> None:
        with patch("gcmon.monitoring.monitor.get_child_pids", return_value=[999, 888]) as mock_get:
            children = monitor._get_child_pids()

        mock_get.assert_called_once_with(12345, recursive=True)
        assert children == [999, 888]

    def test_get_child_pids_exception_returns_none(self, monitor: EventsMonitor) -> None:
        """None rather than [], so the caller can tell a failed listing from
        a target with no children and skip pruning that tick."""
        with patch("gcmon.monitoring.monitor.get_child_pids", side_effect=Exception("boom")) as mock_get:
            children = monitor._get_child_pids()

        mock_get.assert_called_once_with(12345, recursive=True)
        assert children is None

    def test_context_manager_enter_exit(self, monitor: EventsMonitor, exporter: MockExporter) -> None:
        assert monitor.is_enabled

        with monitor as m:
            assert m is monitor
            assert monitor.is_enabled

        assert not monitor.is_enabled

    def test_poll_updates_stats(
        self, monitor: EventsMonitor, one_record: GCStatsInfo, mock_stats_update: MagicMock
    ) -> None:
        monitor._poll(12345)

        mock_stats_update.assert_called_once_with(proc(DEFAULT_PID), one_record)

    def test_poll_skips_invalid_timestamp_event(
        self, monitor: EventsMonitor, exporter: MockExporter, reader: FakeEventsReader
    ) -> None:
        reader.reads = _reads([create_mock_stats_item(ts_start=2_000, ts_stop=1_000)])

        monitor._poll(12345)

        assert exporter.events == []

    def test_poll_skips_equal_timestamp_event(
        self, monitor: EventsMonitor, exporter: MockExporter, reader: FakeEventsReader
    ) -> None:
        reader.reads = _reads([create_mock_stats_item(ts_start=1_000, ts_stop=1_000)])

        monitor._poll(12345)

        assert exporter.events == []

    def test_each_pid_keeps_its_own_cursor(
        self, monitor: EventsMonitor, exporter: MockExporter, reader: FakeEventsReader
    ) -> None:
        """A child PID's events are not suppressed by a later timestamp seen on
        another PID. One monitor polls the target and every child, and their
        event streams interleave in time."""
        per_pid = {
            12345: [create_mock_stats_item(collections=50, ts_start=5_000, ts_stop=5_100)],
            999: [
                create_mock_stats_item(collections=7, ts_start=4_000, ts_stop=4_100),
                create_mock_stats_item(collections=8, ts_start=6_000, ts_stop=6_100),
            ],
        }
        reader.reads = lambda pid: per_pid[pid]

        monitor._poll(12345)
        monitor._poll(999)

        assert [e.ts_start for e in exporter.events] == [5_000, 4_000, 6_000]

    def test_a_record_already_read_is_not_written_twice(
        self, monitor: EventsMonitor, exporter: MockExporter, reader: FakeEventsReader
    ) -> None:
        reader.reads = _reads([create_mock_stats_item(ts_start=5_000, ts_stop=5_100)])

        monitor._poll(12345)
        monitor._poll(12345)

        assert [e.ts_start for e in exporter.events] == [5_000]


class TestEventsMonitor:
    def test_init(self, monitor: EventsMonitor) -> None:
        assert monitor.is_enabled
        assert monitor.pid == 12345

    def test_poll(self, exporter: MockExporter, monitor: EventsMonitor, mock_read: MagicMock) -> None:
        item = create_mock_stats_item(ts_start=1_000_000_000, ts_stop=1_005_000_000)

        mock_read.return_value = [item]

        result = monitor._poll(12345)

        assert result.status == PollStatus.OK
        assert len(exporter.events) == 1

    def test_poll_duplicate_records(self, exporter: MockExporter, monitor: EventsMonitor, mock_read: MagicMock) -> None:
        """The monitor identifies a record by `collections`, so two slots
        reporting the same value are one collection seen twice."""
        item1 = create_mock_stats_item(collections=50, ts_start=1_000_000_000, ts_stop=1_005_000_000)
        item2 = create_mock_stats_item(collections=50, ts_start=1_000_000_000, ts_stop=1_006_000_000)
        item3 = create_mock_stats_item(collections=51, ts_start=2_000_000_000, ts_stop=2_005_000_000)

        mock_read.return_value = [item1, item2, item3]

        monitor._poll(12345)

        assert len(exporter.events) == 2
        assert exporter.events[0].ts_start == 1_000_000_000
        assert exporter.events[1].ts_start == 2_000_000_000

    def test_a_poll_answers_with_the_process_it_filed_under(self, monitor: EventsMonitor, mock_read: MagicMock) -> None:
        """`tick` reports it as live, and asking the registry for it a second
        time takes the registry's lock again."""
        mock_read.return_value = [create_mock_stats_item()]

        assert monitor._poll(12345) == (PollStatus.OK, proc(DEFAULT_PID))

    def test_a_failed_poll_answers_with_no_process(self, monitor: EventsMonitor, mock_read: MagicMock) -> None:
        """The other half: nothing was filed, so there is nothing to name."""
        mock_read.side_effect = TargetUnavailable("PID 12345 is not readable: gone")

        assert monitor._poll(12345) == (PollStatus.INVALID_PROCESS, None)

    def test_poll_unreadable_target(self, monitor: EventsMonitor, mock_read: MagicMock) -> None:
        """One case, not five. Which CPython failures mean "unreadable" is the
        reader's question and `tests/test_events_reader.py` asks it; all the
        monitor decides is what an unreadable target does to a poll."""
        mock_read.side_effect = TargetUnavailable("PID 12345 is not readable: gone")

        assert monitor._poll(12345).status == PollStatus.INVALID_PROCESS

    def test_poll_of_a_departed_target_writes_no_warning(
        self, monitor: EventsMonitor, mock_read: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A target exiting is the ordinary end of a run, so it stays at debug
        level. This is the regression the swap to ``GCMonitor`` most invites: a
        traceback on stderr every time a monitored process finishes."""
        mock_read.side_effect = TargetUnavailable("PID 12345 is not readable: [Errno 3] No such process")

        with caplog.at_level(logging.DEBUG, logger=PROGRAM_NAME):
            assert monitor._poll(12345).status == PollStatus.INVALID_PROCESS

        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
        assert any("12345" in r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG)

    def test_an_unreadable_pid_is_reported_once(
        self, monitor: EventsMonitor, mock_read: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A target that never becomes readable is polled again every tick, and
        the repeat carries nothing the first line did not. Left in, it is a wall
        of one line per pid per tick for as long as the run lasts."""
        mock_read.side_effect = TargetUnavailable("PID 12345 is not readable: Failed to get Python runtime address")

        with caplog.at_level(logging.DEBUG, logger=PROGRAM_NAME):
            monitor._poll(12345)
            monitor._poll(12345)

        assert len(_polling_errors(caplog)) == 1

    def test_a_new_reason_is_reported(
        self, monitor: EventsMonitor, mock_read: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A target walks through several of these as it starts: it has no
        runtime address, then one whose interpreter state cannot be read. The
        second line says it got further, so it is not the repeat above."""
        mock_read.side_effect = [
            TargetUnavailable("PID 12345 is not readable: Failed to get Python runtime address"),
            TargetUnavailable("PID 12345 is not readable: Failed to read interpreter state address"),
        ]

        with caplog.at_level(logging.DEBUG, logger=PROGRAM_NAME):
            for _ in range(2):
                monitor._poll(12345)

        assert len(_polling_errors(caplog)) == 2

    def test_a_pid_that_answers_between_failures_is_reported_again(
        self, monitor: EventsMonitor, mock_read: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """What is suppressed is the repeat, not the second failure: a process
        that answered and then went quiet is a different event from the one that
        was never readable, and says so even where the reason reads the same."""
        mock_read.side_effect = [
            TargetUnavailable("PID 12345 is not readable: No interpreter state found"),
            NO_RECORDS,
            TargetUnavailable("PID 12345 is not readable: No interpreter state found"),
        ]

        with caplog.at_level(logging.DEBUG, logger=PROGRAM_NAME):
            for _ in range(3):
                monitor._poll(12345)

        assert len(_polling_errors(caplog)) == 2

    def test_poll_general_exception(self, monitor: EventsMonitor, mock_read: MagicMock) -> None:
        mock_read.side_effect = ValueError("Unexpected error")

        result = monitor._poll(12345)

        assert result.status == PollStatus.FAIL

    def test_poll_general_exception_is_warned_with_a_traceback(
        self, monitor: EventsMonitor, mock_read: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The other half of the case above: a failure gcmon does not recognise
        must be loud, or the two arms could be swapped without a test noticing.
        """
        mock_read.side_effect = ValueError("Unexpected error")

        assert monitor._poll(12345).status == PollStatus.FAIL

        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) == 1
        assert warnings[0].exc_info is not None

    def test_poll_after_stop(self, monitor: EventsMonitor) -> None:
        monitor.stop()

        assert monitor._poll(12345).status == PollStatus.FAIL

    def test_stop(self, exporter: MockExporter, monitor: EventsMonitor) -> None:
        monitor.stop()

        assert not monitor.is_enabled
        assert exporter._close_called

    def test_stop_idempotent(self, monitor: EventsMonitor) -> None:
        monitor.stop()

        monitor.stop()

        assert not monitor.is_enabled

    def test_stop_lets_go_of_every_attachment(
        self, monitor: EventsMonitor, mock_read: MagicMock, reader: FakeEventsReader
    ) -> None:
        """An attachment is a handle on somebody else's process -- on Windows it
        holds the pid reserved -- so a monitor that has stopped must not still be
        holding one. Leaving it to garbage collection is not the same thing."""
        mock_read.return_value = NO_RECORDS
        monitor._poll(12345)
        monitor._poll(999)
        assert reader.attached == {12345, 999}

        monitor.stop()

        assert reader.attached == set()

    def test_stopping_twice_still_holds_nothing(
        self, monitor: EventsMonitor, mock_read: MagicMock, reader: FakeEventsReader
    ) -> None:
        mock_read.return_value = NO_RECORDS
        monitor._poll(12345)

        monitor.stop()
        monitor.stop()

        assert reader.attached == set()


class TestEventsMonitorReadTime:
    """Tests for read time tracking around the reader."""

    def test_poll_records_read_time(
        self, monitor: EventsMonitor, stats: StreamingStats, mock_read: MagicMock, mock_monotonic: MagicMock
    ) -> None:
        mock_monotonic.side_effect = [1_000_000_000, 1_002_500_000]
        mock_read.return_value = [create_mock_stats_item()]

        assert monitor._poll(12345).status == PollStatus.OK

        # 2.5 ms between the two monotonic_ns readings, stored as nanoseconds
        assert stats.read_time.count() == 1
        assert stats.read_time.sum() == 2_500_000

    def test_poll_records_read_time_without_events(
        self, monitor: EventsMonitor, stats: StreamingStats, mock_read: MagicMock
    ) -> None:
        mock_read.return_value = NO_RECORDS

        assert monitor._poll(12345).status == PollStatus.OK

        assert stats.count() == 0
        assert stats.read_time.count() == 1

    def test_poll_keeps_sub_microsecond_read_time(
        self, monitor: EventsMonitor, stats: StreamingStats, mock_read: MagicMock, mock_monotonic: MagicMock
    ) -> None:
        mock_monotonic.side_effect = [1_000_000_000, 1_000_000_750]
        mock_read.return_value = NO_RECORDS

        assert monitor._poll(12345).status == PollStatus.OK

        assert stats.read_time.sum() == 750

    def test_read_time_accumulates_over_polls(
        self, monitor: EventsMonitor, stats: StreamingStats, mock_read: MagicMock, mock_monotonic: MagicMock
    ) -> None:
        mock_monotonic.side_effect = [0, 1_000_000, 5_000_000, 8_000_000]
        mock_read.return_value = NO_RECORDS

        monitor._poll(12345)
        monitor._poll(12345)

        assert stats.read_time.count() == 2
        assert stats.read_time.sum() == 4_000_000
        assert stats.read_time.average() == 2_000_000

    def test_read_time_shared_across_pids(
        self,
        make_monitor: Callable[..., EventsMonitor],
        stats: StreamingStats,
        mock_read: MagicMock,
    ) -> None:
        mock_read.return_value = NO_RECORDS

        first, second = make_monitor(pid=111), make_monitor(pid=222)

        first._poll(111)
        second._poll(222)

        assert stats.read_time.count() == 2

    def test_read_time_not_recorded_on_failed_read(
        self, monitor: EventsMonitor, stats: StreamingStats, mock_read: MagicMock
    ) -> None:
        mock_read.side_effect = TargetUnavailable("PID 12345 is not readable: not started yet")

        assert monitor._poll(12345).status == PollStatus.INVALID_PROCESS

        assert stats.read_time.count() == 0

    def test_read_time_not_recorded_after_stop(
        self, monitor: EventsMonitor, stats: StreamingStats, mock_read: MagicMock
    ) -> None:
        mock_read.return_value = NO_RECORDS
        monitor.stop()

        assert monitor._poll(12345).status == PollStatus.FAIL

        assert stats.read_time.count() == 0
        mock_read.assert_not_called()


# --------------------------------------------------------------------------
# One tick, one call
# --------------------------------------------------------------------------
#
# ADR-0017. The ring cursors, the poll instant ADR-0015's arithmetic runs on,
# and the wait policy deciding when a pid is finished have one owner between
# them, and one prune against one set.
#
# These drive `tick` and assert on its report and on what reached the exporter.
# Empty state dicts after a prune prove the prune ran, not that it was right.
# Getting it wrong shows up as a `GC Loss` window for collections that never
# happened.


def _ring(*collections: int, gen: int = 0, iid: int = 0, ts_base: int = 0) -> list[GCStatsInfo]:
    """A whole ring buffer holding one finished record per counter given.

    A poll returns the ring, not the new records in it, so this is what
    a read answers -- deciding which of them is unseen is the
    monitor's job and the thing under test.

    *ts_base* dates the records from an instant other than zero. A successor
    on a recycled pid needs it: its collections happen after the departure
    gcmon noticed, and a record dated inside its predecessor's life is one
    the predecessor already exported, which the monitor drops (ADR-0025).
    """
    return [
        create_mock_stats_item(
            gen=gen,
            iid=iid,
            collections=c,
            ts_start=ts_base + c * 1_000,
            ts_stop=ts_base + c * 1_000 + 100,
            duration=c * 0.001,
        )
        for c in collections
    ]


def _policy(*verdicts: bool) -> Mock:
    """A wait policy answering *verdicts* in order, one per poll it judges."""
    return Mock(spec=WaitPolicy, **{"wait.side_effect": list(verdicts)})


def _monitor(
    exporter: MockExporter,
    *,
    pid: int = 12345,
    wait_policy_factory: WaitPolicyFactory = no_wait_policy,
    is_pid_enabled: Callable[[int], bool] | None = None,
    registry: ProcessRegistry | None = None,
) -> EventsMonitor:
    """A monitor wired the way the monitoring command wires one.

    The reader is a fake, always. A real one would attach to whatever process
    happens to hold 12345 or 999 on the machine running the suite. `_drive`
    scripts what it answers.
    """
    return EventsMonitor(
        ExternalProcess(pid=pid),
        exporter,
        StreamingStats(),
        reader=FakeEventsReader(),
        wait_policy_factory=wait_policy_factory,
        is_pid_enabled=is_pid_enabled,
        registry=registry,
    )


Batch = Sequence[GCStatsInfo] | Exception


# What the caller's clock reads on each tick. Spelled out so a test can name
# the instant a liveness observation was stamped with.
TICK_NS = 1_000_000_000


def _never_stops() -> bool:
    """A tick nothing interrupts, which is what most of these tests want.

    `tick` requires a cancel check rather than defaulting to this, since its
    one production caller always has a stop event to hand. It lives here
    because the tests are the only thing that ever wanted the default.
    """
    return False


def _reader_of(monitor: EventsMonitor) -> FakeEventsReader:
    """The fake `_monitor` injected, so `_drive` can script what it answers.

    Reaching for the private is deliberate and confined to this helper: the
    monitor exposes no reader, because nothing in production asks it for one.
    """
    reader = monitor._reader
    assert isinstance(reader, FakeEventsReader), "these tests never build a real reader"
    return reader


def _drive(
    monitor: EventsMonitor,
    listings: Sequence[list[int] | Exception],
    rings: Mapping[int, Sequence[Batch]],
    stop: Callable[[], bool] = _never_stops,
) -> list[PollReport]:
    """One tick per entry in *listings*; one report back per tick.

    *listings* is what the child listing answers each tick. An exception entry
    stands for a listing that failed, which the monitor turns into the ``None``
    meaning "no answer" rather than an empty tree.

    *rings* gives the whole ring buffer each successive poll of a pid returns,
    so a pid polled three times needs three entries. A pid that should never be
    polled needs none: an unexpected poll raises `KeyError` here. An exception
    entry stands for a poll that failed -- `TargetUnavailable` for a target
    gcmon cannot read, anything else for a failure it does not recognise.
    """
    pending = {pid: iter(batches) for pid, batches in rings.items()}

    def read(pid: int) -> list[GCStatsInfo]:
        batch = next(pending[pid])
        if isinstance(batch, Exception):
            raise batch
        return list(batch)

    _reader_of(monitor).reads = read

    reports: list[PollReport] = []
    now_ns = 0
    # A tick's reads land on the instant it was given, so a test that says
    # nothing about the clock gets one stamp per tick. `TestProcessLiveness`
    # drives the reads apart where the spread is the subject.
    with (
        patch("gcmon.monitoring.monitor.get_child_pids", side_effect=list(listings)),
        patch("gcmon.monitoring.monitor.time.monotonic_ns", lambda: now_ns),
    ):
        for tick, _ in enumerate(listings, start=1):
            now_ns = tick * TICK_NS
            reports.append(monitor.tick(now_ns, stop))

    return reports


class TestTheReport:
    """What a tick answers: who was alive, and whether to keep going."""

    def test_names_the_pids_that_answered(self, exporter: MockExporter) -> None:
        reports = _drive(
            _monitor(exporter),
            listings=[[999]],
            rings={12345: [_ring(1)], 999: [_ring(1)]},
        )

        assert reports[0].live == frozenset({proc(DEFAULT_PID), proc(999)})

    def test_a_pid_that_could_not_be_read_is_not_live(self, exporter: MockExporter) -> None:
        """Only ``PollStatus.OK`` is evidence a process was there. A failed
        read is evidence of nothing."""
        reports = _drive(
            _monitor(exporter),
            listings=[[999]],
            rings={12345: [_ring(1)], 999: [TargetUnavailable("no such process")]},
        )

        assert reports[0].live == frozenset({proc(DEFAULT_PID)})

    def test_the_live_set_is_frozen(self, exporter: MockExporter) -> None:
        """Nothing downstream mutates it -- the sampler iterates it and the
        exporter folds it into a min/max -- so it is handed over frozen."""
        reports = _drive(_monitor(exporter), listings=[[]], rings={12345: [_ring(1)]})

        assert isinstance(reports[0].live, frozenset)

    def test_keep_running_while_a_policy_still_waits(self, exporter: MockExporter) -> None:
        reports = _drive(
            _monitor(exporter, wait_policy_factory=Mock(side_effect=[_policy(True)])),
            listings=[[]],
            rings={12345: [_ring(1)]},
        )

        assert reports[0].keep_running

    def test_keep_running_is_false_once_every_policy_has_given_up(self, exporter: MockExporter) -> None:
        reports = _drive(
            _monitor(exporter, wait_policy_factory=Mock(side_effect=[_policy(False), _policy(False)])),
            listings=[[999]],
            rings={12345: [_ring(1)], 999: [_ring(1)]},
        )

        assert not reports[0].keep_running

    def test_one_policy_still_waiting_is_enough(self, exporter: MockExporter) -> None:
        reports = _drive(
            _monitor(exporter, wait_policy_factory=Mock(side_effect=[_policy(False), _policy(True)])),
            listings=[[999]],
            rings={12345: [_ring(1)], 999: [_ring(1)]},
        )

        assert reports[0].keep_running

    def test_keep_running_is_false_when_nothing_was_polled_at_all(self, exporter: MockExporter) -> None:
        """No policy answered, so none is holding the loop open. The tick
        before this one is what kept the run alive; this one ends it."""
        reports = _drive(
            _monitor(exporter, is_pid_enabled=lambda pid: False),
            listings=[[999]],
            rings={},
        )

        assert not reports[0].keep_running
        assert reports[0].live == frozenset()


class TestTheControlPlaneVerdict:
    def test_a_suppressed_pid_is_not_polled_and_is_not_live(self, exporter: MockExporter) -> None:
        """A pid the control server disabled is never read, so it is never
        observed. `rings` has no entry for it, so a poll would raise."""
        reports = _drive(
            _monitor(exporter, is_pid_enabled=lambda pid: pid != 999),
            listings=[[999]],
            rings={12345: [_ring(1)]},
        )

        assert reports[0].live == frozenset({proc(DEFAULT_PID)})
        assert 999 not in exporter.events_by_pid


class TestTheStopCheck:
    def test_a_tick_gives_up_between_pids(self, exporter: MockExporter) -> None:
        """Shutdown need not wait out a whole process tree. The event behind
        the check belongs to the caller; the monitor only reads it."""
        answers = iter([False, True])

        reports = _drive(
            _monitor(exporter),
            listings=[[999]],
            rings={12345: [_ring(1)]},
            stop=lambda: next(answers),
        )

        assert reports[0].live == frozenset({proc(DEFAULT_PID)})
        assert 999 not in exporter.events_by_pid


class TestARecycledPidStartsFromNothing:
    """ADR-0017's hazard, where it would surface.

    A pid that leaves the process tree loses its cursor and its poll instant.
    Whatever the OS gives that number to next is a different process with its
    own `collections` counter, and comparing the two invents a loss window for
    collections that never happened. An operator reads that span as a gcmon
    measurement.
    """

    def test_the_first_poll_back_emits_records_and_no_loss_window(self, exporter: MockExporter) -> None:
        _drive(
            _monitor(exporter),
            listings=[[999], [], [999]],
            rings={
                12345: [_ring(1), _ring(1), _ring(1)],
                # The pid comes back holding a counter from a process that has
                # nothing to do with the one that left, collecting after it
                # arrived.
                999: [_ring(1, 2), _ring(300, 301, ts_base=3 * TICK_NS)],
            },
        )

        assert exporter.loss_events == []
        assert [event.collections for event in exporter.events_by_pid[999]] == [1, 2, 300, 301]

    def test_the_same_ring_without_a_departure_does_open_one(self, exporter: MockExporter) -> None:
        """The control, and the reason the assertion above has teeth. Held in
        the tree the whole time, the same counters are one process's, and the
        jump between them is a real gap that ADR-0015 is right to report."""
        _drive(
            _monitor(exporter),
            listings=[[999], [999], [999]],
            rings={
                12345: [_ring(1), _ring(1), _ring(1)],
                999: [_ring(1, 2), _ring(1, 2), _ring(300, 301)],
            },
        )

        lost = [gen.lost_count for _pid, msg in exporter.loss_events for gen in msg.gens]
        assert lost == [297]


class TestOnePruneOverOneSet:
    """The ring state and the wait policy share one lifetime.

    Two modules used to prune them against two expressions of the same child
    set. These say both go, and go together.
    """

    def test_a_pid_that_leaves_the_tree_loses_its_ring_state(
        self, exporter: MockExporter, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Visible as a re-read: the returning pid hands back slots the
        monitor had already read, which a kept cursor would have swallowed
        silently. They reach no exporter, because the process that produced
        them has gone and the one that read them did not (ADR-0025), so the
        log is where the prune shows.
        """
        with caplog.at_level(logging.DEBUG, logger=PROGRAM_NAME):
            _drive(
                _monitor(exporter),
                listings=[[999], [], [999]],
                rings={
                    12345: [_ring(1), _ring(1), _ring(1)],
                    999: [_ring(1, 2), _ring(1, 2)],
                },
            )

        assert [event.collections for event in exporter.events_by_pid[999]] == [1, 2]
        assert [record.message for record in caplog.records if "dropped" in record.message] == [
            "PID 999: dropped 2 record(s) re-read for 999#2, already exported under 999"
        ]

    def test_a_pid_that_leaves_the_tree_loses_its_policy_too(self, exporter: MockExporter) -> None:
        """Same tick, same set. A policy outliving the cursor would judge a
        new process on what the old one did."""
        factory = Mock(side_effect=[_policy(True, True, True), _policy(True), _policy(True)])

        _drive(
            _monitor(exporter, wait_policy_factory=factory),
            listings=[[999], [], [999]],
            rings={
                12345: [_ring(1), _ring(1), _ring(1)],
                999: [_ring(1, 2), _ring(1, 2)],
            },
        )

        # 12345 once, then 999 twice: once on first sight, once on return.
        assert factory.call_count == 3

    def test_a_pid_that_leaves_the_tree_can_be_reported_unreadable_again(
        self, exporter: MockExporter, caplog: pytest.LogCaptureFixture
    ) -> None:
        """`_poll` says once per pid and reason that it cannot read it, and
        that memory is state of the same lifetime: what comes back under the
        number is a new process, and its first failed poll is news even where
        it failed the way its predecessor did.
        """
        with caplog.at_level(logging.DEBUG, logger=PROGRAM_NAME):
            _drive(
                _monitor(exporter),
                listings=[[999], [999], [], [999]],
                rings={
                    12345: [_ring(1), _ring(1), _ring(1), _ring(1)],
                    999: [TargetUnavailable("not started")] * 3,
                },
            )

        reported = [r for r in caplog.records if "child PID=999" in r.getMessage()]
        assert len(reported) == 2

    def test_a_pid_that_leaves_the_tree_loses_its_attachment(self, exporter: MockExporter) -> None:
        """ADR-0020 puts the reader's attachment under ADR-0017's rule, so it
        goes in the same pass as the cursors.

        This is the half no other assertion can reach. A kept attachment holds
        the runtime address and debug offsets of the process that left, and
        applied to whatever takes that pid next it reads a stranger's memory --
        producing records that are structurally valid, pass every filter, and
        reach the trace.
        """
        monitor = _monitor(exporter)

        _drive(
            monitor,
            listings=[[999], [], []],
            rings={
                12345: [_ring(1), _ring(1), _ring(1)],
                999: [_ring(1, 2)],
            },
        )

        reader = _reader_of(monitor)
        assert reader.retained == [
            frozenset({12345, 999}),
            frozenset({12345}),
            frozenset({12345}),
        ]
        assert reader.attached == {12345}

    def test_a_pid_the_policy_gave_up_on_loses_its_attachment(self, exporter: MockExporter) -> None:
        """The other route out. Here the pid is still listed as a child, so the
        retain pass keeps it and the per-pid forget is what has to drop it."""
        factory = Mock(side_effect=[_policy(True), _policy(False)])
        monitor = _monitor(exporter, wait_policy_factory=factory)

        _drive(
            monitor,
            listings=[[999]],
            rings={
                12345: [_ring(1)],
                999: [TargetUnavailable("gone")],
            },
        )

        reader = _reader_of(monitor)
        assert reader.forgotten == [999]
        assert reader.attached == {12345}

    def test_the_attachment_and_the_cursors_go_in_the_same_pass(self, exporter: MockExporter) -> None:
        """One set, one pass. The retain call the reader saw is the same set the
        cursors were pruned against, which is what "one prune" means."""
        monitor = _monitor(exporter)

        _drive(
            monitor,
            listings=[[999, 888], [999]],
            rings={
                12345: [_ring(1), _ring(1)],
                999: [_ring(1), _ring(1)],
                888: [_ring(1)],
            },
        )

        reader = _reader_of(monitor)
        assert reader.retained == [frozenset({12345, 999, 888}), frozenset({12345, 999})]
        assert reader.attached == {12345, 999}
        assert monitor._pids.keys() == {12345, 999}

    def test_a_failed_listing_prunes_no_attachment_either(self, exporter: MockExporter) -> None:
        """``None`` from the listing means "no answer". Dropping attachments on
        it would make every live child pay to attach again on the next tick,
        which is the cost this seam exists to remove."""
        monitor = _monitor(exporter)

        _drive(
            monitor,
            listings=[[999], Exception("cannot enumerate"), [999]],
            rings={
                12345: [_ring(1), _ring(1), _ring(1)],
                999: [_ring(1, 2), _ring(1, 2)],
            },
        )

        reader = _reader_of(monitor)
        assert reader.retained == [frozenset({12345, 999}), frozenset({12345, 999})]
        assert reader.forgotten == []

    def test_a_failed_listing_prunes_nothing(self, exporter: MockExporter) -> None:
        """``None`` from the listing means "no answer", not "no children".
        Reading it as an empty tree would drop the cursors of every live child
        and re-export their whole rings on the next poll."""
        _drive(
            _monitor(exporter),
            listings=[[999], Exception("cannot enumerate"), [999]],
            rings={
                12345: [_ring(1), _ring(1), _ring(1)],
                # Polled on the first and third ticks; the second cannot see it.
                999: [_ring(1, 2), _ring(1, 2)],
            },
        )

        assert [event.collections for event in exporter.events_by_pid[999]] == [1, 2]


class TestProcessLiveness:
    """A successful read is the only evidence gcmon has that a process was
    still there, and for a process that never collects it is the only evidence
    of any kind. The tick that produced it is what reports it. See ADR-0011.
    """

    def test_reported_once_per_tick_with_the_whole_live_set(self, exporter: MockExporter) -> None:
        """One call and one lock acquisition per tick, not per pid."""
        _drive(
            _monitor(exporter),
            listings=[[999, 888]],
            rings={12345: [_ring(1)], 999: [_ring(1)], 888: [_ring(1)]},
        )

        assert exporter.liveness == [(frozenset({12345, 999, 888}), TICK_NS)]

    def test_reported_every_tick(self, exporter: MockExporter) -> None:
        _drive(
            _monitor(exporter),
            listings=[[999], [888]],
            rings={12345: [_ring(1), _ring(1)], 999: [_ring(1)], 888: [_ring(1)]},
        )

        assert [pids for pids, _ts in exporter.liveness] == [
            frozenset({12345, 999}),
            frozenset({12345, 888}),
        ]

    def test_stamped_with_the_instant_the_tick_reached(self, exporter: MockExporter) -> None:
        _drive(
            _monitor(exporter),
            listings=[[], []],
            rings={12345: [_ring(1), _ring(1)]},
        )

        assert [ts for _pids, ts in exporter.liveness] == [TICK_NS, 2 * TICK_NS]

    def test_stamped_no_earlier_than_the_reads_that_proved_them_alive(self, exporter: MockExporter) -> None:
        """Two processes alive in one tick have to share an end, so the sweep
        that draws the `Processes` track sees them nest rather than cross. The
        opening instant does not do it: reads are sequential, so the pid polled
        second is observed later, and a long-lived parent would be clipped back
        to the start of a short child that recycled a pid (ADR-0011)."""
        monitor = _monitor(exporter)
        reads = iter([TICK_NS + 10, TICK_NS + 20, TICK_NS + 30, TICK_NS + 40])
        with (
            patch("gcmon.monitoring.monitor.get_child_pids", return_value=[999]),
            patch("gcmon.monitoring.monitor.time.monotonic_ns", side_effect=lambda: next(reads)),
        ):
            _reader_of(monitor).reads = lambda pid: [_ring(1)[0]]

            monitor.tick(TICK_NS, _never_stops)

        assert [ts for _pids, ts in exporter.liveness] == [TICK_NS + 40]

    def test_a_pid_that_could_not_be_read_is_not_reported(self, exporter: MockExporter) -> None:
        _drive(
            _monitor(exporter),
            listings=[[999]],
            rings={12345: [_ring(1)], 999: [TargetUnavailable("gone")]},
        )

        assert exporter.liveness == [(frozenset({12345}), TICK_NS)]

    def test_a_suppressed_pid_is_not_reported(self, exporter: MockExporter) -> None:
        """Never polled, so never observed. Its span keeps whatever it had."""
        _drive(
            _monitor(exporter, is_pid_enabled=lambda pid: pid != 999),
            listings=[[999]],
            rings={12345: [_ring(1)]},
        )

        assert exporter.liveness == [(frozenset({12345}), TICK_NS)]

    def test_not_reported_when_nothing_answered(self, exporter: MockExporter) -> None:
        """An empty set would widen no span, so the call is skipped rather
        than made with nothing in it."""
        _drive(
            _monitor(exporter),
            listings=[[]],
            rings={12345: [TargetUnavailable("gone")]},
        )

        assert exporter.liveness == []

    def test_reported_after_the_records_the_same_tick_produced(self) -> None:
        """ADR-0011: after the poll phase, never during it. That ordering is
        what leaves a batch crossing `flush_threshold` mid-poll still able to
        emit a rank-less process descriptor -- which the ADR records, and which
        stays true for the same reason now that both happen in one call."""
        exporter = _OrderedExporter()

        _drive(
            _monitor(exporter),
            listings=[[999]],
            rings={12345: [_ring(1)], 999: [_ring(1)]},
        )

        assert exporter.order == ["record", "record", "liveness"]


class _OrderedExporter(MockExporter):
    """Records what reached the exporter, in the order it arrived."""

    def __init__(self) -> None:
        super().__init__()
        self.order: list[str] = []

    @override
    def add_event(self, process: Process, item: TGCStatsInfo) -> None:
        self.order.append("record")
        super().add_event(process, item)

    @override
    def add_process_liveness(self, processes: Set[Process], ts_ns: int) -> None:
        self.order.append("liveness")
        super().add_process_liveness(processes, ts_ns)


class TestTheCommandLineReachesTheExporter:
    """The monitor creates a process and hands the exporter what it is
    running in the same call, once (ADR-0025)."""

    def test_it_arrives_once_however_often_the_pid_is_polled(self, exporter: MockExporter) -> None:
        registry = ProcessRegistry(cmdline_provider=lambda pid: ("python", "-m", f"target_{pid}"))

        _drive(
            _monitor(exporter, registry=registry),
            listings=[[], []],
            rings={12345: [_ring(1), _ring(1, 2)]},
        )

        assert exporter.launched == [(proc(DEFAULT_PID), ("python", "-m", "target_12345"))]


class TestARetirementIsReported:
    """The exporter is told the moment gcmon lets go of a process, so it can
    draw that process's row without waiting for the end of the run. A run
    killed mid-flight keeps every row already written (ADR-0011).

    Both ways out of the registry report it: a pid that left the tree, and one
    the wait policy gave up on.
    """

    def test_a_pid_that_leaves_the_tree_is_reported(self, exporter: MockExporter) -> None:
        _drive(
            _monitor(exporter),
            listings=[[999], []],
            rings={12345: [_ring(1), _ring(1)], 999: [_ring(1)]},
        )

        assert [process.pid for process in exporter.retired] == [999]

    def test_several_leaving_in_one_tick_are_reported_in_pid_order(self, exporter: MockExporter) -> None:
        """Not in the order they were first polled, which is the order the
        child listing happened to give."""
        _drive(
            _monitor(exporter),
            listings=[[999, 555, 777], []],
            rings={12345: [_ring(1), _ring(1)], 999: [_ring(1)], 555: [_ring(1)], 777: [_ring(1)]},
        )

        assert [process.pid for process in exporter.retired] == [555, 777, 999]

    def test_a_pid_the_policy_gave_up_on_is_reported(self, exporter: MockExporter) -> None:
        factory = Mock(side_effect=[_policy(True, True), _policy(True, False)])

        _drive(
            _monitor(exporter, wait_policy_factory=factory),
            listings=[[999], [999]],
            rings={12345: [_ring(1), _ring(1)], 999: [_ring(1), TargetUnavailable("gone")]},
        )

        assert [process.pid for process in exporter.retired] == [999]

    def test_a_process_still_running_is_not_reported(self, exporter: MockExporter) -> None:
        """The report is a retirement, not a tick. Reporting a live process
        would draw its bar over an interval it has not finished."""
        _drive(
            _monitor(exporter),
            listings=[[999], [999]],
            rings={12345: [_ring(1), _ring(1)], 999: [_ring(1), _ring(1)]},
        )

        assert exporter.retired == []

    def test_reported_once_per_process(self, exporter: MockExporter) -> None:
        """The pid leaves and comes back as a second process. Two reports for
        one epoch would draw a row's bar twice."""
        _drive(
            _monitor(exporter),
            listings=[[999], [], [999], []],
            rings={
                12345: [_ring(1), _ring(1), _ring(1), _ring(1)],
                999: [_ring(1), _ring(1, ts_base=3 * TICK_NS)],
            },
        )

        assert [(process.pid, process.pid_epoch) for process in exporter.retired] == [(999, 1), (999, 2)]


class TestTheExporterIsNotReachableThroughTheMonitor:
    def test_there_is_no_exporter_property(self, exporter: MockExporter) -> None:
        """It existed for one caller, the loop's liveness call, which is now
        inside. The only code emitting to the exporter is the code that owns
        what it emits."""
        assert not hasattr(_monitor(exporter), "exporter")


class TestAPidThePolicyGaveUpOn:
    """Two halves of one rule, and they pull in opposite directions: the
    cursors go, the policy stays. A test that only counts policies would pass
    with the cursors kept, and one that only watched the cursors would pass
    with the policy replaced.
    """

    def test_no_fresh_policy_replaces_it(self, exporter: MockExporter) -> None:
        """A replacement would not have seen the pid alive, so it would answer
        "still starting" to every further invalid poll and hold the run open
        for a whole startup timeout.

        Three ticks, not two: the policy gives up on the second, so the third
        is the only one that can observe whether a replacement was built. With
        two, the factory could not have been called again either way and the
        count held for the wrong reason.
        """
        factory = Mock(side_effect=[_policy(True, True, True), _policy(True, False, False)])

        _drive(
            _monitor(exporter, wait_policy_factory=factory),
            listings=[[999], [999], [999]],
            rings={
                12345: [_ring(1), _ring(1), _ring(1)],
                999: [_ring(1), TargetUnavailable("gone"), TargetUnavailable("still gone")],
            },
        )

        # One each for 12345 and 999, and none built to replace 999.
        assert factory.call_count == 2

    def test_its_cursors_go(self, exporter: MockExporter) -> None:
        """The other half. A pid still listed as a child whose policy has quit
        is finished as far as gcmon is concerned, so what comes back under that
        number is a new process and must not be measured against the old one's
        counter. Kept cursors would answer a fresh low `collections` with a
        loss window for collections that never happened.
        """
        factory = Mock(side_effect=[_policy(True, True, True), _policy(True, False, True)])

        _drive(
            _monitor(exporter, wait_policy_factory=factory),
            listings=[[999], [999], [999]],
            rings={
                12345: [_ring(1), _ring(1), _ring(1)],
                # Reaches 300, dies, and the number comes back on a process
                # counting from 1 again.
                999: [_ring(299, 300), TargetUnavailable("gone"), _ring(1, 2, ts_base=3 * TICK_NS)],
            },
        )

        assert exporter.loss_events == []
        assert [event.collections for event in exporter.events_by_pid[999]] == [299, 300, 1, 2]

    def test_the_run_can_end_on_it(self, exporter: MockExporter) -> None:
        """Every policy giving up in one tick is how a run ends: nothing
        answered, so nothing is live and nothing holds the run open."""
        factory = Mock(side_effect=[_policy(False), _policy(False)])

        reports = _drive(
            _monitor(exporter, wait_policy_factory=factory),
            listings=[[999]],
            rings={12345: [TargetUnavailable("gone")], 999: [TargetUnavailable("gone too")]},
        )

        assert reports[0].live == frozenset()
        assert not reports[0].keep_running
        assert exporter.liveness == [], "an observation of nothing widens no span"


class TestAPidGcmonCannotRead:
    """A pid enters the registry on a read that returned (ADR-0025).

    One created per attempt spent an epoch and a command-line read on every
    poll of a pid that never became readable, for as long as it stayed in the
    tree. A benchmark run at `--rate 0.001` reached `22048#13` that way, and
    12704 of its 12768 registry entries were retired without ever having read
    anything.
    """

    def test_it_enters_no_process(self, exporter: MockExporter) -> None:
        monitor = _monitor(exporter)

        _drive(
            monitor,
            listings=[[999], [999], [999]],
            rings={
                12345: [_ring(1), _ring(1), _ring(1)],
                999: [TargetUnavailable("not started")] * 3,
            },
        )

        assert monitor._processes.live() == frozenset({proc(DEFAULT_PID)})
        # `at` is what the control plane files evidence through, and it reads
        # the retired processes too: one created per attempt leaves three
        # there, so the pid answers with a process that never existed.
        assert monitor._processes.at(999, 3 * TICK_NS) is None

    def test_it_spends_no_epoch(self, exporter: MockExporter) -> None:
        """The number a process is drawn under, so this is what a `#13` on a
        Perfetto row costs when it is wrong: it claims twelve processes held
        the pid before this one."""
        monitor = _monitor(exporter)

        _drive(
            monitor,
            listings=[[999], [999], [999], [999]],
            rings={
                12345: [_ring(1), _ring(1), _ring(1), _ring(1)],
                999: [
                    TargetUnavailable("not started"),
                    TargetUnavailable("not started"),
                    TargetUnavailable("not started"),
                    _ring(1),
                ],
            },
        )

        assert monitor._processes.current(999) == proc(999)

    def test_it_costs_no_cmdline_read(self, exporter: MockExporter) -> None:
        """`psutil` reads the command line as the process is created, which on
        Windows walks the target's environment block. Once per process, not
        once per poll of a pid that answers nothing."""
        asked: list[int] = []

        def cmdline(pid: int) -> tuple[str, ...]:
            asked.append(pid)
            return ("python",)

        monitor = _monitor(exporter, registry=ProcessRegistry(cmdline))

        _drive(
            monitor,
            listings=[[999], [999], [999]],
            rings={
                12345: [_ring(1), _ring(1), _ring(1)],
                999: [TargetUnavailable("not started")] * 3,
            },
        )

        assert asked == [12345]


class TestNoWaitPolicyThroughAWholeTick:
    """`no_wait_policy` is what the eight poll-only monitors in this suite
    configure, so what it does to a tick is worth stating once."""

    def test_a_successful_poll_keeps_the_run_open(self, exporter: MockExporter) -> None:
        reports = _drive(_monitor(exporter), listings=[[]], rings={12345: [_ring(1)]})

        assert reports[0].live == frozenset({proc(DEFAULT_PID)})
        assert reports[0].keep_running

    def test_a_failed_poll_ends_it(self, exporter: MockExporter) -> None:
        """Which is why anything monitoring a target that may still be
        initializing configures `StartupTimeoutPolicy` instead."""
        reports = _drive(
            _monitor(exporter),
            listings=[[]],
            rings={12345: [TargetUnavailable("still starting")]},
        )

        assert not reports[0].keep_running


class TestTheConstructorRefusesToPickAPolicy:
    def test_a_monitor_cannot_be_built_without_one(
        self, exporter: MockExporter, process: ExternalProcess, stats: StreamingStats
    ) -> None:
        """Omitting it used to yield a monitor that gave up on the first failed
        poll and reported that as an orderly finish."""
        with pytest.raises(TypeError):
            EventsMonitor(process, exporter, stats, reader=FakeEventsReader())  # type: ignore[call-arg]


class TestTheConstructorRefusesToPickAReader:
    def test_a_monitor_cannot_be_built_without_one(
        self, exporter: MockExporter, process: ExternalProcess, stats: StreamingStats
    ) -> None:
        """A default would build a real reader, and a test that forgot to inject
        one would attach to whatever process holds the integer it used as a pid.
        Every pid in this file is such an integer."""
        with pytest.raises(TypeError):
            EventsMonitor(process, exporter, stats, wait_policy_factory=no_wait_policy)  # type: ignore[call-arg]

    def test_neither_argument_is_optional(
        self, exporter: MockExporter, process: ExternalProcess, stats: StreamingStats
    ) -> None:
        with pytest.raises(TypeError):
            EventsMonitor(process, exporter, stats)  # type: ignore[call-arg]
