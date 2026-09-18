"""Tests for the control plane (ControlServer)."""

import os
import sys
import threading
import time
from collections.abc import Generator
from itertools import count
from multiprocessing.connection import Client, Connection
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gcmon.control.control_server import (
    CONTROL_ADDRESS_ENV,
    ControlServer,
    set_control_env,
)
from gcmon.control.protocol import MSG, MSG_START, MSG_STOP, START_EVENT, STOP_EVENT
from gcmon.model.names import PID, TS
from gcmon.monitoring.process_registry import ProcessRegistry
from tests.helpers import monitored, proc


@pytest.fixture
def server_not_started(mock_exporter: MagicMock) -> Generator[ControlServer]:
    server = ControlServer(mock_exporter, monitored(42))
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def mock_conn() -> MagicMock:
    m: MagicMock = MagicMock()
    m.poll.return_value = False
    return m


@pytest.fixture
def mock_wait() -> Generator[MagicMock]:
    with patch("gcmon.control.control_server._wait") as mock:
        yield mock


@pytest.fixture
def mock_wait_and_stop(server_not_started: ControlServer) -> Generator[MagicMock]:
    with (
        patch("gcmon.control.control_server._wait") as mock,
        patch.object(
            server_not_started._stop_event,
            "wait",
            side_effect=lambda t: server_not_started._stop_event.set(),
        ),
    ):
        yield mock


def _send_msg(server: ControlServer, msg: str, pid: int) -> None:
    import time

    address: str = server.address
    conn = Client(address)
    try:
        conn.send({MSG: msg, PID: pid, TS: time.monotonic_ns()})
    finally:
        conn.close()


def _wait_msg(control_server: ControlServer, pid: int, expected: bool, timeout: int = 1) -> bool:
    ts = time.monotonic()
    while control_server.is_enabled(pid) is not expected:
        time.sleep(0)
        if time.monotonic() - ts > timeout:
            return False
    return True


# =============================================================================
# Init tests
# =============================================================================


class TestControlServerInit:
    def test_address_returns_string(self, control_server: ControlServer) -> None:
        addr = control_server.address
        assert isinstance(addr, str)

    def test_is_enabled_defaults_to_true(self, control_server: ControlServer) -> None:
        assert control_server.is_enabled(99999) is True
        assert control_server.is_enabled(0) is True

    def test_init_with_custom_name(self) -> None:
        server = ControlServer(MagicMock(), ProcessRegistry(), address="my-name")
        try:
            assert "gcmon-my-name" in server.address
        finally:
            server.close()

    def test_init_listener_not_none(self, server_not_started: ControlServer) -> None:
        assert server_not_started._listener is not None

    def test_init_connections_empty(self, server_not_started: ControlServer) -> None:
        assert server_not_started._connections == set()

    def test_init_enabled_empty(self, server_not_started: ControlServer) -> None:
        assert server_not_started._enabled == {}

    def test_init_not_running(self, server_not_started: ControlServer) -> None:
        assert server_not_started._running is False

    def test_init_exporter_set(self, server_not_started: ControlServer) -> None:
        assert server_not_started._exporter

    def test_init_threads_not_alive(self, server_not_started: ControlServer) -> None:
        assert not server_not_started._accept_thread.is_alive()
        assert not server_not_started._reader_thread.is_alive()


# =============================================================================
# Start tests
# =============================================================================


