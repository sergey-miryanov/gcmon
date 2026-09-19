"""A lossy run, and the loss row a trace processor would build from it.

Helpers, not tests. `ingest` polls a real `EventsMonitor` on a fixed clock and
returns what it exported; `loss_slices` reads the row off the shared
converter's output.

Reading it is the instrument the assertions rest on, because nothing downstream
rejects a row whose spans overlap: it **still parses and still renders**, and
ADR-0015 records that the trace processor reports ``misplaced_end_event = 0``
and reads a crossing span as nested. Something has to go looking for the shape.
"""

from collections.abc import Iterator, Sequence
from itertools import pairwise
from typing import override
from unittest.mock import patch

from gcmon.exporters.exporter import EventsExporter
from gcmon.exporters.trace_converter import convert_to_trace_format
from gcmon.model.data import GCStatsInfo
from gcmon.model.poll_status import PollStatus
from gcmon.model.process import Process
from gcmon.model.protocol import TGCStatsInfo, TInstantMsg, TItem, TLossMsg
from gcmon.model.trace_event import LossTrack, Slice
from gcmon.monitoring.monitor import EventsMonitor
from gcmon.monitoring.target_process import ExternalProcess
from gcmon.monitoring.wait_policy import no_wait_policy
from gcmon.stats.streaming_stats import StreamingStats
from tests.helpers import FakeEventsReader, create_mock_stats_item

PID = 12345
IID = 0

# What the monitor's clock reads at each poll. A span's edges are two of
# these, so pinning them is what lets a test name a timestamp at all.
POLL_TIMES = [1_000_000, 10_000_000, 20_000_000, 30_000_000]

SliceRow = tuple[str, int, int, int]
"""``(name, ts_start, ts_stop, depth)``.

Named apart from the `Slice` the converter emits, which this is resolved from
and `_loss_row` in `test_combine_loss_round_trip` is not.
"""


class Recorder(EventsExporter):
    """Every record the monitor exported, in the order it exported them."""

    def __init__(self) -> None:
        super().__init__()
        self.items: list[TItem] = []

    @override
    def add_event(self, process: Process, item: TGCStatsInfo) -> None:
        self.items.append(item)

    @override
    def add_loss_event(self, process: Process, item: TLossMsg) -> None:
        self.items.append(item)

    @override
    def add_instant_event(self, process: Process, item: TInstantMsg) -> None:
        self.items.append(item)

    @override
    def close(self) -> None:
        pass


def ingest(*batches: Sequence[GCStatsInfo]) -> list[TItem]:
    """Poll a monitor once per batch and hand back what it exported.

    Through ``poll`` rather than ``_ingest``, so the poll instants come from
    where they come from in production. The clock is fixed to ``POLL_TIMES``:
    every loss window's width is the distance between two reads, which a real
    monotonic clock would make unrepeatable.
    """
    recorder = Recorder()
    reads = iter(batches)

    def one_read(pid: int) -> list[GCStatsInfo]:
        return list(next(reads))

    monitor = EventsMonitor(
        ExternalProcess(pid=PID),
        recorder,
        StreamingStats(),
        reader=FakeEventsReader(one_read),
        wait_policy_factory=no_wait_policy,
    )

    def clock() -> Iterator[int]:
        # Two calls per poll: `poll` brackets the read to time it.
        for instant in POLL_TIMES:
            yield instant
            yield instant + 1_000

    ticks = clock()

    with patch("gcmon.monitoring.monitor.time.monotonic_ns", side_effect=lambda: next(ticks)):
        for _ in batches:
            assert monitor._poll(PID).status is PollStatus.OK

    return recorder.items


def loss_slices(items: Sequence[TItem]) -> dict[LossTrack, list[SliceRow]]:
    """Every loss row's slices, in the order the row draws them.

    Fails if a span opens while another is still open. That used to take a
    stack walk, because an END carried no name and pairing it with a BEGIN
    was the only way to know how wide a span was; a `Slice` carries its own
    width, so the overlap is visible in the data. Same defect, and it is
    still the one being designed out: the row claims a sequence of
    intervals, and an interval inside another one is the reading ADR-0015
    rules out.

    The walk is over the converter's output **in the order it emitted it**,
    not over a sorted copy, so it also fails on a row emitted out of order.
    The encoder expands a `Slice` in list order, so that is a defect too --
    it is the one `_loss_in_time_order` exists to prevent, and sorting here
    first would hide it.

    The depth is 0 for that reason -- a row that got past the assert has
    nothing nested on it. It stays in the tuple so this lines up with
    `_loss_row`, which reads a real trace processor and can report a 1.
    """
    events = convert_to_trace_format({PID: items})
    rows: dict[LossTrack, list[Slice]] = {}
    for event in events:
        if isinstance(event, Slice) and isinstance(event.track, LossTrack):
            rows.setdefault(event.track, []).append(event)

    resolved: dict[LossTrack, list[SliceRow]] = {}
    for row, spans in rows.items():
        for previous, span in pairwise(spans):
            assert span.ts_start >= previous.ts_stop, (
                f"{row}: {span.name!r} at {span.ts_start} opened inside {previous.name!r}"
            )
        resolved[row] = sorted(((s.name, s.ts_start, s.ts_stop, 0) for s in spans), key=lambda s: (s[1], -s[2]))

    return resolved


def three_generations() -> tuple[list[GCStatsInfo], list[GCStatsInfo], list[GCStatsInfo]]:
    """Three polls, every generation losing records across each seam."""

    def batch(nth: int, counters: tuple[int, int, int], starts: tuple[int, int, int]) -> list[GCStatsInfo]:
        return [
            create_mock_stats_item(
                gen=gen,
                iid=IID,
                collections=counters[gen],
                ts_start=starts[gen],
                ts_stop=starts[gen] + 100_000,
                # Cumulative through this record: three collections per seam,
                # 100 us each, so every generation loses 200 us per interval.
                duration=(1 + 3 * nth) * 100e-6,
            )
            for gen in (0, 1, 2)
        ]

    return (
        batch(0, (10, 20, 30), (100_000, 200_000, 300_000)),
        batch(1, (13, 23, 33), (5_000_000, 6_000_000, 7_000_000)),
        batch(2, (16, 26, 36), (12_000_000, 14_000_000, 16_000_000)),
    )
