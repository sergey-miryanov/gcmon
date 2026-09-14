from collections.abc import Callable, Generator
from unittest.mock import Mock, patch

import pytest

from gcmon.model.poll_status import PollStatus
from gcmon.monitoring.wait_policy import NoWaitPolicy, StartupTimeoutPolicy
from gcmon.monitoring.wait_policy import no_wait_policy as make_no_wait_policy


@pytest.fixture
def mock_monotonic() -> Generator[Mock]:
    with patch("time.monotonic") as mock:
        yield mock


@pytest.fixture
def no_wait_policy() -> NoWaitPolicy:
    return NoWaitPolicy()


@pytest.fixture
def make_policy() -> Callable[..., StartupTimeoutPolicy]:
    def _make(timeout: int = 5) -> StartupTimeoutPolicy:
        return StartupTimeoutPolicy(timeout)

    return _make


class TestNoWaitPolicy:
    def test_ok_returns_true(self, no_wait_policy: NoWaitPolicy) -> None:
        assert no_wait_policy.wait(PollStatus.OK) is True

    @pytest.mark.parametrize("status", [PollStatus.FAIL, PollStatus.INVALID_PROCESS])
    def test_others_return_false(self, no_wait_policy: NoWaitPolicy, status: PollStatus) -> None:
        assert no_wait_policy.wait(status) is False


class TestTheNoWaitPolicyFactory:
    """`EventsMonitor` takes a factory rather than a policy, since it builds
    one per pid. The class object would satisfy `WaitPolicyFactory` at runtime
    but not to a type checker, so callers name this function instead."""

    def test_it_builds_a_fresh_policy_each_call(self) -> None:
        first, second = make_no_wait_policy(), make_no_wait_policy()

        assert isinstance(first, NoWaitPolicy)
        assert first is not second


class TestStartupTimeoutPolicy:
    def test_ok_returns_true_sets_alive(self, make_policy: Callable[..., StartupTimeoutPolicy]) -> None:
        policy = make_policy()
        assert policy.wait(PollStatus.OK) is True
        assert policy._has_seen_alive

    def test_fail_returns_false(self, make_policy: Callable[..., StartupTimeoutPolicy]) -> None:
        assert make_policy().wait(PollStatus.FAIL) is False

    def test_invalid_process_before_timeout(
        self, make_policy: Callable[..., StartupTimeoutPolicy], mock_monotonic: Mock
    ) -> None:
        mock_monotonic.side_effect = [100.0, 103.0]
        policy = make_policy(5)
        assert policy.wait(PollStatus.INVALID_PROCESS) is True

    def test_invalid_process_after_timeout(
        self, make_policy: Callable[..., StartupTimeoutPolicy], mock_monotonic: Mock
    ) -> None:
        mock_monotonic.side_effect = [100.0, 110.0]
        policy = make_policy(5)
        assert policy.wait(PollStatus.INVALID_PROCESS) is False

    def test_invalid_process_at_exact_timeout(
        self, make_policy: Callable[..., StartupTimeoutPolicy], mock_monotonic: Mock
    ) -> None:
        mock_monotonic.side_effect = [100.0, 105.0]
        policy = make_policy(5)
        assert policy.wait(PollStatus.INVALID_PROCESS) is False

    def test_invalid_process_after_seen_alive(self, make_policy: Callable[..., StartupTimeoutPolicy]) -> None:
        policy = make_policy()
        policy.wait(PollStatus.OK)
        assert policy.wait(PollStatus.INVALID_PROCESS) is False

    def test_timeout_zero(self, make_policy: Callable[..., StartupTimeoutPolicy], mock_monotonic: Mock) -> None:
        mock_monotonic.side_effect = [100.0, 100.0]
        policy = make_policy(0)
        assert policy.wait(PollStatus.INVALID_PROCESS) is False

    def test_unknown_status_raises_value_error(self, make_policy: Callable[..., StartupTimeoutPolicy]) -> None:
        with pytest.raises(ValueError, match="Unknown status"):
            make_policy().wait(999)  # type: ignore[arg-type]

    def test_multiple_ok(self, make_policy: Callable[..., StartupTimeoutPolicy]) -> None:
        policy = make_policy()
        assert policy.wait(PollStatus.OK) is True
        assert policy.wait(PollStatus.OK) is True

    def test_float_timeout(self, make_policy: Callable[..., StartupTimeoutPolicy], mock_monotonic: Mock) -> None:
        mock_monotonic.side_effect = [100.0, 102]
        policy = make_policy(3)
        assert policy.wait(PollStatus.INVALID_PROCESS) is True

    def test_float_timeout_expired(
        self, make_policy: Callable[..., StartupTimeoutPolicy], mock_monotonic: Mock
    ) -> None:
        mock_monotonic.side_effect = [100.0, 104.0]
        policy = make_policy(3)
        assert policy.wait(PollStatus.INVALID_PROCESS) is False
