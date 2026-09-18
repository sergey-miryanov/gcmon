"""Tests for RssSampler: interval timing, live-PID filtering, injectable provider."""

from __future__ import annotations

import logging
from collections.abc import Generator
from unittest.mock import MagicMock, patch

import pytest

from gcmon.monitoring.rss_sampler import RssSampler, _default_rss_sampler, _noop_rss_sampler
from gcmon.support.vocabulary import PROGRAM_NAME
from tests.helpers import proc

# The process the sampler is pointed at.
TARGET_PID: int = 1

SEC = 1_000_000_000
"""One second in nanoseconds, the unit `tick` now speaks."""


@pytest.fixture
def no_psutil() -> Generator[None]:
    """Temporarily remove psutil from sys.modules so import psutil raises ImportError."""
    with patch.dict("sys.modules", {"psutil": None}):
        yield


@pytest.fixture
def mock_psutil() -> Generator[MagicMock]:
    """Create a mock psutil module and inject it into sys.modules.

    The mock has real ``NoSuchProcess`` and ``AccessDenied`` exception
    types so the code under test can catch them.  Tests configure the
    mock's ``Process`` return / side-effect before calling into the
    sampler.
    """
    mock = MagicMock()
    mock.NoSuchProcess = type("NoSuchProcess", (Exception,), {})
    mock.AccessDenied = type("AccessDenied", (Exception,), {})
    with patch.dict("sys.modules", {"psutil": mock}):
        yield mock


