"""Tests for which records a poll treats as new.

A read returns the whole ring buffer every time, in slot order.
The two-poll fixture below is a verbatim capture from a CPython 3.15 target
allocating in a loop, polled twice 100 ms apart. Hand-written batches tend
to come out sorted, which hides the rotation that breaks naive dedup.
"""

from collections.abc import Sequence
from itertools import count

import pytest

from gcmon.model.data import GCStatsInfo
from gcmon.model.poll_status import PollStatus
from gcmon.model.process import Process
from gcmon.model.protocol import TGCStatsInfo
from gcmon.monitoring.monitor import EventsMonitor
from gcmon.stats.streaming_stats import StreamingStats
from tests.helpers import FakeEventsReader, MockExporter, create_mock_stats_item, polled, proc

PID = 12345

# (gen, collections, ts_start, ts_stop), in the slot order the extension
# returned them. Note gen 0 wraps after collections=476, and gen 2 holds two
# slots that were never written.
POLL_0: list[tuple[int, int, int, int]] = [
    (0, 473, 294787151995400, 294787152127300),
    (0, 474, 294787153982200, 294787154114900),
    (0, 475, 294787154366000, 294787154504600),
    (0, 476, 294787154776500, 294787154918900),
    (0, 466, 294787144021400, 294787144152700),
    (0, 467, 294787144418900, 294787144564500),
    (0, 468, 294787146641200, 294787146782800),
    (0, 469, 294787147038600, 294787147170500),
    (0, 470, 294787149158300, 294787149292000),
    (0, 471, 294787149543600, 294787149675000),
    (0, 472, 294787149927900, 294787150058800),
    (1, 42, 294787139971000, 294787140103000),
    (1, 43, 294787152383400, 294787152514700),
    (1, 41, 294787129573800, 294787129708500),
    (2, 0, 0, 0),
    (2, 1, 294786777619400, 294786778177300),
    (2, 0, 0, 0),
]

POLL_1: list[tuple[int, int, int, int]] = [
    (0, 561, 294787252319700, 294787252451800),
    (0, 562, 294787254273900, 294787254404700),
    (0, 563, 294787254660400, 294787254791100),
    (0, 553, 294787244879600, 294787245010000),
    (0, 554, 294787245265700, 294787245399800),
    (0, 555, 294787247335400, 294787247469400),
    (0, 556, 294787247727100, 294787247860100),
    (0, 557, 294787249221500, 294787249363900),
    (0, 558, 294787249616300, 294787249748300),
    (0, 559, 294787250008100, 294787250150200),
    (0, 560, 294787251930300, 294787252063300),
    (1, 51, 294787253889000, 294787254021200),
    (1, 49, 294787228540400, 294787228711600),
    (1, 50, 294787242504100, 294787242638800),
    (2, 0, 0, 0),
    (2, 1, 294786777619400, 294786778177300),
    (2, 0, 0, 0),
]


_POLL_CLOCK = count(1_000_000_000, 100_000_000)


def ingest(monitor: EventsMonitor, pid: int, batch: Sequence[GCStatsInfo]) -> None:
    """One poll, at the next instant on a clock shared by this whole file.

    `_ingest` bounds a loss record by the two polls around it, so it has to be
    told when this one happened. Nothing here looks at loss, so the instants
    only have to increase.
    """
    monitor._ingest(polled(monitor, pid), list(batch), next(_POLL_CLOCK))


def build_batch(slots: Sequence[tuple[int, int, int, int]], iid: int = 0) -> list[GCStatsInfo]:
    return [
        create_mock_stats_item(gen=gen, collections=collections, ts_start=ts_start, ts_stop=ts_stop, iid=iid)
        for gen, collections, ts_start, ts_stop in slots
    ]


def seen(exporter: MockExporter) -> set[tuple[int, int]]:
    """The (gen, collections) pairs the exporter was handed."""
    return {(event.gen, event.collections) for event in exporter.events}


@pytest.fixture
def poll_0() -> list[GCStatsInfo]:
    return build_batch(POLL_0)


@pytest.fixture
def poll_1() -> list[GCStatsInfo]:
    return build_batch(POLL_1)


