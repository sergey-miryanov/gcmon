"""Shared fixtures for the monitoring package's tests."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def mock_monitor() -> MagicMock:
    """Pre-configured MagicMock standing in for a constructed EventsMonitor."""
    return MagicMock()
