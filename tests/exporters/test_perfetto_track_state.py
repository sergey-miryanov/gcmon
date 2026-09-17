"""Tests for ``PerfettoTrackState`` uuid allocation and bookkeeping."""

from gcmon.exporters.perfetto_track_state import PerfettoTrackState
from gcmon.exporters.trace_converter import counter_display_name
from gcmon.model.names import COLLECTED, HEAP_SIZE
from gcmon.model.process import Process
from tests.conftest import DEFAULT_PID
from tests.exporters.perfetto_helpers import span
from tests.helpers import interpreter_track, loss_track, proc

# The process the state is asked about.
TARGET_PID: int = 100

# A second process, so a lookup cannot pass by answering for the first.
OTHER_PID: int = 200


class TestPerfettoTrackState:
    def test_init_empty(self, state: PerfettoTrackState) -> None:
        assert not state.has_process_descriptor(proc(123))
        assert not state.has_track(interpreter_track(123, 0))
        assert not state.has_counter_track(interpreter_track(123, 0), counter_display_name(0, COLLECTED))

    def test_pid_tracking(self, state: PerfettoTrackState) -> None:
        assert not state.has_process_descriptor(proc(TARGET_PID))
        state.mark_process_descriptor(proc(TARGET_PID))
        assert state.has_process_descriptor(proc(TARGET_PID))
        assert not state.has_process_descriptor(proc(OTHER_PID))

    def test_tid_tracking(self, state: PerfettoTrackState) -> None:
        assert not state.has_track(interpreter_track(TARGET_PID, 0))
        state.mark_track(interpreter_track(TARGET_PID, 0))
        assert state.has_track(interpreter_track(TARGET_PID, 0))
        assert not state.has_track(interpreter_track(TARGET_PID, 1))
        assert not state.has_track(interpreter_track(OTHER_PID, 0))

    def test_process_track_uuid(self, state: PerfettoTrackState) -> None:
        uuid = state.get_process_track_uuid(proc(DEFAULT_PID))
        assert uuid == 1

    def test_thread_track_uuid(self, state: PerfettoTrackState) -> None:
        uuid = state.get_track_uuid(interpreter_track(DEFAULT_PID, 0))
        assert uuid == 1

    def test_thread_track_uuid_different_iid(self, state: PerfettoTrackState) -> None:
        uuid0 = state.get_track_uuid(interpreter_track(DEFAULT_PID, 0))
        uuid1 = state.get_track_uuid(interpreter_track(DEFAULT_PID, 1))
        assert uuid0 != uuid1

    def test_counter_track_uuid_sequential(self, state: PerfettoTrackState) -> None:
        uuid0 = state.get_or_create_counter_track_uuid(
            interpreter_track(TARGET_PID, 0), counter_display_name(0, COLLECTED)
        )
        uuid1 = state.get_or_create_counter_track_uuid(interpreter_track(TARGET_PID, 0), HEAP_SIZE)
        assert uuid0 == 1
        assert uuid1 == 2

    def test_counter_track_uuid_idempotent(self, state: PerfettoTrackState) -> None:
        uuid1 = state.get_or_create_counter_track_uuid(
            interpreter_track(TARGET_PID, 0), counter_display_name(0, COLLECTED)
        )
        uuid2 = state.get_or_create_counter_track_uuid(
            interpreter_track(TARGET_PID, 0), counter_display_name(0, COLLECTED)
        )
        assert uuid1 == uuid2

    def test_has_counter_track(self, state: PerfettoTrackState) -> None:
        assert not state.has_counter_track(interpreter_track(TARGET_PID, 0), counter_display_name(0, COLLECTED))
        state.get_or_create_counter_track_uuid(interpreter_track(TARGET_PID, 0), counter_display_name(0, COLLECTED))
        assert state.has_counter_track(interpreter_track(TARGET_PID, 0), counter_display_name(0, COLLECTED))
        assert not state.has_counter_track(interpreter_track(TARGET_PID, 0), counter_display_name(1, COLLECTED))


