"""Tests for gcmon.support.set_on_exit."""

import threading

import pytest

from gcmon.support.set_on_exit import set_on_exit


class TestSetOnExit:
    def test_sets_event_on_normal_exit(self) -> None:
        event = threading.Event()
        with set_on_exit(event):
            pass
        assert event.is_set()

    def test_sets_event_on_exception(self) -> None:
        event = threading.Event()
        with pytest.raises(RuntimeError), set_on_exit(event):
            raise RuntimeError("boom")
        assert event.is_set()

    def test_event_not_set_during_body(self) -> None:
        event = threading.Event()
        with set_on_exit(event):
            assert not event.is_set()
