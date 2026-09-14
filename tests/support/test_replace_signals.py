"""Tests for gcmon.support.replace_signals."""

from __future__ import annotations

import signal
from collections.abc import Generator

import pytest

from gcmon.support.replace_signals import replace_signals


@pytest.fixture
def installed_handlers() -> Generator[tuple[object, object]]:
    """Capture and restore signal handlers around a test."""
    original_int = signal.getsignal(signal.SIGINT)
    original_term = signal.getsignal(signal.SIGTERM)
    yield original_int, original_term
    signal.signal(signal.SIGINT, original_int)
    signal.signal(signal.SIGTERM, original_term)


class TestReplaceSignals:
    def test_replaces_sigint_and_sigterm(self, installed_handlers: tuple[signal.Handlers, signal.Handlers]) -> None:
        def handler(signum: int, frame: object) -> None:
            pass

        with replace_signals(handler):
            assert signal.getsignal(signal.SIGINT) is handler
            assert signal.getsignal(signal.SIGTERM) is handler

    def test_restores_handlers_on_normal_exit(
        self, installed_handlers: tuple[signal.Handlers, signal.Handlers]
    ) -> None:
        original_int, original_term = installed_handlers

        def handler(signum: int, frame: object) -> None:
            pass

        with replace_signals(handler):
            pass
        assert signal.getsignal(signal.SIGINT) is original_int
        assert signal.getsignal(signal.SIGTERM) is original_term

    def test_restores_handlers_on_exception(self, installed_handlers: tuple[signal.Handlers, signal.Handlers]) -> None:
        original_int, original_term = installed_handlers

        def handler(signum: int, frame: object) -> None:
            pass

        with pytest.raises(RuntimeError), replace_signals(handler):
            raise RuntimeError("boom")
        assert signal.getsignal(signal.SIGINT) is original_int
        assert signal.getsignal(signal.SIGTERM) is original_term

    def test_handler_receives_signal(self, installed_handlers: tuple[signal.Handlers, signal.Handlers]) -> None:
        received: list[int] = []

        def handler(signum: int, frame: object) -> None:
            received.append(signum)

        with replace_signals(handler):
            signal.raise_signal(signal.SIGINT)
            signal.raise_signal(signal.SIGTERM)
        assert signal.SIGINT in received
        assert signal.SIGTERM in received

    def test_nested_replacement_restores_outer(
        self, installed_handlers: tuple[signal.Handlers, signal.Handlers]
    ) -> None:
        original_int, _ = installed_handlers

        def outer_handler(signum: int, frame: object) -> None:
            pass

        def inner_handler(signum: int, frame: object) -> None:
            pass

        with replace_signals(outer_handler):
            with replace_signals(inner_handler):
                assert signal.getsignal(signal.SIGINT) is inner_handler
            assert signal.getsignal(signal.SIGINT) is outer_handler
        assert signal.getsignal(signal.SIGINT) is original_int