class TestRankAllocation:
    """`rank_processes` hands out each rank once, sorting the group it is
    given before it draws from a counter that only goes up."""

    def _ranked(self, state: PerfettoTrackState, *processes: Process) -> list[int | None]:
        return [state.get_process_track_rank(process) for process in processes]

    def test_a_process_with_no_span_is_left_unranked(self, state: PerfettoTrackState) -> None:
        """gcmon has not observed it, so there is nothing to sort it by."""
        state.rank_processes([proc(TARGET_PID)])
        assert state.get_process_track_rank(proc(TARGET_PID)) is None

    def test_a_group_is_sorted_by_first_observation(self, state: PerfettoTrackState) -> None:
        state.update_process_lifetime(proc(TARGET_PID), 5_000)
        state.update_process_lifetime(proc(OTHER_PID), 1_000)
        state.rank_processes([proc(TARGET_PID), proc(OTHER_PID)])
        assert self._ranked(state, proc(OTHER_PID), proc(TARGET_PID)) == [0, 1]

    def test_the_group_order_does_not_reach_the_ranks(self, state: PerfettoTrackState) -> None:
        state.update_process_lifetime(proc(TARGET_PID), 5_000)
        state.update_process_lifetime(proc(OTHER_PID), 1_000)
        state.rank_processes([proc(OTHER_PID), proc(TARGET_PID)])
        assert self._ranked(state, proc(OTHER_PID), proc(TARGET_PID)) == [0, 1]

    def test_an_equal_start_is_broken_by_process(self, state: PerfettoTrackState) -> None:
        for pid in (200, 100):
            state.update_process_lifetime(proc(pid), 1_000)
        state.rank_processes([proc(OTHER_PID), proc(TARGET_PID)])
        assert self._ranked(state, proc(TARGET_PID), proc(OTHER_PID)) == [0, 1]

    def test_a_later_group_takes_the_ranks_after_it(self, state: PerfettoTrackState) -> None:
        """Even where it was observed first: gcmon cannot sort a process
        against one it has not reached."""
        state.update_process_lifetime(proc(TARGET_PID), 5_000)
        state.rank_processes([proc(TARGET_PID)])
        state.update_process_lifetime(proc(OTHER_PID), 1_000)
        state.rank_processes([proc(OTHER_PID)])
        assert self._ranked(state, proc(TARGET_PID), proc(OTHER_PID)) == [0, 1]

    def test_ranking_twice_leaves_the_first_answer(self, state: PerfettoTrackState) -> None:
        state.update_process_lifetime(proc(TARGET_PID), 5_000)
        state.rank_processes([proc(TARGET_PID)])
        state.update_process_lifetime(proc(OTHER_PID), 1_000)
        state.rank_processes([proc(OTHER_PID), proc(TARGET_PID)])
        assert self._ranked(state, proc(TARGET_PID), proc(OTHER_PID)) == [0, 1]

    def test_a_repeated_process_in_one_group_spends_one_rank(self, state: PerfettoTrackState) -> None:
        """The convert pass names a process once per event on it."""
        state.update_process_lifetime(proc(TARGET_PID), 5_000)
        state.update_process_lifetime(proc(OTHER_PID), 9_000)
        state.rank_processes([proc(TARGET_PID), proc(TARGET_PID), proc(OTHER_PID)])
        assert self._ranked(state, proc(TARGET_PID), proc(OTHER_PID)) == [0, 1]

    def test_an_unobservable_process_spends_no_rank(self, state: PerfettoTrackState) -> None:
        """It stays unranked, and the one beside it still takes 0."""
        state.update_process_lifetime(proc(OTHER_PID), 9_000)
        state.rank_processes([proc(TARGET_PID), proc(OTHER_PID)])
        assert self._ranked(state, proc(TARGET_PID), proc(OTHER_PID)) == [None, 0]


