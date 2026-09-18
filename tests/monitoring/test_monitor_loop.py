"""The loop: timing and shutdown, nothing else.

One tick is one call on the monitor, which answers a `PollReport`. What is left
to test here is what the loop does with that report and with the clock: who it
hands the live set to, when it breaks, and that a stop reaches the monitor
mid-tick. Discovery, the prune, the policies and the enable check belong to the
monitor, and `test_monitor.py` tests them there.
"""

import threading
from itertools import count
from typing import override
from unittest.mock import MagicMock, Mock, patch

import pytest

from gcmon.model.run_report import RunReport
from gcmon.monitoring.monitor import PollReport
from gcmon.monitoring.monitor_loop import MonitorLoop
from gcmon.monitoring.rss_sampler import RssSampler
from gcmon.monitoring.run_policy import InfinityRunner, Runner
from tests.conftest import DEFAULT_PID
from tests.helpers import proc


def _report(*live: int, keep_running: bool = True) -> PollReport:
    return PollReport(live=frozenset(proc(pid) for pid in live), keep_running=keep_running)


@pytest.fixture
def mock_monitor() -> MagicMock:
    monitor = MagicMock()
    monitor.pid = 12345
    monitor.tick.return_value = _report(12345)
    monitor.__enter__.return_value = monitor
    monitor.__exit__.return_value = None
    return monitor


@pytest.fixture
def mock_runner() -> Mock:
    runner = Mock(spec=Runner)
    runner.run.return_value = iter([None])
    return runner


def _runner(ticks: int) -> Mock:
    runner = Mock(spec=Runner)
    runner.run.return_value = iter([None] * ticks)
    return runner


@pytest.fixture
def loop(mock_monitor: MagicMock, mock_runner: Mock) -> MonitorLoop:
    return MonitorLoop(mock_monitor, mock_runner, rate=0.01)


class TestMonitorLoopInitAndClose:
    @pytest.mark.parametrize("rate", [0.0, -0.1, 1e-12])
    def test_a_rate_it_cannot_hold_is_refused(self, mock_monitor: MagicMock, mock_runner: Mock, rate: float) -> None:
        """Zero, negative, and small enough to round to no nanoseconds at all.
        The schedule is arithmetic on a rate, so there is nothing to run. The
        CLI reports these; here it is a precondition."""
        with pytest.raises(AssertionError):
            MonitorLoop(mock_monitor, mock_runner, rate=rate)

    def test_close_sets_stop_event(self, loop: MonitorLoop) -> None:
        assert not loop._stop_event.is_set()

        loop.close()

        assert loop._stop_event.is_set()

    def test_close_idempotent(self, loop: MonitorLoop) -> None:
        loop.close()

        loop.close()

        assert loop._stop_event.is_set()