class TestControlServerStartFailure:
    """Regression tests for BUG-30: start() must not leak state on failure."""

    def _stub_threads(self, server: ControlServer, fail_on: int) -> tuple[MagicMock, MagicMock]:
        calls: dict[str, int] = {"n": 0}
        accept: MagicMock = MagicMock(name="accept_thread")
        reader: MagicMock = MagicMock(name="reader_thread")

        def start_side_effect() -> None:
            calls["n"] += 1
            if calls["n"] == fail_on:
                raise RuntimeError(f"simulated start failure on call {fail_on}")

        accept.start.side_effect = start_side_effect
        reader.start.side_effect = start_side_effect
        server._accept_thread = accept
        server._reader_thread = reader
        return accept, reader

    def test_start_when_accept_thread_fails_does_not_mark_running(
        self,
        server_not_started: ControlServer,
    ) -> None:
        _, reader_mock = self._stub_threads(server_not_started, fail_on=1)

        with pytest.raises(RuntimeError, match="simulated start failure on call 1"):
            server_not_started.start()

        assert server_not_started.is_running() is False
        reader_mock.start.assert_not_called()
        assert server_not_started._listener is not None
        assert server_not_started.address
        server_not_started.close()

    def test_start_when_reader_thread_fails_joins_accept_thread(
        self,
        server_not_started: ControlServer,
    ) -> None:
        accept_mock, reader_mock = self._stub_threads(server_not_started, fail_on=2)

        with pytest.raises(RuntimeError, match="simulated start failure on call 2"):
            server_not_started.start()

        assert server_not_started.is_running() is False
        accept_mock.start.assert_called_once()
        reader_mock.start.assert_called_once()
        accept_mock.join.assert_called()
        reader_mock.join.assert_called()
        assert server_not_started._listener is not None
        assert server_not_started.address
        server_not_started.close()

    def test_double_start_raises_and_leaves_state_intact(
        self,
        server_not_started: ControlServer,
    ) -> None:
        self._stub_threads(server_not_started, fail_on=0)
        try:
            server_not_started.start()
            assert server_not_started.is_running() is True
            first_addr = server_not_started.address

            with pytest.raises(RuntimeError, match="already running"):
                server_not_started.start()

            assert server_not_started.is_running() is True
            assert server_not_started.address == first_addr
        finally:
            server_not_started.close()


class TestControlServerStart:
    def test_start_starts_threads(self, control_server: ControlServer) -> None:
        assert control_server._accept_thread.is_alive()
        assert control_server._reader_thread.is_alive()

    def test_accepts_connection(self, control_server: ControlServer) -> None:
        _send_msg(control_server, msg=MSG_STOP, pid=42)
        assert _wait_msg(control_server, pid=42, expected=False)

    def test_start_sets_running(self, server_not_started: ControlServer) -> None:
        server_not_started.start()
        try:
            assert server_not_started.is_running()
        finally:
            server_not_started.close()

    def test_start_clears_stop_event(self, server_not_started: ControlServer) -> None:
        server_not_started._stop_event.set()
        server_not_started.start()
        try:
            assert not server_not_started._stop_event.is_set()
        finally:
            server_not_started.close()

    def test_start_twice_raises(self, control_server: ControlServer) -> None:
        with pytest.raises(RuntimeError, match="already running"):
            control_server.start()


# =============================================================================
# Enabled tests
# =============================================================================


class TestControlServerEnabled:
    def test_stop_sets_enabled_false(self, control_server: ControlServer) -> None:
        _send_msg(control_server, MSG_STOP, 42)
        assert _wait_msg(control_server, pid=42, expected=False)
        assert control_server.is_enabled(42) is False

    def test_start_sets_enabled_true(self, control_server: ControlServer) -> None:
        _send_msg(control_server, MSG_STOP, 42)
        assert _wait_msg(control_server, 42, False)

        _send_msg(control_server, MSG_START, 42)

        assert _wait_msg(control_server, 42, True)
        assert control_server.is_enabled(42) is True

    def test_a_stop_leaves_the_other_pids_enabled(self, control_server: ControlServer) -> None:
        _send_msg(control_server, MSG_STOP, 1)
        _send_msg(control_server, MSG_STOP, 2)

        assert _wait_msg(control_server, 1, False)
        assert _wait_msg(control_server, 2, False)
        assert control_server.is_enabled(3) is True

    def test_a_start_leaves_the_other_pids_stopped(self, control_server: ControlServer) -> None:
        _send_msg(control_server, MSG_STOP, 1)
        _send_msg(control_server, MSG_STOP, 2)
        assert _wait_msg(control_server, 1, False)
        assert _wait_msg(control_server, 2, False)

        _send_msg(control_server, MSG_START, 1)

        assert _wait_msg(control_server, 1, True)
        assert control_server.is_enabled(2) is False

    def test_start_after_stop_removes_pid(self, control_server: ControlServer) -> None:
        _send_msg(control_server, MSG_STOP, 42)
        assert _wait_msg(control_server, 42, False)
        _send_msg(control_server, MSG_START, 42)
        assert _wait_msg(control_server, 42, True)
        assert 42 not in control_server._enabled