class TestInterpreterCount:
    """How many interpreters gcmon read a record from, per process.

    An `InterpreterTrack` is built in one place, off a record's iid, so a
    track marked here is an interpreter that produced something.
    """

    def test_a_process_with_no_tracks_counts_zero(self, state: PerfettoTrackState) -> None:
        assert state.get_interpreter_count(proc(TARGET_PID)) == 0

    def test_each_interpreter_counts_once(self, state: PerfettoTrackState) -> None:
        state.mark_track(interpreter_track(TARGET_PID, 0))
        state.mark_track(interpreter_track(TARGET_PID, 1))
        assert state.get_interpreter_count(proc(TARGET_PID)) == 2

    def test_marking_one_twice_counts_once(self, state: PerfettoTrackState) -> None:
        state.mark_track(interpreter_track(TARGET_PID, 0))
        state.mark_track(interpreter_track(TARGET_PID, 0))
        assert state.get_interpreter_count(proc(TARGET_PID)) == 1

    def test_a_loss_row_does_not_widen_the_count(self, state: PerfettoTrackState) -> None:
        """A loss row names an interpreter but is not one, and gcmon builds
        its groups from records it already read."""
        state.mark_track(interpreter_track(TARGET_PID, 0))
        state.mark_track(loss_track(TARGET_PID, 0))
        assert state.get_interpreter_count(proc(TARGET_PID)) == 1

    def test_another_process_is_counted_apart(self, state: PerfettoTrackState) -> None:
        state.mark_track(interpreter_track(TARGET_PID, 0))
        state.mark_track(interpreter_track(OTHER_PID, 0))
        state.mark_track(interpreter_track(OTHER_PID, 1))
        assert state.get_interpreter_count(proc(TARGET_PID)) == 1
        assert state.get_interpreter_count(proc(OTHER_PID)) == 2

    def test_two_processes_on_one_pid_are_counted_apart(self, state: PerfettoTrackState) -> None:
        state.mark_track(interpreter_track(TARGET_PID, 0))
        state.mark_track(interpreter_track(TARGET_PID, 0, pid_epoch=2))
        state.mark_track(interpreter_track(TARGET_PID, 1, pid_epoch=2))
        assert state.get_interpreter_count(proc(TARGET_PID)) == 1
        assert state.get_interpreter_count(proc(TARGET_PID, 2)) == 2