class TestMonitorLoopRun:
    def test_ticks_the_monitor(self, loop: MonitorLoop, mock_monitor: MagicMock) -> None:
        loop.run()

        mock_monitor.tick.assert_called_once()

    def test_ticks_once_per_iteration(self, mock_monitor: MagicMock) -> None:
        loop = MonitorLoop(mock_monitor, _runner(3), rate=0.01)

        loop.run()

        assert mock_monitor.tick.call_count == 3

    def test_stop_before_run_skips_the_loop(self, mock_monitor: MagicMock) -> None:
        runner = Mock(spec=Runner)
        runner.run.return_value = iter([])
        loop = MonitorLoop(mock_monitor, runner, rate=0.01)
        loop._stop_event.set()

        report = loop.run()

        mock_monitor.tick.assert_not_called()
        assert (report.ticks_run, report.ticks_scheduled) == (0, 0), "a run with no ticks scheduled none"

    def test_breaks_when_the_report_says_to_stop(self, mock_monitor: MagicMock) -> None:
        """The wait policies live in the monitor; `keep_running` is their
        verdict arriving as one answer."""
        mock_monitor.tick.return_value = _report(12345, keep_running=False)
        loop = MonitorLoop(mock_monitor, _runner(3), rate=0.01)

        loop.run()

        mock_monitor.tick.assert_called_once()

    def test_the_stop_event_reaches_the_monitor(self, loop: MonitorLoop, mock_monitor: MagicMock) -> None:
        """A tick polls a whole process tree, so shutdown has to be able to
        interrupt one. The loop owns the event and lends the monitor a read of
        it -- nothing more."""
        loop.run()

        _now_ns, stop = mock_monitor.tick.call_args[0]
        assert stop == loop._stop_event.is_set

    def test_close_during_run(self, mock_monitor: MagicMock) -> None:
        """A rate of a minute, so a run that returns was woken out of its idle."""
        ticked = threading.Event()

        def tick(now_ns: int, stop: object) -> PollReport:
            ticked.set()
            return _report(12345)

        mock_monitor.tick.side_effect = tick
        loop = MonitorLoop(mock_monitor, InfinityRunner(), rate=60)
        t = threading.Thread(target=loop.run, daemon=True)
        t.start()
        assert ticked.wait(timeout=2)

        loop.close()

        t.join(timeout=2)
        assert not t.is_alive()

    def test_stop_event_set_after_normal_exit(self, loop: MonitorLoop) -> None:
        assert not loop._stop_event.is_set()

        loop.run()

        assert loop._stop_event.is_set()


class TestMonitorLoopContextManager:
    def test_exit_calls_close(self, mock_monitor: MagicMock) -> None:
        loop = MonitorLoop(mock_monitor, Mock(spec=Runner))
        assert not loop._stop_event.is_set()

        with loop:
            pass

        assert loop._stop_event.is_set()


class TestRssSamplerInLoop:
    """The sampler is driven once per tick, off the live set the report
    carried and the instant the tick was stamped with (ADR-0013)."""

    def test_tick_called_with_the_live_set(self, mock_monitor: MagicMock) -> None:
        mock_monitor.tick.return_value = _report(12345, 999)
        rss_sampler = Mock(spec=RssSampler)

        MonitorLoop(mock_monitor, _runner(1), rate=0.01, rss_sampler=rss_sampler).run()

        rss_sampler.tick.assert_called_once()
        _now, live = rss_sampler.tick.call_args[0]
        assert live == frozenset({proc(DEFAULT_PID), proc(999)})

    def test_no_sampler_is_not_an_error(self, mock_monitor: MagicMock) -> None:
        MonitorLoop(mock_monitor, _runner(1), rate=0.01).run()

        # No error -- tick is simply not called.

    def test_tick_receives_the_nanosecond_instant(self, mock_monitor: MagicMock) -> None:
        """Nanoseconds, unconverted: the sampler paces in the same unit it
        stamps in, and gcmon's canonical unit is ns (ADR-0009)."""
        rss_sampler = Mock(spec=RssSampler)

        with patch("time.monotonic_ns", return_value=42_000_000_000):
            MonitorLoop(mock_monitor, _runner(1), rate=0.01, rss_sampler=rss_sampler).run()

        rss_sampler.tick.assert_called_once_with(42_000_000_000, frozenset({proc(DEFAULT_PID)}))

    def test_tick_called_each_iteration(self, mock_monitor: MagicMock) -> None:
        rss_sampler = Mock(spec=RssSampler)

        MonitorLoop(mock_monitor, _runner(3), rate=0.01, rss_sampler=rss_sampler).run()

        assert rss_sampler.tick.call_count == 3

    def test_tick_called_even_with_nothing_live(self, mock_monitor: MagicMock) -> None:
        """Unlike liveness, the sampler is called on an empty set and decides
        for itself -- its own interval bookkeeping runs either way."""
        mock_monitor.tick.return_value = _report()
        rss_sampler = Mock(spec=RssSampler)

        MonitorLoop(mock_monitor, _runner(1), rate=0.01, rss_sampler=rss_sampler).run()

        rss_sampler.tick.assert_called_once()
        _now, live = rss_sampler.tick.call_args[0]
        assert live == frozenset()


