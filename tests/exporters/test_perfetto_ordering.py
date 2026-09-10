"""Tests for process ordering: root descriptor, ``sibling_order_rank``
and ``start_timestamp_ns``.

The code under test lives in ``perfetto_format``, but the subject is
ADR-0011's, which is why these sit apart from the convert-core tests.
"""

from perfetto.protos.perfetto.trace.perfetto_trace_pb2 import (
    TrackDescriptor,
)

from gcmon.exporters.perfetto_format import convert_trace_events_to_perfetto
from gcmon.exporters.perfetto_process_lifetime import process_track_name
from gcmon.exporters.perfetto_track_state import PerfettoTrackState
from gcmon.exporters.trace_converter import convert_item_to_trace_format
from gcmon.model.data import GCStatsInfo
from gcmon.model.trace_event import Instant, TraceEvent
from tests.exporters.perfetto_helpers import (
    parse_track_descriptor,
)
from tests.helpers import proc, process_track


def _process_descriptor_fields_for_pid(
    descriptors: list[bytes],
    pid: int,
) -> list[TrackDescriptor]:
    """Return the ``TrackDescriptor`` protos describing a process on
    *pid*, empty where none does.

    Matched on the name, which carries the pid and the epoch. The
    ``process.pid`` these descriptors carry is the row's, one per process
    and none of them the operating system's (ADR-0011).
    """
    prefix = process_track_name(proc(pid))
    matched: list[TrackDescriptor] = []
    for d in descriptors:
        td = parse_track_descriptor(d)
        if td is None:
            continue
        if td.HasField("process") and (td.name == prefix or td.name.startswith(f"{prefix}#")):
            matched.append(td)
    return matched


def _root_descriptor_fields(descriptors: list[bytes]) -> list[TrackDescriptor]:
    """Return the ``TrackDescriptor`` protos for the root
    descriptor (the one with ``uuid = 0``)."""
    matched: list[TrackDescriptor] = []
    for d in descriptors:
        td = parse_track_descriptor(d)
        if td is None:
            continue
        if td.uuid == 0:
            matched.append(td)
    return matched