class TestProcessLifetimeState:
    """State accessors for the shared ``Processes`` track."""

    def test_track_uuid_lazy_and_idempotent(self, state: PerfettoTrackState) -> None:
        assert not state.has_process_lifetime_track()
        uuid1 = state.get_or_create_process_lifetime_track_uuid()
        assert state.has_process_lifetime_track()
        uuid2 = state.get_or_create_process_lifetime_track_uuid()
        assert uuid1 == uuid2

    def test_track_uuid_distinct_from_process_uuid(self, state: PerfettoTrackState) -> None:
        proc_uuid = state.get_process_track_uuid(proc(TARGET_PID))
        lifetime_uuid = state.get_or_create_process_lifetime_track_uuid()
        assert lifetime_uuid != proc_uuid

    def test_first_update_seeds_both_ends(self, state: PerfettoTrackState) -> None:
        assert not state.has_process_lifetime(proc(TARGET_PID))
        state.update_process_lifetime(proc(TARGET_PID), 1_000)
        assert state.has_process_lifetime(proc(TARGET_PID))
        assert state.get_process_lifetimes() == [span(TARGET_PID, 1_000, 1_000)]
        assert not state.has_process_lifetime(proc(OTHER_PID))

    def test_span_widens_in_both_directions(self, state: PerfettoTrackState) -> None:
        state.update_process_lifetime(proc(TARGET_PID), 2_000)
        state.update_process_lifetime(proc(TARGET_PID), 5_000)
        state.update_process_lifetime(proc(TARGET_PID), 1_000)
        state.update_process_lifetime(proc(TARGET_PID), 3_000)  # inside; no effect
        assert state.get_process_lifetimes() == [span(TARGET_PID, 1_000, 5_000)]

    def test_a_counter_widens_both_ends(self, state: PerfettoTrackState) -> None:
        """The reverse of the old rule, where a counter moved the start
        and never the end. An RSS sample at 9us is evidence the process
        was alive at 9us exactly as a GC event would be, and the caller
        no longer says which kind it is holding."""
        state.update_process_lifetime(proc(TARGET_PID), 2_000)
        state.update_process_lifetime(proc(TARGET_PID), 4_000)
        # A counter before the span's start pulls the start back...
        state.update_process_lifetime(proc(TARGET_PID), 1_000)
        # ...and one after its end pushes the end out.
        state.update_process_lifetime(proc(TARGET_PID), 9_000)
        assert state.get_process_lifetimes() == [span(TARGET_PID, 1_000, 9_000)]

    def test_counter_only_pid_gets_a_span(self, state: PerfettoTrackState) -> None:
        """A pid seen only through counters used to get a start, and
        therefore a rank, but no span and no slice. It now gets both."""
        state.update_process_lifetime(proc(TARGET_PID), 1_000)
        state.update_process_lifetime(proc(TARGET_PID), 7_000)
        assert state.has_process_lifetime(proc(TARGET_PID))
        assert state.get_process_lifetime_start_ts(proc(TARGET_PID)) == 1_000
        assert state.get_process_lifetimes() == [span(TARGET_PID, 1_000, 7_000)]

    def test_a_leading_counter_seeds_the_end(self, state: PerfettoTrackState) -> None:
        """A counter sets the end like anything else, including when it
        is the first event folded for a pid.

        Events reach the encoder in buffer order, not timestamp order: a
        poll returns GC events that already happened, while an RSS sample
        is stamped when taken, so a counter can arrive first and carry a
        later ts than every GC event in the batch.
        """
        state.update_process_lifetime(proc(TARGET_PID), 1_000)
        state.update_process_lifetime(proc(TARGET_PID), 500)
        state.update_process_lifetime(proc(TARGET_PID), 600)
        assert state.get_process_lifetimes() == [span(TARGET_PID, 500, 1_000)]

    def test_both_ends_always_carry_the_same_pids(self, state: PerfettoTrackState) -> None:
        """One call gives a pid both a start and an end, so no pid can
        land in one dict and not the other -- which is what lets
        ``get_process_lifetimes`` index the start dict while iterating
        the end one."""
        for pid, ts in ((100, 1_000), (200, 2_000), (100, 3_000)):
            state.update_process_lifetime(proc(pid), ts)
        assert state._process_lifetime_start.keys() == state._process_lifetime_end.keys()
        assert sorted(state.get_process_lifetimes()) == [span(TARGET_PID, 1_000, 3_000), span(OTHER_PID, 2_000, 2_000)]

    def test_get_returns_every_span_regardless_of_order(self, state: PerfettoTrackState) -> None:
        """Order is not part of the contract -- ``_clip_spans_to_laminar``
        sorts what it needs -- so this pins the contents only."""
        # Inserted out of order, with a tie on start ts
        # between pids 300 and 100.
        for one in (
            span(OTHER_PID, 2_000, 3_000),
            span(300, 1_000, 4_000),
            span(TARGET_PID, 1_000, 9_000),
        ):
            state.update_process_lifetime(one.process, one.start_ts)
            state.update_process_lifetime(one.process, one.end_ts)
        assert sorted(state.get_process_lifetimes()) == [
            span(TARGET_PID, 1_000, 9_000),
            span(OTHER_PID, 2_000, 3_000),
            span(300, 1_000, 4_000),
        ]

    def test_get_does_not_drain(self, state: PerfettoTrackState) -> None:
        """Reading spans has no side effect: the once-per-trace contract
        is ``finalize_perfetto_packets``' flag, not a drain here."""
        state.update_process_lifetime(proc(TARGET_PID), 1_000)
        assert state.get_process_lifetimes() == [span(TARGET_PID, 1_000, 1_000)]
        assert state.get_process_lifetimes() == [span(TARGET_PID, 1_000, 1_000)]
        assert state.has_process_lifetime(proc(TARGET_PID))
        assert state.get_process_lifetime_start_ts(proc(TARGET_PID)) == 1_000

    def test_process_lifetime_emitted_flag(self, state: PerfettoTrackState) -> None:
        assert not state.has_process_lifetime_emitted()
        state.mark_process_lifetime_emitted()
        assert state.has_process_lifetime_emitted()