class TestTheTickInstant:
    """One stamping instant per tick, and everything downstream of it.

    The monitor stamps liveness with it (ADR-0011) and the sampler both paces
    and stamps with it (ADR-0013), so everything one tick emits agrees on when
    the tick was.

    What is guarded here is that one instant, not how many times the loop reads
    the clock. The loop also reads it to pace itself, and that read stamps
    nothing and reaches nothing outside `run`.
    """

    def test_the_tick_is_given_the_instant(self, mock_monitor: MagicMock) -> None:
        with patch("time.monotonic_ns", return_value=42_000_000_000):
            MonitorLoop(mock_monitor, _runner(1), rate=0.01).run()

        now_ns, _stop = mock_monitor.tick.call_args[0]
        assert now_ns == 42_000_000_000

    def test_one_stamping_instant_per_tick_shared_with_the_sampler(self, mock_monitor: MagicMock) -> None:
        """Whatever else the loop reads the clock for, the monitor and the
        sampler are handed one instant per tick, and the same one."""
        rss_sampler = Mock(spec=RssSampler)

        with patch("time.monotonic_ns", side_effect=count(1_500_000_000, 1_000_000)):
            MonitorLoop(mock_monitor, _runner(2), rate=0.01, rss_sampler=rss_sampler).run()

        tick_ns = [c[0][0] for c in mock_monitor.tick.call_args_list]
        sampler_ns = [c[0][0] for c in rss_sampler.tick.call_args_list]
        assert len(tick_ns) == 2, "one stamping instant per tick"
        assert sampler_ns == tick_ns, "no conversion between the two"
        assert tick_ns[0] < tick_ns[1], "a tick is stamped with its own instant, not the run's"

    def test_the_sampler_and_the_monitor_get_the_same_integer_instant(self, mock_monitor: MagicMock) -> None:
        """The loop used to hand the sampler `now_ns / 1e9`, which was the only
        place gcmon converted out of nanoseconds before the encoder."""
        rss_sampler = Mock(spec=RssSampler)

        with patch("time.monotonic_ns", return_value=1_500_000_000):
            MonitorLoop(mock_monitor, _runner(1), rate=0.01, rss_sampler=rss_sampler).run()

        now_ns, _stop = mock_monitor.tick.call_args[0]
        sampler_ns, _pids = rss_sampler.tick.call_args[0]
        assert sampler_ns == now_ns
        assert isinstance(sampler_ns, int)


class _RecordingEvent(threading.Event):
    """A stop event that records what it was asked to wait for, and never sleeps.

    The loop's intended interval is what the tests assert. The achieved one
    carries a scheduler quantum of noise -- up to ~16 ms on Windows, where
    `Event.wait` rounds up to the scheduler tick -- so a test that measured
    elapsed time would be asserting the operating system.
    """

    def __init__(self) -> None:
        super().__init__()
        self.waits: list[float | None] = []

    @override
    def wait(self, timeout: float | None = None) -> bool:
        self.waits.append(timeout)
        return self.is_set()


def _waits(monitor: MagicMock, instants: list[int], ticks: int = 1, rate: float = 0.1) -> list[float | None]:
    """Run the loop over a scripted clock and answer what it waited for.

    *instants* is read in order: one stamping instant then one pacing instant
    per tick, so a tick's cost is the difference between its pair. The loop
    reads once more before its first tick, to seed position zero, and that read
    is served the first instant again so the pairs stay as written.
    """
    loop = MonitorLoop(monitor, _runner(ticks), rate=rate)
    event = _RecordingEvent()
    loop._stop_event = event
    with patch("time.monotonic_ns", side_effect=[instants[0], *instants]):
        loop.run()
    return event.waits