# =============================================================================
# Exporter tests
# =============================================================================


class TestControlServerExporter:
    def test_exporter_receives_instant_events(self, mock_exporter: MagicMock) -> None:
        from tests.helpers import MockExporter

        exporter = MockExporter()
        server = ControlServer(exporter, monitored(42))
        server.start()
        try:
            _send_msg(server, MSG_STOP, 42)
            assert _wait_msg(server, 42, False)

            assert len(exporter.instant_events) >= 1
            pid, msg = exporter.instant_events[0]
            assert pid == 42
            assert msg.name == STOP_EVENT
            assert msg.type == "i"
        finally:
            server.close()

    def test_instant_keeps_the_timestamp_the_client_captured(self, mock_exporter: MagicMock) -> None:
        from gcmon.control.control_client import ControlClient
        from tests.helpers import MockExporter

        exporter = MockExporter()
        server = ControlServer(exporter, monitored(os.getpid()))
        server.start()
        try:
            captured = time.monotonic_ns() - 5_000_000_000
            with ControlClient(server.address) as client:
                client.instant_msg("gcmon:bm_x:1:begin", ts=captured)
                deadline = time.monotonic() + 5
                while not exporter.instant_events and time.monotonic() < deadline:
                    time.sleep(0.01)

            assert len(exporter.instant_events) == 1
            _, msg = exporter.instant_events[0]
            assert msg.name == "gcmon:bm_x:1:begin"
            assert msg.ts == captured
        finally:
            server.close()

    def test_exporter_receives_multiple_events(self, mock_exporter: MagicMock) -> None:
        from tests.helpers import MockExporter

        exporter = MockExporter()
        server = ControlServer(exporter, monitored(1))
        server.start()
        try:
            _send_msg(server, MSG_STOP, 1)
            assert _wait_msg(server, 1, False)
            _send_msg(server, MSG_START, 1)
            assert _wait_msg(server, 1, True)

            assert len(exporter.instant_events) == 2
            assert exporter.instant_events[0][1].name == STOP_EVENT
            assert exporter.instant_events[1][1].name == START_EVENT
        finally:
            server.close()


# =============================================================================
# Internal method tests
# =============================================================================