class TestProcessOrderingByFirstTs:
    """Wire-level tests for the root descriptor and per-process
    ``sibling_order_rank`` derived from the first event timestamp."""

    def test_root_descriptor_present_with_explicit_ordering(self) -> None:
        state = PerfettoTrackState()
        events: list[TraceEvent] = [
            Instant(process_track(100), "start", ts=5_000),
        ]
        descriptors, _ = convert_trace_events_to_perfetto(
            events,
            state,
            sequence_id=1,
        )
        roots = _root_descriptor_fields(descriptors)
        assert len(roots) == 1
        td = roots[0]
        assert td.process_ordering == 1
        # Nothing gcmon draws is a thread track, so there is no thread
        # ordering to ask for (ADR-0027).
        assert not td.HasField("thread_ordering")
        assert not td.HasField("name")
        assert not td.HasField("process")
        assert not td.HasField("thread")
        assert not td.HasField("counter")
        assert not td.HasField("parent_uuid")
        assert not td.HasField("child_ordering")

    def test_root_descriptor_emitted_exactly_once_across_calls(self) -> None:
        state = PerfettoTrackState()
        events1: list[TraceEvent] = [
            Instant(process_track(100), "first", ts=1_000),
        ]
        events2: list[TraceEvent] = [
            Instant(process_track(200), "second", ts=2_000),
        ]
        d1, _ = convert_trace_events_to_perfetto(events1, state, sequence_id=1)
        d2, _ = convert_trace_events_to_perfetto(events2, state, sequence_id=1)
        total_roots = len(_root_descriptor_fields(d1)) + len(_root_descriptor_fields(d2))
        assert total_roots == 1, f"expected one root descriptor total, got {total_roots}"

    def test_root_descriptor_not_emitted_for_empty_input(self) -> None:
        state = PerfettoTrackState()
        descriptors, packets = convert_trace_events_to_perfetto([], state, sequence_id=1)
        assert descriptors == []
        assert packets == []

    def test_process_descriptor_carries_sibling_order_rank_by_first_ts(self) -> None:
        """Pid with earlier first ts gets the smaller rank."""
        state = PerfettoTrackState()
        events: list[TraceEvent] = [
            Instant(process_track(1), "ev1", ts=2_000),
            Instant(process_track(2), "ev2", ts=1_000),
        ]
        descriptors, _ = convert_trace_events_to_perfetto(
            events,
            state,
            sequence_id=1,
        )
        ranks = {
            pid: td.sibling_order_rank for pid in (1, 2) for td in _process_descriptor_fields_for_pid(descriptors, pid)
        }
        assert ranks == {1: 1, 2: 0}, f"unexpected rank assignment: {ranks}"

    def test_sibling_order_rank_ties_broken_by_pid(self) -> None:
        """When two pids share the same first event ts, ranks follow
        ascending pid (deterministic)."""
        state = PerfettoTrackState()
        events: list[TraceEvent] = [
            Instant(process_track(2), "ev", ts=1_000),
            Instant(process_track(1), "ev", ts=1_000),
        ]
        descriptors, _ = convert_trace_events_to_perfetto(
            events,
            state,
            sequence_id=1,
        )
        ranks = {
            pid: td.sibling_order_rank for pid in (1, 2) for td in _process_descriptor_fields_for_pid(descriptors, pid)
        }
        assert ranks == {1: 0, 2: 1}, f"expected pid-ascending tiebreak; got {ranks}"

    def test_rank_follows_the_first_event_and_not_the_descriptor_order(self) -> None:
        """The pid whose descriptor goes out first is not the pid that
        ranks first: the rank comes from the earliest event, and the
        descriptors follow whichever event named a track first."""
        state = PerfettoTrackState()
        events: list[TraceEvent] = [
            Instant(process_track(100), "late", ts=5_000),
            Instant(process_track(200), "early", ts=1_000),
        ]
        descriptors, _ = convert_trace_events_to_perfetto(
            events,
            state,
            sequence_id=1,
        )
        ranks = {
            pid: td.sibling_order_rank
            for pid in (100, 200)
            for td in _process_descriptor_fields_for_pid(descriptors, pid)
        }
        assert ranks == {100: 1, 200: 0}, f"unexpected rank assignment: {ranks}"

    def test_sibling_order_rank_uses_ts_start_for_gc_stats(self) -> None:
        """For ``TGCStatsInfo`` events, the first event ts is the
        ``ts_start`` (the earliest emitted event for that pause)."""
        state = PerfettoTrackState()
        item1 = GCStatsInfo(
            gen=0,
            iid=0,
            ts_start=3_000,
            ts_stop=4_000,
            heap_size=1000,
            collections=1,
            collected=10,
            uncollectable=0,
            candidates=5,
            duration=0.001,
        )
        events: list[TraceEvent] = [
            Instant(process_track(2), "ev", ts=2_000),
            *convert_item_to_trace_format(proc(1), item1),
        ]
        descriptors, _ = convert_trace_events_to_perfetto(
            events,
            state,
            sequence_id=1,
        )
        ranks = {
            pid: td.sibling_order_rank for pid in (1, 2) for td in _process_descriptor_fields_for_pid(descriptors, pid)
        }
        assert ranks == {1: 1, 2: 0}, f"unexpected rank assignment: {ranks}"

    def test_sibling_order_rank_unchanged_when_input_pid_order_swapped(self) -> None:
        """Reordering the input pids (with the same first-ts values)
        must produce identical rank assignments."""

        def _make_events(ordered_pids: list[int]) -> list[TraceEvent]:
            ts_map = {1: 2_000, 2: 1_000}
            return [ev for pid in ordered_pids for ev in (Instant(process_track(pid), "ev", ts=ts_map[pid]),)]

        s1 = PerfettoTrackState()
        d1, _ = convert_trace_events_to_perfetto(_make_events([1, 2]), s1, sequence_id=1)
        s2 = PerfettoTrackState()
        d2, _ = convert_trace_events_to_perfetto(_make_events([2, 1]), s2, sequence_id=1)
        ranks1 = {pid: td.sibling_order_rank for pid in (1, 2) for td in _process_descriptor_fields_for_pid(d1, pid)}
        ranks2 = {pid: td.sibling_order_rank for pid in (1, 2) for td in _process_descriptor_fields_for_pid(d2, pid)}
        assert ranks1 == ranks2 == {1: 1, 2: 0}

    def test_rank_persists_across_batches(self) -> None:
        """First-ts recorded in one batch must be remembered when
        computing ranks in a later batch (multi-flush invariant)."""
        s = PerfettoTrackState()
        d1, _ = convert_trace_events_to_perfetto(
            [Instant(process_track(1), "a", ts=1_000)],
            s,
            sequence_id=1,
        )
        d2, _ = convert_trace_events_to_perfetto(
            [Instant(process_track(2), "b", ts=5_000)],
            s,
            sequence_id=1,
        )
        # The pre-scan also re-records for batch 2, but the first-ts
        # for pid 1 from batch 1 is preserved (record_first_event_ts
        # only sets the first ts for a pid). Pid 1 should still get
        # rank 0 (ts=1_000) and pid 2 rank 1 (ts=5_000).
        ranks = {
            pid: td.sibling_order_rank
            for descriptors in (d1, d2)
            for pid in (1, 2)
            for td in _process_descriptor_fields_for_pid(descriptors, pid)
        }
        assert ranks == {1: 0, 2: 1}, f"unexpected rank assignment: {ranks}"

    def test_process_descriptor_writes_start_timestamp_ns(self) -> None:
        """Each process descriptor carries ``start_timestamp_ns``
        set to the first non-meta event ts for the pid (nanoseconds).
        The Perfetto UI uses this to align the process track with the
        process's actual start time.
        """
        state = PerfettoTrackState()
        events: list[TraceEvent] = [
            Instant(process_track(100), "start", ts=5_000),
            Instant(process_track(200), "start", ts=1_000),
        ]
        descriptors, _ = convert_trace_events_to_perfetto(
            events,
            state,
            sequence_id=1,
        )
        start_ts: dict[int, int] = {}
        for pid in (100, 200):
            tds = _process_descriptor_fields_for_pid(descriptors, pid)
            assert len(tds) == 1
            start_ts[pid] = tds[0].process.start_timestamp_ns
        assert start_ts == {100: 5_000, 200: 1_000}

    def test_start_timestamp_ns_uses_ts_start_for_gc_stats(self) -> None:
        """For ``TGCStatsInfo`` events, the first-ts (and therefore
        ``start_timestamp_ns``) is the ``ts_start`` of the first GC
        pause, not the ``ts_stop`` or any sub-event ts."""
        from gcmon.model.data import GCStatsInfo

        state = PerfettoTrackState()
        item = GCStatsInfo(
            gen=0,
            iid=0,
            ts_start=3_000,
            ts_stop=4_000,
            heap_size=1000,
            collections=1,
            collected=10,
            uncollectable=0,
            candidates=5,
            duration=0.001,
        )
        events: list[TraceEvent] = [
            Instant(process_track(2), "ev", ts=2_000),
            *convert_item_to_trace_format(proc(1), item),
        ]
        descriptors, _ = convert_trace_events_to_perfetto(
            events,
            state,
            sequence_id=1,
        )
        start_ts: dict[int, int] = {}
        for pid in (1, 2):
            tds = _process_descriptor_fields_for_pid(descriptors, pid)
            start_ts[pid] = tds[0].process.start_timestamp_ns
        assert start_ts == {1: 3_000, 2: 2_000}

    def test_start_timestamp_ns_persists_across_batches(self) -> None:
        """First-ts recorded in one batch must be remembered when
        the process descriptor is emitted in a later batch."""
        s = PerfettoTrackState()
        d1, _ = convert_trace_events_to_perfetto(
            [Instant(process_track(1), "a", ts=1_000)],
            s,
            sequence_id=1,
        )
        d2, _ = convert_trace_events_to_perfetto(
            [Instant(process_track(2), "b", ts=5_000)],
            s,
            sequence_id=1,
        )
        # Pid 1 was seen in batch 1; pid 2 in batch 2.
        tds_1 = _process_descriptor_fields_for_pid(d1, 1)
        assert len(tds_1) == 1
        assert tds_1[0].process.start_timestamp_ns == 1_000
        tds_2 = _process_descriptor_fields_for_pid(d2, 2)
        assert len(tds_2) == 1
        assert tds_2[0].process.start_timestamp_ns == 5_000