class TestRssSampler:
    """RssSampler unit tests; all use injectable rss_provider, no psutil dependency."""

    def test_a_round_with_nobody_live_does_not_use_up_the_interval(self) -> None:
        """Sampling nobody draws nothing either way. What an empty round must
        not do is push the next real one a whole interval out."""
        exporter = MagicMock()
        sampler = RssSampler(exporter, interval=5.0, rss_provider=lambda pid: 42)
        sampler.tick(now_ns=5 * SEC, live=set())

        sampler.tick(now_ns=6 * SEC, live={proc(TARGET_PID)})

        exporter.add_rss_sample.assert_called_once_with(proc(TARGET_PID), 42, 6 * SEC)

    def test_a_due_tick_samples_every_live_process(self) -> None:
        """Sampling occurs when interval has elapsed."""
        exporter = MagicMock()
        calls: list[int] = []

        def provider_fn(pid: int) -> int:
            calls.append(pid)
            return 42

        sampler = RssSampler(exporter, interval=1.0, rss_provider=provider_fn)
        sampler._last_sample_ns = 0

        sampler.tick(now_ns=2 * SEC, live={proc(101), proc(102)})

        assert sorted(calls) == [101, 102]
        sampled = sorted(call.args for call in exporter.add_rss_sample.call_args_list)
        assert sampled == [(proc(101), 42, 2 * SEC), (proc(102), 42, 2 * SEC)]

    def test_a_tick_inside_the_interval_does_not_sample(self) -> None:
        exporter = MagicMock()
        sampler = RssSampler(exporter, interval=5.0, rss_provider=lambda pid: 42)
        sampler._last_sample_ns = 0

        sampler.tick(now_ns=1 * SEC, live={proc(TARGET_PID)})

        exporter.add_rss_sample.assert_not_called()

    def test_the_first_tick_past_the_interval_samples(self) -> None:
        exporter = MagicMock()
        sampler = RssSampler(exporter, interval=5.0, rss_provider=lambda pid: 42)
        sampler._last_sample_ns = 0
        sampler.tick(now_ns=1 * SEC, live={proc(TARGET_PID)})

        sampler.tick(now_ns=10 * SEC, live={proc(TARGET_PID)})

        exporter.add_rss_sample.assert_called_once()

    def test_provider_returns_zero_skips_exporter(self) -> None:
        """When the provider returns 0, exporter is not called (0 = unreachable)."""
        exporter = MagicMock()
        sampler = RssSampler(exporter, interval=0.0, rss_provider=_noop_rss_sampler)
        sampler._last_sample_ns = -1 * SEC

        sampler.tick(now_ns=0, live={proc(TARGET_PID)})

        exporter.add_rss_sample.assert_not_called()

    def test_provider_exception_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        """Exception in provider is caught and logged at DEBUG."""
        exporter = MagicMock()

        def failing_provider(pid: int) -> int:
            raise RuntimeError("oops")

        sampler = RssSampler(exporter, interval=0.0, rss_provider=failing_provider)
        logger = logging.getLogger(PROGRAM_NAME)
        logger.setLevel(logging.DEBUG)
        sampler._last_sample_ns = -1 * SEC

        sampler.tick(now_ns=0, live={proc(TARGET_PID)})

        exporter.add_rss_sample.assert_not_called()
        assert "Could not sample RSS for PID 1" in caplog.text

    def test_tick_updates_last_sample(self) -> None:
        """last_sample is updated after a sampling round."""
        exporter = MagicMock()
        sampler = RssSampler(exporter, interval=1.0, rss_provider=lambda pid: 42)
        sampler._last_sample_ns = 0

        sampler.tick(now_ns=5 * SEC, live={proc(TARGET_PID)})

        assert sampler._last_sample_ns == 5 * SEC

    def test_sample_carries_the_instant_the_round_was_given(self) -> None:
        """The sampler reads no clock of its own. The caller's tick instant is
        what every sample in the round is stamped with."""
        exporter = MagicMock()
        sampler = RssSampler(exporter, interval=0.0, rss_provider=lambda pid: 42)

        sampler.tick(now_ns=987_654_321, live={proc(42)})

        exporter.add_rss_sample.assert_called_once_with(proc(42), 42, 987_654_321)

    def test_one_round_lands_on_one_instant(self) -> None:
        """Every pid in a round shares a timestamp, so their Perfetto lifetime
        spans nest instead of being clipped in set-iteration order (ADR-0011).
        """
        exporter = MagicMock()
        sampler = RssSampler(exporter, interval=0.0, rss_provider=lambda pid: 42)

        sampler.tick(now_ns=5 * SEC, live={proc(TARGET_PID), proc(2), proc(3), proc(4)})

        stamps = {call[0][2] for call in exporter.add_rss_sample.call_args_list}
        assert stamps == {5 * SEC}

    def test_injectable_provider_with_multiple_pids(self) -> None:
        """All live PIDs are sampled in one tick."""
        exporter = MagicMock()
        results: dict[int, int] = {1: 100, 2: 200, 3: 300}
        sampler = RssSampler(
            exporter,
            interval=0.0,
            rss_provider=lambda pid: results[pid],
        )
        sampler._last_sample_ns = -1 * SEC

        sampler.tick(now_ns=0, live={proc(TARGET_PID), proc(2), proc(3)})

        sampled = sorted(call.args for call in exporter.add_rss_sample.call_args_list)
        assert sampled == [(proc(TARGET_PID), 100, 0), (proc(2), 200, 0), (proc(3), 300, 0)]

    def test_enabled_flag(self) -> None:
        """Disabled sampler does nothing even with high interval."""
        exporter = MagicMock()
        sampler = RssSampler(exporter, interval=0.0, rss_provider=lambda pid: 42)
        sampler._enabled = False

        sampler.tick(now_ns=0, live={proc(TARGET_PID)})

        exporter.add_rss_sample.assert_not_called()

    def test_default_provider_uses_default_rss_sampler(self) -> None:
        """With rss_provider=None and psutil available, _default_rss_sampler is used."""
        exporter = MagicMock()

        sampler = RssSampler(exporter, interval=0.0)

        assert sampler._enabled
        assert sampler._provider is _default_rss_sampler

    def test_psutil_unavailable_fallback(
        self,
        caplog: pytest.LogCaptureFixture,
        no_psutil: None,
    ) -> None:
        """When psutil is missing, RssSampler disables and uses _noop_rss_sampler."""
        exporter = MagicMock()

        sampler = RssSampler(exporter, interval=0.0)

        assert not sampler._enabled
        assert sampler._provider is _noop_rss_sampler

        sampler.tick(now_ns=1 * SEC, live={proc(TARGET_PID)})
        exporter.add_rss_sample.assert_not_called()

        assert "psutil not available" in caplog.text


class TestDefaultRssSamplerMocked:
    """_default_rss_sampler unit tests with a mocked psutil (no real psutil needed)."""

    def test_returns_rss_value(self, mock_psutil: MagicMock) -> None:
        mock_psutil.Process.return_value.memory_info.return_value.rss = 42 * 4096

        result = _default_rss_sampler(123)

        assert result == 42 * 4096

    def test_zero_on_no_such_process(self, mock_psutil: MagicMock) -> None:
        mock_psutil.Process.side_effect = mock_psutil.NoSuchProcess(999)

        result = _default_rss_sampler(999)

        assert result == 0

    def test_zero_on_access_denied(self, mock_psutil: MagicMock) -> None:
        mock_psutil.Process.side_effect = mock_psutil.AccessDenied(999)

        result = _default_rss_sampler(999)

        assert result == 0


class TestDefaultRssSamplerIntegration:
    """Integration-light tests for _default_rss_sampler (requires psutil)."""

    def test_default_sampler_returns_int(self) -> None:
        result = _default_rss_sampler(__import__("os").getpid())

        assert isinstance(result, int)
        assert result > 0

    def test_default_sampler_zero_for_invalid_pid(self) -> None:
        result = _default_rss_sampler(999_999_999)

        assert result == 0