class TestControlServerInternal:
    def test_add_event_with_exporter(self, server_not_started: ControlServer, mock_exporter: MagicMock) -> None:
        server_not_started._add_event("test event", 42, 12345)
        mock_exporter.add_instant_event.assert_called_once()
        args = mock_exporter.add_instant_event.call_args[0]
        assert args[0] == proc(42)
        assert args[1].name == "test event"
        assert args[1].type == "i"
        assert args[1].ts == 12345

    def test_remove_connections_closes_and_removes(
        self, server_not_started: ControlServer, mock_conn: MagicMock
    ) -> None:
        server_not_started._connections.add(mock_conn)
        server_not_started._remove_connections([mock_conn])
        assert mock_conn not in server_not_started._connections
        mock_conn.close.assert_called_once()

    def test_remove_connections_multiple(self, server_not_started: ControlServer) -> None:
        c1: MagicMock = MagicMock()
        c2: MagicMock = MagicMock()
        server_not_started._connections.update([c1, c2])
        server_not_started._remove_connections([c1, c2])
        assert server_not_started._connections == set()
        c1.close.assert_called_once()
        c2.close.assert_called_once()

    def test_remove_nonexistent_connection(self, server_not_started: ControlServer, mock_conn: MagicMock) -> None:
        server_not_started._remove_connections([mock_conn])
        mock_conn.close.assert_called_once()

    def test_clear_connections_removes_all(self, server_not_started: ControlServer) -> None:
        c1: MagicMock = MagicMock()
        c2: MagicMock = MagicMock()
        server_not_started._connections.update([c1, c2])
        server_not_started._clear_connections()
        assert server_not_started._connections == set()
        c1.close.assert_called_once()
        c2.close.assert_called_once()

    def test_clear_connections_empty(self, server_not_started: ControlServer) -> None:
        server_not_started._clear_connections()
        assert server_not_started._connections == set()

    def test_close_connections(self, server_not_started: ControlServer) -> None:
        c1: MagicMock = MagicMock()
        c2: MagicMock = MagicMock()
        server_not_started._close_connections([c1, c2])
        c1.close.assert_called_once()
        c2.close.assert_called_once()

    def test_close_connections_suppresses_exception(self, server_not_started: ControlServer) -> None:
        bad_conn: MagicMock = MagicMock()
        bad_conn.close.side_effect = OSError("connection broken")
        server_not_started._close_connections([bad_conn])
        bad_conn.close.assert_called_once()

    def test_recv_returns_control_msg(self, server_not_started: ControlServer, mock_conn: MagicMock) -> None:
        mock_conn.recv.return_value = {MSG: MSG_START, PID: 42, TS: 12345}
        to_remove: list[Any] = []
        result = server_not_started._recv(mock_conn, to_remove)
        assert result is not None
        assert result.msg == MSG_START
        assert result.pid == 42
        assert result.ts == 12345
        assert to_remove == []

    def test_recv_eof_removes_conn(self, server_not_started: ControlServer, mock_conn: MagicMock) -> None:
        mock_conn.recv.side_effect = EOFError()
        to_remove: list[Any] = []
        result = server_not_started._recv(mock_conn, to_remove)
        assert result is None
        assert mock_conn in to_remove

    def test_recv_oserror_removes_conn(self, server_not_started: ControlServer, mock_conn: MagicMock) -> None:
        mock_conn.recv.side_effect = OSError("pipe broken")
        to_remove: list[Any] = []
        result = server_not_started._recv(mock_conn, to_remove)
        assert result is None
        assert mock_conn in to_remove

    def test_recv_connection_error_removes_conn(self, server_not_started: ControlServer, mock_conn: MagicMock) -> None:
        mock_conn.recv.side_effect = ConnectionError("connection reset")
        to_remove: list[Any] = []
        result = server_not_started._recv(mock_conn, to_remove)
        assert result is None
        assert mock_conn in to_remove

    def test_recv_generic_exception_removes_conn(self, server_not_started: ControlServer, mock_conn: MagicMock) -> None:
        mock_conn.recv.side_effect = ValueError("bad data")
        to_remove: list[Any] = []
        result = server_not_started._recv(mock_conn, to_remove)
        assert result is None
        assert mock_conn in to_remove

    def test_recv_existing_to_remove_appended(self, server_not_started: ControlServer, mock_conn: MagicMock) -> None:
        mock_conn.recv.side_effect = EOFError()
        other: MagicMock = MagicMock()
        to_remove: list[Any] = [other]
        result = server_not_started._recv(mock_conn, to_remove)
        assert result is None
        assert len(to_remove) == 2
        assert other in to_remove
        assert mock_conn in to_remove

    def test_safe_wait_returns_ready(
        self, server_not_started: ControlServer, mock_conn: MagicMock, mock_wait: MagicMock
    ) -> None:
        mock_wait.return_value = [mock_conn]
        result = server_not_started._safe_wait([mock_conn])
        assert result == [mock_conn]

    def test_safe_wait_exception_polls_conns(self, server_not_started: ControlServer, mock_wait: MagicMock) -> None:
        c1: MagicMock = MagicMock()
        c2: MagicMock = MagicMock()
        c1.poll.return_value = True
        c2.poll.return_value = True
        mock_wait.side_effect = Exception("wait failed")
        result = server_not_started._safe_wait([c1, c2])
        assert result == []
        c1.poll.assert_called_once_with(timeout=0)
        c2.poll.assert_called_once_with(timeout=0)

    def test_safe_wait_exception_removes_broken(self, server_not_started: ControlServer, mock_wait: MagicMock) -> None:
        c1: MagicMock = MagicMock()
        c2: MagicMock = MagicMock()
        c1.poll.side_effect = OSError("broken")
        c2.poll.return_value = True
        server_not_started._connections.update([c1, c2])

        mock_wait.side_effect = Exception("wait failed")
        result = server_not_started._safe_wait([c1, c2])

        assert result == []
        assert c1 not in server_not_started._connections
        assert c2 in server_not_started._connections
        c1.close.assert_called_once()

    def test_safe_wait_exception_all_bad(self, server_not_started: ControlServer, mock_wait: MagicMock) -> None:
        c1: MagicMock = MagicMock()
        c2: MagicMock = MagicMock()
        c1.poll.side_effect = OSError("broken")
        c2.poll.side_effect = ConnectionError("reset")
        server_not_started._connections.update([c1, c2])

        mock_wait.side_effect = Exception("wait failed")
        result = server_not_started._safe_wait([c1, c2])

        assert result == []
        assert server_not_started._connections == set()
        c1.close.assert_called_once()
        c2.close.assert_called_once()