class TestARankIsHandedOutOnce:
    """A rank is drawn from a counter that only goes up, and the processes
    described together are sorted before they draw.

    The descriptor carrying a rank is written once, at the first flush that
    names the process, so a rank cannot be revised afterwards: re-emitting a
    corrected one is ignored, the first descriptor for a uuid wins. Sorting
    settles the order within a group; the counter settles it between groups,
    in the order gcmon reached them.
    """

    def _rank(self, descriptors: list[bytes], pid: int) -> int:
        matched = _process_descriptor_fields_for_pid(descriptors, pid)
        assert len(matched) == 1, f"expected one descriptor for pid {pid}, got {len(matched)}"
        return int(matched[0].sibling_order_rank)

    def test_two_batches_never_share_a_rank(self) -> None:
        """The reason this exists. A process reached in a later batch can
        still be observed earlier than one already described -- a first poll
        drains the whole ring, so its oldest record predates the poll. Ranked
        against the batch alone, both took 0 and the UI ordered them
        arbitrarily.
        """
        state = PerfettoTrackState()
        first, _ = convert_trace_events_to_perfetto([Instant(process_track(100), "ev", ts=5_000)], state, sequence_id=1)
        second, _ = convert_trace_events_to_perfetto(
            [Instant(process_track(200), "ev", ts=2_000)], state, sequence_id=1
        )

        assert self._rank(first, 100) == 0
        assert self._rank(second, 200) == 1

    def test_a_later_batch_ranks_after_an_earlier_one(self) -> None:
        """Whatever it was observed at. gcmon cannot rank a process against
        one it has not reached yet, and a process it reaches later was, save
        for the first tick, started later."""
        state = PerfettoTrackState()
        convert_trace_events_to_perfetto([Instant(process_track(100), "ev", ts=9_000)], state, sequence_id=1)
        second, _ = convert_trace_events_to_perfetto(
            [Instant(process_track(200), "ev", ts=1_000), Instant(process_track(300), "ev", ts=8_000)],
            state,
            sequence_id=1,
        )

        # Sorted within the batch, and both after the process already ranked.
        assert self._rank(second, 200) == 1
        assert self._rank(second, 300) == 2

    def test_ranks_are_contiguous_across_batches(self) -> None:
        state = PerfettoTrackState()
        seen: list[int] = []
        for offset, pid in enumerate((100, 200, 300, 400)):
            descriptors, _ = convert_trace_events_to_perfetto(
                [Instant(process_track(pid), "ev", ts=1_000 * (offset + 1))], state, sequence_id=1
            )
            seen.append(self._rank(descriptors, pid))

        assert seen == [0, 1, 2, 3]

    def test_a_second_batch_does_not_move_a_rank_already_given(self) -> None:
        """Emission is idempotent, so a rank that moved would describe a row
        no packet ever carried."""
        state = PerfettoTrackState()
        first, _ = convert_trace_events_to_perfetto([Instant(process_track(100), "ev", ts=5_000)], state, sequence_id=1)
        convert_trace_events_to_perfetto([Instant(process_track(200), "ev", ts=1_000)], state, sequence_id=1)
        again, _ = convert_trace_events_to_perfetto([Instant(process_track(100), "ev", ts=6_000)], state, sequence_id=1)

        assert self._rank(first, 100) == 0
        assert _process_descriptor_fields_for_pid(again, 100) == [], "the descriptor goes out once"