class TestFirstPoll:
    def test_ingests_every_written_slot(
        self, monitor: EventsMonitor, exporter: MockExporter, poll_0: list[GCStatsInfo]
    ) -> None:
        """A cursor advancing while walking the batch would stop at
        collections=476 and discard the seven gen-0 slots behind the wrap."""
        ingest(monitor, PID, poll_0)

        assert seen(exporter) == {(0, c) for c in range(466, 477)} | {(1, 41), (1, 42), (1, 43), (2, 1)}

    def test_older_generations_are_not_shadowed(
        self, monitor: EventsMonitor, exporter: MockExporter, poll_0: list[GCStatsInfo]
    ) -> None:
        """Each generation has its own ring and counter, so gen-1 records are
        new regardless of how recent the gen-0 slots preceding them are."""
        ingest(monitor, PID, poll_0)

        assert {event.collections for event in exporter.events if event.gen == 1} == {41, 42, 43}
        assert {event.collections for event in exporter.events if event.gen == 2} == {1}

    def test_unwritten_slots_are_skipped(
        self, monitor: EventsMonitor, exporter: MockExporter, poll_0: list[GCStatsInfo]
    ) -> None:
        ingest(monitor, PID, poll_0)

        assert len(exporter.events) == 15
        assert all(event.ts_start > 0 for event in exporter.events)

    def test_emitted_in_timestamp_order(
        self, monitor: EventsMonitor, exporter: MockExporter, poll_0: list[GCStatsInfo]
    ) -> None:
        ingest(monitor, PID, poll_0)

        timestamps = [event.ts_start for event in exporter.events]
        assert timestamps == sorted(timestamps)


class TestSubsequentPoll:
    def test_emits_only_records_not_seen_before(
        self, monitor: EventsMonitor, exporter: MockExporter, poll_0: list[GCStatsInfo], poll_1: list[GCStatsInfo]
    ) -> None:
        ingest(monitor, PID, poll_0)
        exporter.events.clear()

        ingest(monitor, PID, poll_1)

        assert seen(exporter) == {(0, c) for c in range(553, 564)} | {(1, 49), (1, 50), (1, 51)}

    def test_unchanged_generation_emits_nothing(
        self, monitor: EventsMonitor, exporter: MockExporter, poll_0: list[GCStatsInfo], poll_1: list[GCStatsInfo]
    ) -> None:
        """gen 2 did not collect between the polls, so its one written slot
        is unchanged and the monitor must not emit it twice."""
        ingest(monitor, PID, poll_0)
        exporter.events.clear()

        ingest(monitor, PID, poll_1)

        assert [event for event in exporter.events if event.gen == 2] == []

    def test_repeating_a_batch_emits_nothing(
        self, monitor: EventsMonitor, exporter: MockExporter, poll_0: list[GCStatsInfo]
    ) -> None:
        ingest(monitor, PID, poll_0)
        exporter.events.clear()

        ingest(monitor, PID, build_batch(POLL_0))

        assert exporter.events == []

    def test_duplicate_record_is_emitted_once(self, monitor: EventsMonitor, exporter: MockExporter) -> None:
        """Two slots reporting the same counter are one collection, a state
        the target holds while it copies a record forward."""
        item = create_mock_stats_item(gen=0, collections=7, ts_start=1_000, ts_stop=2_000)
        twin = create_mock_stats_item(gen=0, collections=7, ts_start=1_000, ts_stop=2_000)

        ingest(monitor, PID, [item, twin])

        assert len(exporter.events) == 1


