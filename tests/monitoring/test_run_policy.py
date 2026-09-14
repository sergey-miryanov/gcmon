from collections.abc import Callable, Generator
from unittest.mock import Mock, patch

import pytest

from gcmon.monitoring.run_policy import DurationRunner, InfinityRunner, RunnerFactory


@pytest.fixture
def mock_monotonic() -> Generator[Mock]:
    with patch("time.monotonic") as mock:
        yield mock


@pytest.fixture
def stop_never() -> Callable[[], bool]:
    return lambda: False


@pytest.fixture
def infinity_runner() -> InfinityRunner:
    return InfinityRunner()


@pytest.fixture
def make_duration_runner() -> Callable[..., DurationRunner]:
    def _make(duration: float = 0.01) -> DurationRunner:
        return DurationRunner(duration)

    return _make


class TestInfinityRunner:
    def test_yields_while_not_stopped(self, infinity_runner: InfinityRunner, stop_never: Callable[[], bool]) -> None:
        gen = infinity_runner.run(stop_never)
        next(gen)
        next(gen)
        next(gen)

    def test_stops_when_stopped(self, infinity_runner: InfinityRunner) -> None:
        flag = [False]
        gen = infinity_runner.run(lambda: flag[0])
        next(gen)
        flag[0] = True
        with pytest.raises(StopIteration):
            next(gen)


class TestDurationRunner:
    def test_stops_after_duration(
        self, make_duration_runner: Callable[..., DurationRunner], mock_monotonic: Mock
    ) -> None:
        mock_monotonic.side_effect = [0, 0.6]
        runner = make_duration_runner(0.5)
        gen = runner.run(lambda: False)
        results = list(gen)
        assert len(results) == 1

    def test_stops_early_if_stop_flag(self, make_duration_runner: Callable[..., DurationRunner]) -> None:
        flag = [False]
        gen = make_duration_runner(10).run(lambda: flag[0])
        next(gen)
        flag[0] = True
        with pytest.raises(StopIteration):
            next(gen)

    def test_zero_duration(self, make_duration_runner: Callable[..., DurationRunner], mock_monotonic: Mock) -> None:
        mock_monotonic.side_effect = [100.0, 100.01]
        runner = make_duration_runner(0)
        gen = runner.run(lambda: False)
        results = list(gen)
        assert len(results) == 1

    def test_negative_duration(self, make_duration_runner: Callable[..., DurationRunner], mock_monotonic: Mock) -> None:
        mock_monotonic.side_effect = [100.0, 100.0]
        runner = make_duration_runner(-1.0)
        gen = runner.run(lambda: False)
        next(gen)
        with pytest.raises(StopIteration):
            next(gen)


class TestRunnerFactory:
    def test_none_returns_infinity(self) -> None:
        assert isinstance(RunnerFactory(None), InfinityRunner)

    def test_with_duration_returns_duration(self) -> None:
        assert isinstance(RunnerFactory(5.0), DurationRunner)

    def test_zero_duration_returns_duration(self) -> None:
        assert isinstance(RunnerFactory(0), DurationRunner)