class TestThePace:
    """What the loop does with the schedule the arithmetic hands it.

    It holds `t0` across ticks and waits the idle out on its stop event,
    converted to seconds. Where those numbers come from is asserted above, on
    the arithmetic itself.
    """

    def test_the_wait_subtracts_what_the_tick_cost(self, mock_monitor: MagicMock) -> None:
        """The defect: the loop used to wait the whole rate on top of the tick,
        so the target's size decided how often gcmon looked."""
        waits = _waits(mock_monitor, [0, 30_000_000])

        assert waits == [pytest.approx(0.07)]

    def test_the_grid_keeps_its_phase_across_ticks(self, mock_monitor: MagicMock) -> None:
        """The positions are absolute, so a tick that starts late and runs long
        leaves its successor on the original grid rather than re-basing it."""
        waits = _waits(mock_monitor, [0, 30_000_000, 40_000_000, 160_000_000], ticks=2)

        assert waits == [pytest.approx(0.07), pytest.approx(0.04)]

    def test_a_late_wake_up_does_not_shift_the_grid(self, mock_monitor: MagicMock) -> None:
        """`Event.wait` delivers no earlier than asked and can deliver much
        later. Tick two was due at 100 ms and starts at 250: the wait after it
        goes to 300, the next position on the original grid, not to 350."""
        waits = _waits(mock_monitor, [0, 10_000_000, 250_000_000, 260_000_000], ticks=2)

        assert waits == [pytest.approx(0.09), pytest.approx(0.04)]

    def test_the_stamping_instant_is_not_the_pacing_one(self, mock_monitor: MagicMock) -> None:
        """The pacing read stamps nothing. What reaches the monitor is the
        instant taken before the tick, never the one taken after it."""
        _waits(mock_monitor, [0, 30_000_000])

        now_ns, _stop = mock_monitor.tick.call_args[0]
        assert now_ns == 0


def _report_of(monitor: MagicMock, instants: list[int], ticks: int = 1, rate: float = 0.1) -> RunReport:
    """Run the loop over a scripted clock and answer what `run` returned.

    Instants as `_waits` takes them, seeding read included.
    """
    loop = MonitorLoop(monitor, _runner(ticks), rate=rate)
    loop._stop_event = _RecordingEvent()
    with patch("time.monotonic_ns", side_effect=[instants[0], *instants]):
        return loop.run()