class TestTwoProcessesOnOnePidGetTheirOwnRows:
    """A `Track` names the process it was drawn for, and every row the
    exporter allocates is filed under that process: a pid handed on draws two
    process tracks, two thread tracks, two loss tracks, two counter groups and
    two of each counter (ADR-0011)."""

    FIRST = interpreter_track(TARGET_PID, 0, 1)
    SECOND = interpreter_track(TARGET_PID, 0, 2)

    def test_the_process_track_gets_a_uuid_per_process(self, state: PerfettoTrackState) -> None:

        assert state.get_process_track_uuid(proc(TARGET_PID, 2)) != state.get_process_track_uuid(proc(TARGET_PID, 1))

    def test_the_process_descriptor_goes_out_per_process(self, state: PerfettoTrackState) -> None:
        state.mark_process_descriptor(proc(TARGET_PID, 1))

        assert not state.has_process_descriptor(proc(TARGET_PID, 2))

    def test_the_thread_track_gets_a_uuid_per_process(self, state: PerfettoTrackState) -> None:

        assert state.get_track_uuid(self.SECOND) != state.get_track_uuid(self.FIRST)

    def test_the_thread_descriptor_goes_out_per_process(self, state: PerfettoTrackState) -> None:
        state.mark_track(self.FIRST)

        assert not state.has_track(self.SECOND)

    def test_the_loss_track_gets_a_uuid_per_process(self, state: PerfettoTrackState) -> None:

        assert state.get_track_uuid(loss_track(TARGET_PID, 0, 2)) != state.get_track_uuid(loss_track(TARGET_PID, 0, 1))

    def test_the_counter_group_gets_a_uuid_per_process(self, state: PerfettoTrackState) -> None:
        first = state.get_or_create_counter_group_track_uuid(self.FIRST)

        assert not state.has_counter_group_track(self.SECOND)
        assert state.get_or_create_counter_group_track_uuid(self.SECOND) != first

    def test_a_counter_gets_a_uuid_per_process(self, state: PerfettoTrackState) -> None:
        first = state.get_or_create_counter_track_uuid(self.FIRST, counter_display_name(0, COLLECTED))

        assert not state.has_counter_track(self.SECOND, counter_display_name(0, COLLECTED))
        assert state.get_or_create_counter_track_uuid(self.SECOND, counter_display_name(0, COLLECTED)) != first

    def test_two_interpreters_are_still_two_rows(self, state: PerfettoTrackState) -> None:
        """The control: dropping the epoch must not fold anything else."""

        assert state.get_track_uuid(interpreter_track(TARGET_PID, 1, 1)) != state.get_track_uuid(self.FIRST)


