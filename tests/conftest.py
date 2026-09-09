from __future__ import annotations

import logging
import subprocess
import types
from collections.abc import Callable, Generator
from unittest.mock import Mock

import pytest

from gcmon.model.data import GCStatsInfo, InstantMsg
from gcmon.model.protocol import TGCStatsInfo, TMapping, to_mapping
from gcmon.monitoring.monitor import EventsMonitor
from gcmon.monitoring.target_process import ExternalProcess
from gcmon.monitoring.wait_policy import no_wait_policy
from gcmon.stats.streaming_stats import StreamingStats
from tests.data_helpers import create_instant_msg
from tests.helpers import FakeEventsReader, MockExporter, create_mock_stats_item

DEFAULT_PID: int = 12345


@pytest.fixture(autouse=True)
def _caplog_gcmon(caplog: pytest.LogCaptureFixture) -> Generator[pytest.LogCaptureFixture]:
    """Auto-configure gcmon logger to INFO level for caplog.

    Also snapshots and restores ``logging.getLogger("gcmon")``'s handlers and
    level so production code that attaches handlers to the shared "gcmon"
    logger (e.g. the pyperf hook entry point) does not leak across tests and
    duplicate log records to stderr in subsequent tests.
    """
    logger = logging.getLogger("gcmon")
    original_level = logger.level
    original_handlers = list(logger.handlers)
    try:
        logger.setLevel(logging.INFO)
        yield caplog
    finally:
        for handler in list(logger.handlers):
            if handler not in original_handlers:
                logger.removeHandler(handler)
        for handler in original_handlers:
            if handler not in logger.handlers:
                logger.addHandler(handler)
        logger.setLevel(original_level)


@pytest.fixture
def mock_stats_item() -> TGCStatsInfo:
    """Create a mock StatsItem with default values (gen=0).

    Note: ts_start/ts_stop are in nanoseconds (int), duration is in seconds (float).

    Returns:
        GCStatsItem dict with all required fields.
    """
    return create_mock_stats_item()


@pytest.fixture
def mock_stats_item_batch() -> list[TGCStatsInfo]:
    """Create a batch of mock GCStatsItem instances with incrementing values.

    Returns:
        List of 3 GCStatsItem dicts with different generations.
    """
    items: list[TGCStatsInfo] = []
    for gen in range(3):
        item = create_mock_stats_item(
            gen=gen,
            ts_start=1_000_000_000 + gen * 100_000_000,
            ts_stop=1_005_000_000 + gen * 100_000_000,
            collections=10 * (gen + 1),
            collected=50 * (gen + 1),
            uncollectable=gen,
            candidates=20 * (gen + 1),
            heap_size=1_000_000 * (gen + 1),
            duration=0.001 * (gen + 1),
        )
        items.append(item)
    return items


@pytest.fixture
def mock_logger() -> Mock:
    """Create a mock logger instance.

    Returns:
        Mock logging.Logger instance.
    """
    return Mock(spec=logging.Logger)


@pytest.fixture
def mock_process() -> Mock:
    """Create a mock subprocess.Popen instance.

    Returns:
        Mock subprocess.Popen instance with common attributes.
    """
    process = Mock(spec=subprocess.Popen)
    process.pid = 12345
    process.returncode = 0
    process.communicate.return_value = (b"stdout data", b"stderr data")
    return process


@pytest.fixture
def exporter() -> MockExporter:
    return MockExporter()


@pytest.fixture
def process() -> ExternalProcess:
    return ExternalProcess(pid=12345)


@pytest.fixture
def stats() -> StreamingStats:
    return StreamingStats()


@pytest.fixture
def reader() -> FakeEventsReader:
    """The injected reader every monitor in the suite is built with.

    Never a real one: a monitor built without a reader would attach to whatever
    process happens to hold the small integers these tests use as pids.
    """
    return FakeEventsReader()


@pytest.fixture
def monitor(
    exporter: MockExporter, process: ExternalProcess, stats: StreamingStats, reader: FakeEventsReader
) -> EventsMonitor:
    return EventsMonitor(process, exporter, stats, reader=reader, wait_policy_factory=no_wait_policy)


@pytest.fixture
def make_monitor(
    exporter: MockExporter, stats: StreamingStats, reader: FakeEventsReader
) -> Callable[..., EventsMonitor]:
    def _make(pid: int = 12345, exp: MockExporter | None = None) -> EventsMonitor:
        proc = ExternalProcess(pid=pid)
        return EventsMonitor(proc, exp or exporter, stats, reader=reader, wait_policy_factory=no_wait_policy)

    return _make


@pytest.fixture
def env_module() -> types.ModuleType:
    """Provide the _env module for testing."""
    from gcmon.cli.monitor import _env

    return _env


@pytest.fixture
def simple_item() -> GCStatsInfo:
    return GCStatsInfo(
        gen=0,
        iid=1,
        ts_start=1_000_000,
        ts_stop=2_000_000,
        heap_size=1024,
        collections=5,
        collected=50,
        uncollectable=0,
        candidates=10,
        duration=0.005,
    )


@pytest.fixture
def incremental_item() -> GCStatsInfo:
    return GCStatsInfo(
        gen=1,
        iid=2,
        ts_start=3_000_000,
        ts_stop=4_000_000,
        heap_size=2048,
        collections=10,
        collected=100,
        uncollectable=1,
        candidates=20,
        duration=0.01,
        increment_size=500,
        alive_size=300,
        ts_mark_alive_start=3_000_500,
        ts_mark_alive_stop=3_001_000,
        ts_fill_increment_start=3_001_500,
        ts_fill_increment_stop=3_002_000,
        ts_deduce_unreachable_start=3_002_500,
        ts_deduce_unreachable_stop=3_003_000,
        ts_handle_weakref_callbacks_start=3_003_000,
        ts_handle_weakref_callbacks_stop=3_004_000,
        ts_finalize_garbage_stop=3_005_000,
        finalized_garbage_count=42,
        ts_handle_resurrected_stop=3_006_000,
        ts_clear_weakrefs_stop=3_007_000,
        clear_weakrefs_count=7,
        ts_delete_garbage_start=3_008_000,
        ts_delete_garbage_stop=3_009_000,
        deleted_garbage_count=13,
    )


@pytest.fixture
def instant_item() -> InstantMsg:
    return create_instant_msg()


@pytest.fixture
def gc_stats_dict(simple_item: GCStatsInfo) -> TMapping:
    return to_mapping(simple_item)


@pytest.fixture
def incremental_dict(incremental_item: GCStatsInfo) -> TMapping:
    return to_mapping(incremental_item)


@pytest.fixture
def instant_dict(instant_item: InstantMsg) -> TMapping:
    return to_mapping(instant_item)