# =============================================================================
# Accept loop tests
# =============================================================================


class TestControlServerAcceptLoop:
    def test_accept_loop_stops_on_stop_event(self, server_not_started: ControlServer) -> None:
        server_not_started._stop_event.set()
        server_not_started._accept_loop()
        assert server_not_started._connections == set()

    def test_accept_loop_stops_on_listener_none(self, server_not_started: ControlServer) -> None:
        server_not_started._listener = None
        server_not_started._accept_loop()
        assert server_not_started._connections == set()

    def test_accept_loop_accept_exception_breaks(
        self, server_not_started: ControlServer, caplog: pytest.LogCaptureFixture
    ) -> None:
        assert server_not_started._listener is not None
        listener_address = server_not_started._listener.address
        with patch("gcmon.control.control_server._accept", side_effect=OSError("accept failed")):
            server_not_started._accept_loop()
        assert len(server_not_started._connections) == 0
        assert "Error accepting connection on control server" in caplog.text
        assert f"address={listener_address!r}" in caplog.text
        assert "accept failed" in caplog.text

    def test_a_failed_accept_keeps_the_connection_made_before_it(
        self, server_not_started: ControlServer, mock_conn: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_listener = MagicMock()
        mock_listener.address = "/tmp/gcmon-test"
        server_not_started._listener = mock_listener
        accepts = [mock_conn, OSError("second accept fails")]

        with patch("gcmon.control.control_server._accept", side_effect=accepts):
            server_not_started._accept_loop()

        assert not mock_conn.close.called
        assert mock_conn in server_not_started._connections
        assert "Error accepting connection on control server" in caplog.text
        assert "address='/tmp/gcmon-test'" in caplog.text
        assert "second accept fails" in caplog.text


# =============================================================================
# Reader loop tests
# =============================================================================


class TestControlServerReaderLoop:
    def test_reader_loop_stops_on_stop_event(self, server_not_started: ControlServer) -> None:
        server_not_started._stop_event.set()
        server_not_started._reader_loop()
        assert server_not_started._connections == set()

    def test_reader_loop_processes_start_msg(
        self,
        server_not_started: ControlServer,
        mock_conn: MagicMock,
        mock_wait_and_stop: MagicMock,
    ) -> None:
        mock_conn.recv.return_value = {MSG: MSG_START, PID: 42, TS: 12345}
        server_not_started._connections.add(mock_conn)
        server_not_started._enabled[42] = False

        mock_wait_and_stop.return_value = [mock_conn]
        server_not_started._reader_loop()

        assert 42 not in server_not_started._enabled

    def test_reader_loop_processes_stop_msg(
        self,
        server_not_started: ControlServer,
        mock_conn: MagicMock,
        mock_wait_and_stop: MagicMock,
    ) -> None:
        mock_conn.recv.return_value = {MSG: MSG_STOP, PID: 42, TS: 12345}
        server_not_started._connections.add(mock_conn)

        mock_wait_and_stop.return_value = [mock_conn]
        server_not_started._reader_loop()

        assert server_not_started._enabled.get(42) is False

    def test_reader_loop_removes_bad_connection(
        self,
        server_not_started: ControlServer,
        mock_conn: MagicMock,
        mock_wait_and_stop: MagicMock,
    ) -> None:
        mock_conn.recv.side_effect = EOFError()
        server_not_started._connections.add(mock_conn)

        mock_wait_and_stop.return_value = [mock_conn]
        server_not_started._reader_loop()

        assert mock_conn not in server_not_started._connections
        mock_conn.close.assert_called_once()

    def test_reader_loop_handles_malformed_msg(
        self,
        server_not_started: ControlServer,
        mock_conn: MagicMock,
        mock_wait_and_stop: MagicMock,
    ) -> None:
        mock_conn.recv.return_value = {"bad": "data", TS: 12345}
        server_not_started._connections.add(mock_conn)

        mock_wait_and_stop.return_value = [mock_conn]
        server_not_started._reader_loop()

        assert mock_conn not in server_not_started._connections
        mock_conn.close.assert_called_once()

    def test_reader_loop_no_connections(self, server_not_started: ControlServer, mock_wait_and_stop: MagicMock) -> None:
        mock_wait_and_stop.return_value = list[Connection]()
        server_not_started._reader_loop()
        assert server_not_started._connections == set()

    def test_reader_loop_drains_pending_messages(
        self, server_not_started: ControlServer, mock_wait_and_stop: MagicMock
    ) -> None:
        mock_conn = MagicMock()
        mock_conn.recv.return_value = {MSG: MSG_STOP, PID: 42, TS: 12345}
        mock_conn.poll.side_effect = [True, False]
        server_not_started._connections.add(mock_conn)

        mock_wait_and_stop.return_value = list[Connection]()
        server_not_started._reader_loop()

        assert server_not_started._enabled.get(42) is False


# =============================================================================
# Drain connections tests
# =============================================================================


class TestDrainConnections:
    def test_drain_no_connections(self, server_not_started: ControlServer) -> None:
        server_not_started._drain_connections()
        assert server_not_started._connections == set()

    def test_drain_no_data_exits_immediately(self, server_not_started: ControlServer, mock_conn: MagicMock) -> None:
        server_not_started._connections.add(mock_conn)
        server_not_started._drain_connections()
        mock_conn.close.assert_not_called()

    def test_drain_poll_exception_removes_conn(self, server_not_started: ControlServer) -> None:
        mock_conn: MagicMock = MagicMock()
        mock_conn.poll.side_effect = OSError("pipe broken")
        mock_conn.recv.return_value = {MSG: MSG_STOP, PID: 1, TS: 12345}
        server_not_started._connections.add(mock_conn)

        server_not_started._drain_connections()

        assert mock_conn not in server_not_started._connections
        mock_conn.close.assert_called_once()

    def test_drain_recv_eof_removes_conn(self, server_not_started: ControlServer) -> None:
        mock_conn: MagicMock = MagicMock()
        mock_conn.poll.return_value = True
        mock_conn.recv.side_effect = EOFError()
        server_not_started._connections.add(mock_conn)

        server_not_started._drain_connections()

        assert mock_conn not in server_not_started._connections
        mock_conn.close.assert_called_once()

    def test_drain_handle_msg_error_is_nonfatal(
        self, server_not_started: ControlServer, mock_exporter: MagicMock
    ) -> None:
        mock_exporter.add_instant_event.side_effect = ValueError("exporter failure")
        mock_conn: MagicMock = MagicMock()
        mock_conn.poll.side_effect = [True, False]
        mock_conn.recv.return_value = {MSG: MSG_STOP, PID: 42, TS: 12345}
        server_not_started._connections.add(mock_conn)

        server_not_started._drain_connections()

        assert mock_conn in server_not_started._connections
        mock_conn.close.assert_not_called()

    def test_drain_processes_messages_round_robin(self, server_not_started: ControlServer) -> None:
        c1: MagicMock = MagicMock()
        c1.poll.side_effect = [True, False]
        c1.recv.return_value = {MSG: MSG_STOP, PID: 1, TS: 12345}
        c2: MagicMock = MagicMock()
        c2.poll.side_effect = [True, False]
        c2.recv.return_value = {MSG: MSG_STOP, PID: 2, TS: 12346}
        server_not_started._connections.update([c1, c2])

        server_not_started._drain_connections()

        assert server_not_started._enabled.get(1) is False
        assert server_not_started._enabled.get(2) is False

    def test_drain_timeout_expiry(self, server_not_started: ControlServer) -> None:
        """A connection that has data on every round. The clock steps 0.02 s a
        reading, so a 0.05 s drain is two rounds; the ten messages run out
        before a drain that ignores its deadline can hang the suite."""
        mock_conn: MagicMock = MagicMock()
        mock_conn.poll.return_value = True
        mock_conn.recv.side_effect = [{MSG: MSG_STOP, PID: 999, TS: 12345}] * 10
        server_not_started._connections.add(mock_conn)

        with patch("gcmon.control.control_server.time.monotonic", side_effect=count(0.0, 0.02)):
            server_not_started._drain_connections(timeout=0.05)

        assert mock_conn.recv.call_count == 2
        assert server_not_started._enabled.get(999) is False


# =============================================================================
# Close tests
# =============================================================================


class TestControlServerClose:
    def _make_server(self) -> ControlServer:
        return ControlServer(MagicMock(), monitored(42))

    def test_close_stops_accepting(self) -> None:
        server: ControlServer = self._make_server()
        server.start()
        server.close()
        assert not server.is_running()

    def test_close_clears_enabled(self) -> None:
        server: ControlServer = self._make_server()
        server.start()
        _send_msg(server, MSG_STOP, 42)
        assert _wait_msg(server, 42, False)

        server.close()

        assert server.is_enabled(42) is True

    def test_close_is_idempotent(self) -> None:
        server: ControlServer = self._make_server()
        server.start()
        server.close()
        server.close()

    def test_close_not_started(self) -> None:
        server: ControlServer = self._make_server()
        server.close()
        assert not server.is_running()

    def test_close_closes_listener(self) -> None:
        server: ControlServer = self._make_server()
        server.start()
        server.close()
        assert server._listener is None

    def test_close_clears_connections(self) -> None:
        server: ControlServer = self._make_server()
        server.start()
        server.close()
        assert len(server._connections) == 0

    def test_close_twice_not_started(self) -> None:
        server: ControlServer = self._make_server()
        server.close()
        server.close()

    def test_address_raises_after_listener_cleared(
        self,
        server_not_started: ControlServer,
    ) -> None:
        assert server_not_started._listener is not None
        server_not_started._listener = None
        with pytest.raises(RuntimeError, match="closed or not initialized"):
            _ = server_not_started.address


# =============================================================================
# set_control_env tests
# =============================================================================


class TestSetControlEnv:
    def test_sets_address(self) -> None:
        env: dict[str, str] = {}
        set_control_env(env, "/tmp/gcmon-test")
        assert env[CONTROL_ADDRESS_ENV] == "/tmp/gcmon-test"


# =============================================================================
# Platform-specific tests
# =============================================================================


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific test")
class TestPlatformWindows:
    def test_make_address_windows(self) -> None:
        from gcmon.control.control_server import _make_address

        result = _make_address("test-name")
        assert result == r"\\.\pipe\gcmon-test-name"

    def test_tconnection_is_pipe_connection(self) -> None:
        if sys.platform == "win32":  # fix mypy errors
            from multiprocessing.connection import PipeConnection

            from gcmon.control.control_server import TConnection

            assert TConnection is PipeConnection


@pytest.mark.skipif(sys.platform == "win32", reason="Unix-specific test")
class TestPlatformUnix:
    def test_make_address_unix(self) -> None:
        from gcmon.control.control_server import _make_address

        result = _make_address("test-name")
        assert result == "/tmp/gcmon-test-name"

    def test_tconnection_is_connection(self) -> None:
        from multiprocessing.connection import Connection

        from gcmon.control.control_server import TConnection

        if sys.platform != "win32":
            assert TConnection is Connection


# =============================================================================
# Thread safety tests
# =============================================================================


class TestControlServerThreadSafety:
    def test_concurrent_is_enabled(self, control_server: ControlServer) -> None:
        errors: list[Exception] = []
        barrier = threading.Barrier(10)

        def access_enabled() -> None:
            try:
                barrier.wait(timeout=5)
                for _ in range(50):
                    control_server.is_enabled(42)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=access_enabled, daemon=True) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert errors == []
        assert not [t for t in threads if t.is_alive()]

    def test_concurrent_add_event(self, server_not_started: ControlServer, mock_exporter: MagicMock) -> None:
        """The registry holds pid 42, so every message reaches the exporter."""
        errors: list[Exception] = []
        barrier = threading.Barrier(2)

        def add_event_loop() -> None:
            try:
                barrier.wait(timeout=5)
                for _ in range(20):
                    server_not_started._add_event("test", 42, 0)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=add_event_loop, daemon=True) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert errors == []
        assert not [t for t in threads if t.is_alive()]
        assert len(mock_exporter.add_instant_event.call_args_list) == 40