class TestCursorScope:
    def test_pids_are_independent(
        self, monitor: EventsMonitor, exporter: MockExporter, poll_0: list[GCStatsInfo]
    ) -> None:
        ingest(monitor, PID, poll_0)
        exporter.events.clear()

        ingest(monitor, 999, build_batch(POLL_0))

        assert len(exporter.events) == 15

    def test_interpreters_are_independent(self, monitor: EventsMonitor, exporter: MockExporter) -> None:
        """Sub-interpreters count from one, so their counters overlap and a
        shared cursor would drop whichever lagged."""
        first = create_mock_stats_item(gen=0, iid=0, collections=90, ts_start=9_000, ts_stop=9_500)
        second = create_mock_stats_item(gen=0, iid=1, collections=3, ts_start=3_000, ts_stop=3_500)

        ingest(monitor, PID, [first, second])

        assert seen(exporter) == {(0, 90), (0, 3)}

    def test_forget_drops_cursors_for_one_pid(
        self, monitor: EventsMonitor, exporter: MockExporter, poll_0: list[GCStatsInfo]
    ) -> None:
        ingest(monitor, PID, poll_0)
        ingest(monitor, 999, build_batch(POLL_0))
        exporter.events.clear()

        monitor._forget(PID, 0)
        ingest(monitor, PID, build_batch(POLL_0))
        ingest(monitor, 999, build_batch(POLL_0))

        assert len(exporter.events) == 15

    def test_forget_is_safe_for_an_unknown_pid(self, monitor: EventsMonitor) -> None:
        monitor._forget(777, 0)


class TestRetain:
    def test_a_stale_cursor_silences_a_reused_pid(self, monitor: EventsMonitor, exporter: MockExporter) -> None:
        """Why the loop has to drop cursors for pids that leave the process
        tree. Nothing here notices the counter restarting, so a reused pid
        stays silent until it climbs past its predecessor."""
        ingest(monitor, PID, [create_mock_stats_item(gen=0, collections=800, ts_start=8_000, ts_stop=8_500)])
        exporter.events.clear()

        ingest(monitor, PID, [create_mock_stats_item(gen=0, collections=2, ts_start=100, ts_stop=200)])

        assert exporter.events == []

    def test_retain_drops_pids_outside_the_tree(
        self, monitor: EventsMonitor, exporter: MockExporter, poll_0: list[GCStatsInfo]
    ) -> None:
        ingest(monitor, PID, poll_0)
        ingest(monitor, 999, build_batch(POLL_0))
        exporter.events.clear()

        monitor._retain({PID}, 0)

        ingest(monitor, PID, build_batch(POLL_0))
        assert exporter.events == [], "the retained pid kept its cursors"
        ingest(monitor, 999, build_batch(POLL_0))
        assert len(exporter.events) == 15, "the dropped pid started over"

    def test_retain_keeps_a_pid_with_no_cursors_yet(self, monitor: EventsMonitor) -> None:
        monitor._retain({PID, 999}, 0)


