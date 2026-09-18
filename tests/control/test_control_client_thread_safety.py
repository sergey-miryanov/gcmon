"""Thread-safety stress tests for ControlClient.

These tests exercise concurrent send() / close() paths on a single
ControlClient connection.
"""

from __future__ import annotations

import os
import threading
import time

import pytest

from gcmon.control.control_client import ControlClient
from gcmon.control.control_server import ControlServer
from tests.helpers import MockExporter, monitored

N_SENDERS = 4
N_PER_SENDER = 10
N_THREADS = 2
LOOP_COUNT = 1000
BARIER_TIMEOUT = 2


def _run_threads(threads: list[threading.Thread], timeout: float = 5.0) -> None:
    """Start all threads, join all with bounded timeout, report survivors."""
    for t in threads:
        t.start()
    deadline = time.monotonic() + timeout
    for t in threads:
        remaining = max(0.0, deadline - time.monotonic())
        t.join(timeout=remaining)
    survivors = [t.name for t in threads if t.is_alive()]
    assert not survivors, f"threads hung: {survivors}"


def _wait_for_event_count(exporter: MockExporter, count: int, timeout: float = 2.0) -> None:
    """Wait until the exporter has received at least `count` events."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(exporter.instant_events) >= count:
            return
        time.sleep(0.01)


def _make_server_with_exporter() -> tuple[ControlServer, MockExporter]:
    """Spin up a real ControlServer with a real MockExporter (collects events)."""
    exporter = MockExporter()
    server = ControlServer(exporter, monitored(os.getpid()))
    server.start()
    return server, exporter


def _assert_no_messages_loss(
    exporter: MockExporter,
    expected_total: int,
    *,
    timeout: float = 15.0,
) -> None:
    _wait_for_event_count(exporter, expected_total, timeout=timeout)
    assert len(exporter.instant_events) == expected_total


def _assert_no_messages_garbling(exporter: MockExporter) -> None:
    """Assert every sent name appears exactly once in the received set."""
    expected_names = {f"t{i}-m{n}" for i in range(N_SENDERS) for n in range(N_PER_SENDER)}
    actual_names = {msg.name for _, msg in exporter.instant_events}
    missing = expected_names - actual_names
    extra = actual_names - expected_names
    assert not (missing or extra), f"missing={missing}, extra={extra}"


def _assert_send_order_preserved(exporter: MockExporter) -> None:
    """Assert that each sender's messages arrived in send order."""
    for i in range(N_SENDERS):
        sender_events = [msg.name for _, msg in exporter.instant_events if msg.name.startswith(f"t{i}-")]
        expected = [f"t{i}-m{n}" for n in range(N_PER_SENDER)]
        assert sender_events == expected, (
            f"sender {i}: order broken. got {sender_events[:5]}... expected {expected[:5]}..."
        )


def _assert_no_thread_errors(errors: list[BaseException]) -> None:
    """Assert no thread escaped an uncaught exception."""
    assert errors == [], f"thread raised: {errors}"


def _sender_sends_n(
    i: int,
    client: ControlClient,
    barrier: threading.Barrier,
    errors: list[BaseException],
) -> None:
    """Worker: send N_PER_SENDER messages on `client`, tagged with index `i`."""
    try:
        barrier.wait(timeout=BARIER_TIMEOUT)
        for n in range(N_PER_SENDER):
            client.instant_msg(f"t{i}-m{n}")
    except BaseException as e:
        errors.append(e)


def _run_sender_threads(
    senders: list[tuple[int, ControlClient]],
    barrier: threading.Barrier,
    *,
    timeout: float = 10.0,
) -> list[BaseException]:
    """Launch one thread per (index, client) pair; return the errors list."""
    errors: list[BaseException] = []
    threads = [threading.Thread(target=_sender_sends_n, args=(i, client, barrier, errors)) for i, client in senders]
    _run_threads(threads, timeout=timeout)
    return errors


def _send_and_close_n(
    client: ControlClient,
    tag_prefix: str,
    barrier: threading.Barrier,
    loop_count: int,
    errs: list[BaseException],
) -> None:
    """One thread's real-work loop: `loop_count` iterations of (client.instant_msg + client.close)."""
    try:
        barrier.wait(timeout=BARIER_TIMEOUT)
        for i in range(loop_count):
            client.instant_msg(f"{tag_prefix}-{i}")
            client.close()
    except BaseException as e:
        errs.append(e)


def _run_send_and_close_threads(
    client: ControlClient,
    n_threads: int,
    loop_count: int,
) -> list[BaseException]:
    """N threads run a real-work loop of (send + close) against `client`."""
    barrier = threading.Barrier(n_threads)
    errors: list[BaseException] = []

    threads = [
        threading.Thread(
            target=_send_and_close_n,
            name=f"thread-{i}",
            args=(client, f"thread-{i}", barrier, loop_count, errors),
        )
        for i in range(n_threads)
    ]
    _run_threads(threads, timeout=60)
    return errors


class TestConcurrentSend:
    @pytest.mark.stress
    def test_concurrent_send_does_not_lose_messages(self) -> None:
        server, exporter = _make_server_with_exporter()
        client = ControlClient(server.address)
        try:
            senders = [(i, client) for i in range(N_SENDERS)]

            errors = _run_sender_threads(senders, threading.Barrier(N_SENDERS))

            _assert_no_thread_errors(errors)
            _assert_no_messages_loss(exporter, N_SENDERS * N_PER_SENDER)
            _assert_no_messages_garbling(exporter)
        finally:
            client.close()
            server.close()

    @pytest.mark.stress
    def test_per_sender_fifo_preserved(self) -> None:
        server, exporter = _make_server_with_exporter()
        clients = [ControlClient(server.address) for _ in range(N_SENDERS)]
        try:
            senders = [(i, clients[i]) for i in range(N_SENDERS)]

            errors = _run_sender_threads(senders, threading.Barrier(N_SENDERS))

            _assert_no_thread_errors(errors)
            _assert_no_messages_loss(exporter, N_SENDERS * N_PER_SENDER)
            _assert_send_order_preserved(exporter)
        finally:
            for c in clients:
                c.close()
            server.close()


class TestSendCloseRace:
    @pytest.mark.stress
    def test_concurrent_real_work_loops_exercise_contention(self) -> None:
        server, _exporter = _make_server_with_exporter()
        client = ControlClient(server.address)
        try:
            errors = _run_send_and_close_threads(client, N_THREADS, LOOP_COUNT)

            _assert_no_thread_errors(errors)
        finally:
            client.close()
            server.close()