class TestTheRunReport:
    """What the run did with its schedule, answered once at the end.

    A run that overran is indistinguishable from a healthy one otherwise: both
    show low coverage, and only this says whether gcmon ever got to look as
    often as it was asked to.
    """

    def test_a_run_that_kept_up_scheduled_what_it_ran(self, mock_monitor: MagicMock) -> None:
        report = _report_of(mock_monitor, [0, 30_000_000])

        assert report.ticks_run == 1
        assert report.ticks_scheduled == 1
        assert not report.overran

    def test_a_skipped_position_still_counts_as_scheduled(self, mock_monitor: MagicMock) -> None:
        """Two ticks ran where the grid offered three positions, because the
        first tick outlasted its own."""
        report = _report_of(mock_monitor, [0, 150_000_000, 200_000_000, 210_000_000], ticks=2)

        assert report.ticks_run == 2
        assert report.ticks_scheduled == 3
        assert report.overran

    def test_a_late_wake_up_costs_the_positions_it_slept_through(self, mock_monitor: MagicMock) -> None:
        """The same run: two ticks over three positions, because the wait after
        the first slept through the one at 100 ms. Nothing about the target was
        slow, and the report counts it all the same."""
        report = _report_of(mock_monitor, [0, 10_000_000, 250_000_000, 260_000_000], ticks=2)

        assert (report.ticks_run, report.ticks_scheduled) == (2, 3)
        assert report.overran

    def test_a_run_cut_short_reports_the_ticks_it_ran(self, mock_monitor: MagicMock) -> None:
        """The runner offered five; the monitor gave up after two."""
        mock_monitor.tick.side_effect = [_report(12345), _report(keep_running=False)]

        report = _report_of(mock_monitor, [0, 30_000_000, 100_000_000], ticks=5)

        assert report.ticks_run == 2
        assert report.ticks_scheduled == 2

    def test_a_short_run_against_the_floor_has_lost_no_position_yet(self, mock_monitor: MagicMock) -> None:
        """Four ticks costing 0.2 ms against a 1 ms rate: the guard stretches
        every interval to 1.2 ms, and that drift takes five ticks to eat a
        whole position."""
        instants = [0, 200_000, 1_200_000, 1_400_000, 2_400_000, 2_600_000, 3_600_000, 3_800_000]

        report = _report_of(mock_monitor, instants, ticks=4, rate=0.001)

        assert report.ticks_scheduled == 4
        assert not report.overran

    def test_a_rate_the_floor_cannot_serve_reads_as_unreachable(self, mock_monitor: MagicMock) -> None:
        """Ten of those ticks drift past two positions. The operator asked for
        1 ms and gcmon holds 1.2, which no smaller `--rate` fixes, so the run
        has to read as one that did not keep up."""
        instants = [k * 1_200_000 + offset for k in range(10) for offset in (0, 200_000)]

        report = _report_of(mock_monitor, instants, ticks=10, rate=0.001)

        assert (report.ticks_run, report.ticks_scheduled) == (10, 12)
        assert report.overran

    def test_a_tick_that_really_outlasts_its_position_still_counts(self, mock_monitor: MagicMock) -> None:
        """The guard forgiving its own overshoot must not forgive a genuine
        overrun beside it."""
        report = _report_of(mock_monitor, [0, 150_000_000], rate=0.1)

        assert report.ticks_scheduled == 2
        assert report.overran

    def test_the_counters_are_not_public_on_the_loop(self, loop: MonitorLoop) -> None:
        """They leave in the report or not at all, so nothing downstream grows
        a second way to ask."""
        loop.run()

        assert not [name for name in vars(loop) if "tick" in name or "skip" in name]


class TestTheLoopDoesNotTouchTheExporter:
    def test_it_never_reaches_through_the_monitor(self, mock_monitor: MagicMock) -> None:
        """`EventsMonitor.exporter` existed for the loop's liveness call and
        nothing else. The call is inside the tick now, and the property is
        gone -- so the only code emitting to the exporter is the code that
        owns what it emits."""
        MonitorLoop(mock_monitor, _runner(2), rate=0.01).run()

        assert "exporter" not in mock_monitor._mock_children


class TestTheLoopHoldsNoPerPidState:
    """ADR-0017's structural claim, stated where it can regress.

    A second home for any of this is a second prune waiting to be written, and
    two prunes disagreeing is what fabricates a loss window.
    """

    @pytest.mark.parametrize("attribute", ["_wait_policy_factory", "_enabled", "_pid_policies"])
    def test_the_attribute_is_gone(self, loop: MonitorLoop, attribute: str) -> None:
        assert not hasattr(loop, attribute)

    def test_the_constructor_takes_neither_policies_nor_an_enable_predicate(self) -> None:
        import inspect

        parameters = set(inspect.signature(MonitorLoop.__init__).parameters)

        assert parameters == {"self", "monitor", "runner", "rate", "rss_sampler"}


class TestADeadTargetDoesNotExtendTheRun:
    def test_a_stop_on_a_later_tick_ends_the_run_there(self) -> None:
        """The regression a policy deletion once caused, now expressed against
        the report: a target that dies while a child is still alive must not
        keep the loop polling until a fresh startup timeout expires."""
        monitor = MagicMock()
        monitor.pid = 12345
        monitor.tick.side_effect = [
            _report(12345, 999),
            _report(999),
            _report(keep_running=False),
        ]

        loop = MonitorLoop(monitor, _runner(50), rate=0.001)

        loop.run()

        assert monitor.tick.call_count == 3, "the loop kept ticking a finished run"