class TestSettlingAnExitedPid:
    """The monitor is the only side that learns a pid has gone, so it tells
    the statistics. ADR-0016: a ring settles then and never before."""

    def test_retain_settles_the_pids_it_leaves_out(self, monitor: EventsMonitor, stats: StreamingStats) -> None:
        ingest(monitor, PID, [create_mock_stats_item(gen=0, collections=1, ts_start=0, ts_stop=1_000)])
        ingest(monitor, 999, [create_mock_stats_item(gen=0, collections=1, ts_start=0, ts_stop=1_000)])

        monitor._retain({PID}, 0)

        assert stats._open_processes == {proc(PID)}

    def test_the_settled_row_survives_the_pid(self, monitor: EventsMonitor, stats: StreamingStats) -> None:
        """What the run printed for a process that exited half way through it
        still has to be there at the end."""
        ingest(monitor, PID, [create_mock_stats_item(gen=0, collections=1, ts_start=0, ts_stop=1_000)])

        monitor._forget(PID, 0)

        assert stats.rings() == [(proc(PID), 0)]
        assert stats.pause_totals(proc(PID), 0, 0).sampled_count == 1

    def test_the_departed_rings_are_settled_rather_than_dropped(
        self, monitor: EventsMonitor, stats: StreamingStats
    ) -> None:
        """`forget` reads the process out of the registry before retiring it.
        Retiring first would leave nothing to settle, and the predecessor's
        percentiles would stay unfixed with its row still standing."""
        ingest(monitor, PID, [create_mock_stats_item(gen=0, collections=1, ts_start=0, ts_stop=1_000)])

        monitor._forget(PID, 0)

        assert stats._running_rings == {}
        ring = stats.get_ring_stats(proc(PID), 0)
        assert ring is not None
        assert ring["pause"][0].percentiles == {50: 1_000, 90: 1_000, 95: 1_000, 99: 1_000}

    def test_a_successor_on_the_same_pid_gets_a_block_of_its_own(
        self, monitor: EventsMonitor, stats: StreamingStats
    ) -> None:
        """`forget` drops the cursor so the successor's records read as new,
        and its numbers land beside its predecessor's rather than on them.

        The successor collects after the departure here, so nothing is
        re-read and the two blocks are built from disjoint records."""
        ingest(monitor, PID, [create_mock_stats_item(gen=0, collections=1, ts_start=0, ts_stop=1_000)])
        monitor._forget(PID, 5_000)

        ingest(monitor, PID, [create_mock_stats_item(gen=0, collections=1, ts_start=10_000, ts_stop=19_000)])

        assert stats.rings() == [(proc(PID), 0), (proc(PID, 2), 0)]
        assert stats.pause_totals(proc(PID, 1), 0, 0).sampled_pause_ns == 1_000
        assert stats.pause_totals(proc(PID, 2), 0, 0).sampled_pause_ns == 9_000
        assert stats.untracked_rings() == 0

    def test_a_re_read_record_is_dropped(self, monitor: EventsMonitor, exporter: MockExporter) -> None:
        """A pid pruned from the tree loses its cursor, so its successor
        re-reads the ring and hands gcmon records dated inside its
        predecessor's life. They went out under the predecessor already.

        Giving them to the successor would back-date its span to before it
        existed; giving them back to the predecessor would widen a span gcmon
        had stopped observing and leave no retirement final (ADR-0025). The
        successor's own record, dated after the departure, still lands."""
        seen: list[Process] = []
        original = exporter.add_event

        def _watch(process: Process, item: TGCStatsInfo) -> None:
            seen.append(process)
            original(process, item)

        exporter.add_event = _watch  # type: ignore[method-assign]

        ingest(monitor, PID, [create_mock_stats_item(gen=0, collections=1, ts_start=0, ts_stop=1_000)])
        monitor._forget(PID, 5_000)
        ingest(
            monitor,
            PID,
            [
                create_mock_stats_item(gen=0, collections=1, ts_start=0, ts_stop=1_000),
                create_mock_stats_item(gen=0, collections=2, ts_start=10_000, ts_stop=19_000),
            ],
        )

        assert seen == [proc(PID), proc(PID, 2)], (
            "the record dated inside the predecessor's life is a duplicate of one already exported"
        )

    def test_a_pid_polled_after_retain_starts_a_second_block(
        self, monitor: EventsMonitor, stats: StreamingStats
    ) -> None:
        """`retain` is the tick's own listing, so it decides a pid has gone
        the same way `forget` does."""
        ingest(monitor, PID, [create_mock_stats_item(gen=0, collections=1, ts_start=0, ts_stop=1_000)])
        monitor._retain({999}, 5_000)

        ingest(monitor, PID, [create_mock_stats_item(gen=0, collections=1, ts_start=10_000, ts_stop=19_000)])

        assert stats.rings() == [(proc(PID), 0), (proc(PID, 2), 0)]

    def test_retain_settles_a_pid_once(self, monitor: EventsMonitor, stats: StreamingStats) -> None:
        """It runs every tick, and a pid that has gone stays gone. Counting
        each tick as a fresh process would leave a run of empty blocks."""
        ingest(monitor, PID, [create_mock_stats_item(gen=0, collections=1, ts_start=0, ts_stop=1_000)])

        for _ in range(5):
            monitor._retain({999}, 0)

        assert stats.rings() == [(proc(PID), 0)]


class TestPollIntegration:
    def test_poll_uses_the_cursor(
        self,
        monitor: EventsMonitor,
        exporter: MockExporter,
        reader: FakeEventsReader,
        poll_0: list[GCStatsInfo],
        poll_1: list[GCStatsInfo],
    ) -> None:
        """Two polls of the same target, through ``poll`` rather than
        ``_ingest``, so the read path is covered too."""
        polls = iter([poll_0, poll_1])
        reader.reads = lambda pid: next(polls)

        assert monitor._poll(PID).status == PollStatus.OK
        assert len(exporter.events) == 15

        assert monitor._poll(PID).status == PollStatus.OK

        assert len(exporter.events) == 29
