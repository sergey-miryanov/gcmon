"""Tests for child-side control plane API."""

import os
from collections.abc import Generator
from itertools import count
from unittest.mock import MagicMock, patch

import pytest

from gcmon.control.control_client import ControlClient, _default_connect, connect_with_retry
from gcmon.control.control_server import CONTROL_ADDRESS_ENV
from gcmon.control.protocol import MSG, MSG_START, MSG_STOP
from gcmon.model.names import PID, TS


def assert_payload(mock_conn: MagicMock, expected_msg: str, *, call_index: int = 0) -> dict[str, int | str]:
    payload: dict[str, int | str] = mock_conn.send.call_args_list[call_index][0][0]
    assert payload[MSG] == expected_msg
    assert payload[PID] == os.getpid()
    assert isinstance(payload[TS], int)
    return payload


def assert_stopped_then_started(mock_conn: MagicMock) -> None:
    assert mock_conn.send.call_count == 2
    assert mock_conn.send.call_args_list[0][0][0][MSG] == MSG_STOP
    assert mock_conn.send.call_args_list[1][0][0][MSG] == MSG_START


@pytest.fixture
def mock_conn() -> MagicMock:
    return MagicMock()


@pytest.fixture
def mock_connection_factory(mock_conn: MagicMock) -> MagicMock:
    return MagicMock(return_value=mock_conn)


@pytest.fixture
def client(mock_connection_factory: MagicMock) -> ControlClient:
    return ControlClient("test-address", connection_factory=mock_connection_factory)


@pytest.fixture
def disconnected_client() -> ControlClient:
    return ControlClient("addr", connection_factory=MagicMock(return_value=None))


@pytest.fixture
def mock_sleep() -> Generator[None]:
    with patch("gcmon.control.control_client.time.sleep"):
        yield


@pytest.fixture
def one_attempt_clock(mock_sleep: Generator[None]) -> Generator[None]:
    """A clock that lets one attempt in ahead of the default deadline."""
    with patch("gcmon.control.control_client.time.monotonic", side_effect=count(0.0, 3.0)):
        yield


@pytest.fixture
def patched_client_factory(mock_sleep: Generator[None]) -> Generator[MagicMock]:
    with patch("gcmon.control.control_client.Client") as mock_client:
        yield mock_client


class TestPublicAPI:
    @pytest.mark.parametrize(
        "method, args, expected_msg",
        [
            ("start_monitoring", (), MSG_START),
            ("stop_monitoring", (), MSG_STOP),
            ("instant_msg", ("custom event",), "custom event"),
        ],
    )
    def test_sends_payload(
        self, client: ControlClient, mock_conn: MagicMock, method: str, args: tuple[str, ...], expected_msg: str
    ) -> None:
        getattr(client, method)(*args)
        mock_conn.send.assert_called_once()
        assert_payload(mock_conn, expected_msg)

    def test_pause_monitoring_stops_and_starts_again(self, client: ControlClient, mock_conn: MagicMock) -> None:
        with client.pause_monitoring():
            pass

        assert_stopped_then_started(mock_conn)

    def test_pause_monitoring_starts_again_when_the_body_raises(
        self, client: ControlClient, mock_conn: MagicMock
    ) -> None:
        """The exception leaves the target running, not paused for good."""
        with pytest.raises(RuntimeError), client.pause_monitoring():
            raise RuntimeError()

        assert_stopped_then_started(mock_conn)


class TestInstantMsg:
    def test_stamps_at_send_time_without_ts(self, client: ControlClient, mock_conn: MagicMock) -> None:
        with patch("gcmon.control.control_client.time.monotonic_ns", return_value=98765):
            client.instant_msg("mark")
        assert assert_payload(mock_conn, "mark")[TS] == 98765

    def test_carries_the_given_ts(self, client: ControlClient, mock_conn: MagicMock) -> None:
        with patch("gcmon.control.control_client.time.monotonic_ns", return_value=98765):
            client.instant_msg("mark", ts=111)
        assert assert_payload(mock_conn, "mark")[TS] == 111

    def test_a_later_send_does_not_inherit_the_ts(self, client: ControlClient, mock_conn: MagicMock) -> None:
        with patch("gcmon.control.control_client.time.monotonic_ns", return_value=98765):
            client.instant_msg("first", ts=111)
            client.instant_msg("second")
        assert assert_payload(mock_conn, "second", call_index=1)[TS] == 98765