class TestCaptureTotals:
    """What gcmon read for a process and what it never got to read.

    Both are running totals over the whole trace, folded in one record and
    one loss interval at a time.
    """

    def test_a_process_nothing_was_recorded_for_reads_zero(self, state: PerfettoTrackState) -> None:
        assert state.get_sampled_count(proc(TARGET_PID)) == 0
        assert state.get_lost_count(proc(TARGET_PID)) == 0
        assert state.get_lost_pause_ns(proc(TARGET_PID)) == 0

    def test_each_record_counts_one(self, state: PerfettoTrackState) -> None:
        for _ in range(3):
            state.record_sampled(proc(TARGET_PID))
        assert state.get_sampled_count(proc(TARGET_PID)) == 3

    def test_loss_intervals_add_up(self, state: PerfettoTrackState) -> None:
        state.record_loss(proc(TARGET_PID), lost_count=4, lost_pause_ns=1_000)
        state.record_loss(proc(TARGET_PID), lost_count=6, lost_pause_ns=2_500)
        assert state.get_lost_count(proc(TARGET_PID)) == 10
        assert state.get_lost_pause_ns(proc(TARGET_PID)) == 3_500

    def test_an_interval_that_lost_nothing_leaves_the_totals_alone(self, state: PerfettoTrackState) -> None:
        """The fold is additive, so a zero cannot clear what is there.

        `EventsMonitor` sends no such interval, and this holds the
        accumulator to arithmetic rather than to that guarantee.
        """
        state.record_loss(proc(TARGET_PID), lost_count=4, lost_pause_ns=1_000)
        state.record_loss(proc(TARGET_PID), lost_count=0, lost_pause_ns=0)
        assert state.get_lost_count(proc(TARGET_PID)) == 4
        assert state.get_lost_pause_ns(proc(TARGET_PID)) == 1_000

    def test_records_and_losses_are_counted_apart(self, state: PerfettoTrackState) -> None:
        state.record_sampled(proc(TARGET_PID))
        state.record_loss(proc(TARGET_PID), lost_count=7, lost_pause_ns=99)
        assert state.get_sampled_count(proc(TARGET_PID)) == 1
        assert state.get_lost_count(proc(TARGET_PID)) == 7

    def test_each_process_keeps_its_own_totals(self, state: PerfettoTrackState) -> None:
        state.record_sampled(proc(TARGET_PID))
        state.record_sampled(proc(OTHER_PID))
        state.record_sampled(proc(OTHER_PID))
        state.record_loss(proc(OTHER_PID), lost_count=3, lost_pause_ns=50)
        assert state.get_sampled_count(proc(TARGET_PID)) == 1
        assert state.get_lost_count(proc(TARGET_PID)) == 0
        assert state.get_sampled_count(proc(OTHER_PID)) == 2
        assert state.get_lost_count(proc(OTHER_PID)) == 3

    def test_two_processes_on_one_pid_keep_their_own_totals(self, state: PerfettoTrackState) -> None:
        """A reused pid draws two rows, and each bar counts its own life."""
        state.record_sampled(proc(TARGET_PID, 1))
        state.record_loss(proc(TARGET_PID, 2), lost_count=5, lost_pause_ns=10)
        assert state.get_sampled_count(proc(TARGET_PID, 1)) == 1
        assert state.get_lost_count(proc(TARGET_PID, 1)) == 0
        assert state.get_sampled_count(proc(TARGET_PID, 2)) == 0
        assert state.get_lost_count(proc(TARGET_PID, 2)) == 5


class TestRowPids:
    """The pid gcmon writes for a process row.

    The trace processor keys process identity on that field, so it is the one
    thing that has to differ between two processes on one operating-system pid
    (ADR-0011).
    """

    def test_one_process_keeps_the_same_row_pid(self, state: PerfettoTrackState) -> None:
        assert state.get_row_pid(proc(TARGET_PID)) == state.get_row_pid(proc(TARGET_PID))

    def test_two_processes_on_one_pid_get_two(self, state: PerfettoTrackState) -> None:
        """The regression: equal row pids fold both processes into one row."""
        assert state.get_row_pid(proc(TARGET_PID, 1)) != state.get_row_pid(proc(TARGET_PID, 2))

    def test_no_two_processes_share_a_row_pid(self, state: PerfettoTrackState) -> None:
        """The invariant the whole pid exists for. Two processes on one pid is
        the case that used to break it, and two pids is the case that must
        keep working."""
        pids = [state.get_row_pid(proc(pid, epoch)) for pid in (100, 200, 300) for epoch in (1, 2, 3)]
        assert len(set(pids)) == len(pids), pids

    def test_the_count_leaves_the_idle_process_alone(self) -> None:
        """The trace processor files its own idle process at ``pid = 0``, so
        counting from there would draw gcmon's first row on it."""
        assert PerfettoTrackState().get_row_pid(proc(TARGET_PID)) == 1