class TestReusedPidRanksAndStampsPerProcess:
    """A pid held twice gets a rank and a start stamp per process.

    Neither field reaches SQL: ``sibling_order_rank`` is a UI hint with no
    column, and ``start_timestamp_ns`` arrives as ``process.start_ts``,
    which cannot say which of two rows on one pid it belongs to without
    the name. So this is where they are asserted, on the wire, and
    ``TestReusedPidDrawsTwoOfEveryRow`` in the integration tests reads
    back what a reader makes of them.
    """

    def _by_name(self, descriptors: list[bytes]) -> dict[str, TrackDescriptor]:
        """Every process descriptor in *descriptors*, keyed on its name.

        Raises rather than deduplicating: a name arriving twice is a
        process described twice, which last-write-wins would hide.
        """
        found = [td for pid in (10, 20) for td in _process_descriptor_fields_for_pid(descriptors, pid)]
        names = [td.name for td in found]
        assert len(set(names)) == len(names), f"a process was described more than once: {names}"
        return {td.name: td for td in found}

    def test_a_successor_ranks_on_its_own_first_observation(self) -> None:
        """The successor's first event falls between the two other
        processes', so it ranks between them. Ranking on the first
        process to hold a pid would give it its predecessor's rank, and
        appending it would put it last.
        """
        state = PerfettoTrackState()
        events: list[TraceEvent] = [
            Instant(process_track(10, 1), "first", ts=1_000),
            Instant(process_track(20, 1), "other", ts=3_000),
            Instant(process_track(10, 2), "second", ts=2_000),
        ]

        descriptors, _ = convert_trace_events_to_perfetto(events, state, sequence_id=1)

        assert {name: td.sibling_order_rank for name, td in self._by_name(descriptors).items()} == {
            "Process 10": 0,
            "Process 10#2": 1,
            "Process 20": 2,
        }

    def test_a_successor_is_stamped_with_its_own_first_observation(self) -> None:
        """The stamp orders the row in the UI and dates it for a reader.
        Taken from the pid it would put the successor's row before the
        successor existed.
        """
        state = PerfettoTrackState()
        events: list[TraceEvent] = [
            Instant(process_track(10, 1), "first", ts=1_000),
            Instant(process_track(10, 2), "second", ts=2_000),
        ]

        descriptors, _ = convert_trace_events_to_perfetto(events, state, sequence_id=1)

        assert {name: td.process.start_timestamp_ns for name, td in self._by_name(descriptors).items()} == {
            "Process 10": 1_000,
            "Process 10#2": 2_000,
        }

    def test_each_descriptor_carries_a_pid_of_its_own(self) -> None:
        """One pid on both would read as one process described twice, and
        fold the rows together. Each takes one off gcmon's own count instead
        (ADR-0011)."""
        state = PerfettoTrackState()
        events: list[TraceEvent] = [
            Instant(process_track(10, 1), "first", ts=1_000),
            Instant(process_track(10, 2), "second", ts=2_000),
        ]

        descriptors, _ = convert_trace_events_to_perfetto(events, state, sequence_id=1)

        pids = {name: td.process.pid for name, td in self._by_name(descriptors).items()}
        assert sorted(pids) == ["Process 10", "Process 10#2"]
        assert len(set(pids.values())) == 2, f"two processes share a row pid: {pids}"
        assert 10 not in pids.values(), f"a row pid is gcmon's, not the operating system's: {pids}"