class TestSend:
    def test_uses_monotonic_ns(self, client: ControlClient, mock_conn: MagicMock) -> None:
        with patch("gcmon.control.control_client.time.monotonic_ns", return_value=98765):
            client._send("test")
        mock_conn.send.assert_called_once()
        assert mock_conn.send.call_args[0][0][TS] == 98765

    def test_noop_when_not_connected(self, disconnected_client: ControlClient) -> None:
        disconnected_client._send("test")
        assert disconnected_client._conn is None

    def test_clears_stale_connection_on_failure(self, client: ControlClient, mock_conn: MagicMock) -> None:
        mock_conn.send.side_effect = OSError("broken pipe")
        client._send("test")
        assert client._conn is None
        mock_conn.close.assert_called_once()

    def test_reconnects_after_cleared_connection(
        self, mock_connection_factory: MagicMock, mock_conn: MagicMock
    ) -> None:
        mock_conn.send.side_effect = OSError("broken pipe")
        client = ControlClient("test-address", connection_factory=mock_connection_factory)
        client._send("test")
        assert mock_connection_factory.call_count == 1
        mock_conn.send.side_effect = None
        client._send("retry")
        assert mock_connection_factory.call_count == 2


class TestConnectionLifecycle:
    def test_ensure_connected_creates_once(self, mock_connection_factory: MagicMock, mock_conn: MagicMock) -> None:
        client = ControlClient("test-address", connection_factory=mock_connection_factory)
        result1 = client._ensure_connected()
        result2 = client._ensure_connected()
        assert result1 is mock_conn
        assert result2 is mock_conn
        mock_connection_factory.assert_called_once_with("test-address")

    def test_ensure_connected_returns_none_without_address(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(CONTROL_ADDRESS_ENV, raising=False)
        client = ControlClient(connection_factory=MagicMock())
        assert client._ensure_connected() is None

    def test_ensure_connected_falls_back_to_env_var(
        self, monkeypatch: pytest.MonkeyPatch, mock_connection_factory: MagicMock, mock_conn: MagicMock
    ) -> None:
        monkeypatch.setenv(CONTROL_ADDRESS_ENV, "env-address")
        client = ControlClient(connection_factory=mock_connection_factory)
        assert client._ensure_connected() is mock_conn
        mock_connection_factory.assert_called_once_with("env-address")

    def test_close_closes_connection(self, client: ControlClient, mock_conn: MagicMock) -> None:
        client._ensure_connected()
        client.close()
        mock_conn.close.assert_called_once()
        assert client._conn is None

    def test_close_safe_to_call_multiple_times(self, client: ControlClient, mock_conn: MagicMock) -> None:
        client._ensure_connected()
        client.close()
        client.close()
        mock_conn.close.assert_called_once()

    def test_close_noop_when_not_connected(self, disconnected_client: ControlClient) -> None:
        disconnected_client.close()
        assert disconnected_client._conn is None

    def test_context_manager_closes_on_exit(self, client: ControlClient, mock_conn: MagicMock) -> None:
        client._ensure_connected()
        with client:
            pass
        mock_conn.close.assert_called_once()
        assert client._conn is None


class TestConnectWithRetry:
    def test_connects_on_first_attempt(self, patched_client_factory: MagicMock) -> None:
        mock_conn = MagicMock()
        patched_client_factory.return_value = mock_conn
        result = connect_with_retry("test-address")
        assert result is mock_conn
        patched_client_factory.assert_called_once_with("test-address")

    def test_retries_on_failure_and_succeeds(
        self, patched_client_factory: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_conn = MagicMock()
        patched_client_factory.side_effect = [OSError("conn refused"), mock_conn]
        result = connect_with_retry("test-address")
        assert result is mock_conn
        assert patched_client_factory.call_count == 2
        assert "Failed to connect to control plane" not in caplog.text

    def test_returns_none_after_timeout(
        self, patched_client_factory: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        patched_client_factory.side_effect = OSError("conn refused")
        result = connect_with_retry("test-address", timeout=0.1)
        assert result is None
        assert "Failed to connect to control plane" in caplog.text
        assert "address='test-address'" in caplog.text
        assert "conn refused" in caplog.text


class TestDefaultConnect:
    def test_returns_none_on_connection_failure(
        self, caplog: pytest.LogCaptureFixture, one_attempt_clock: Generator[None]
    ) -> None:
        assert _default_connect("/nonexistent/control/socket") is None
        assert "Failed to connect to control plane" in caplog.text
        assert "address='/nonexistent/control/socket'" in caplog.text
