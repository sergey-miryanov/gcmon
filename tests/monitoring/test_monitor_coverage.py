"""Tests for the warning a run gets when it read too little of its target.

`StreamingStats` answers whether coverage dropped; wording that and saying it
once are the monitor's, so every test here reads the log. The stats side reads
the answer instead, in `tests/stats/test_stats.py`.

Batches are hand-built and go through `_ingest`, the in-memory pattern
`test_monitor_cursor.py` uses: no target process, and a `collections` counter
that skips is all it takes to make a run lossy.
"""

from collections.abc import Sequence
from itertools import count

import pytest

from gcmon.model.data import GCStatsInfo
from gcmon.monitoring.monitor import EventsMonitor
from tests.helpers import create_mock_stats_item, polled

PID = 4242
OTHER_PID = 4243

# "so far": a run that recovers ends well above the figure warned about here,
# and the end-of-run summary states the final one.
ADVISORY = "of collections observed so far"

# One collection's pause and the interval between two of them, in nanoseconds.
PAUSE_NS = 1_000_000
PERIOD_NS = 2 * PAUSE_NS

_POLL_CLOCK = count(1_000_000_000, 100_000_000)


def ring(collections: Sequence[int], gen: int = 0, iid: int = 0) -> list[GCStatsInfo]:
    """One ring of a poll, holding the records at *collections*.

    ``duration`` is the target's cumulative pause total, which is what the loss
    reconstruction reads, so it counts every run up to the record rather than
    the ones this poll returned.
    """
    return [
        create_mock_stats_item(
            gen=gen,
            iid=iid,
            collections=n,
            ts_start=n * PERIOD_NS,
            ts_stop=n * PERIOD_NS + PAUSE_NS,
            duration=n * PAUSE_NS / 1e9,
        )
        for n in collections
    ]


def poll(monitor: EventsMonitor, pid: int, collections: Sequence[int], gen: int = 0) -> None:
    """One poll of *pid*, at the next instant on a clock shared by this file.

    `_ingest` bounds a loss record by the two polls around it, so it has to be
    told when this one happened. Nothing here reads those instants; they only
    have to increase.
    """
    poll_rings(monitor, pid, ring(collections, gen=gen))


def poll_rings(monitor: EventsMonitor, pid: int, rings: list[GCStatsInfo]) -> None:
    """One poll of *pid* returning several rings at once.

    A read hands back every interpreter's buffer together, which is where a
    starved interpreter sits.
    """
    monitor._ingest(polled(monitor, pid), rings, next(_POLL_CLOCK))


class TestCoverageWarning:
    def test_it_fires_below_the_threshold(self, monitor: EventsMonitor, caplog: pytest.LogCaptureFixture) -> None:
        """Two records read of ten that ran: 20%, under the 90% floor."""
        poll(monitor, PID, [1])
        poll(monitor, PID, [10])

        assert f"PID {PID} interpreter 0 generation 0: only 20% {ADVISORY}" in caplog.text

    def test_a_figure_under_the_floor_does_not_read_as_meeting_it(
        self, monitor: EventsMonitor, caplog: pytest.LogCaptureFixture
    ) -> None:
        """225 read of 251 is 89.6%, which rounds to the floor the warning
        fires below. `Cov` has the same hazard and the same answer."""
        poll(monitor, PID, range(1, 225))
        poll(monitor, PID, [251])

        assert f"only 89% {ADVISORY}" in caplog.text

    def test_a_float_artifact_does_not_cost_a_point(
        self, monitor: EventsMonitor, caplog: pytest.LogCaptureFixture
    ) -> None:
        """29 read of 100 is 0.29, and 0.29 * 100 is 28.999999999999996.
        Truncating that alone understates a run by a whole point."""
        poll(monitor, PID, range(1, 29))
        poll(monitor, PID, [100])

        assert f"only 29% {ADVISORY}" in caplog.text

    def test_it_stays_quiet_above_the_threshold(self, monitor: EventsMonitor, caplog: pytest.LogCaptureFixture) -> None:
        poll(monitor, PID, range(1, 100))
        poll(monitor, PID, [101])

        assert ADVISORY not in caplog.text

    def test_it_fires_once_across_many_polls(self, monitor: EventsMonitor, caplog: pytest.LogCaptureFixture) -> None:
        """A lossy run loses records every tick, so a warning per poll would
        bury the trace it is warning about."""
        for collections in range(1, 200, 20):
            poll(monitor, PID, [collections])

        assert caplog.text.count(ADVISORY) == 1

    def test_one_warning_covers_every_pid(self, monitor: EventsMonitor, caplog: pytest.LogCaptureFixture) -> None:
        """The latch is per run, not per pid: the advice is about the poll
        rate, which no pid owns."""
        poll(monitor, PID, [1])
        poll(monitor, OTHER_PID, [1])
        poll(monitor, PID, [10])
        poll(monitor, OTHER_PID, [10])

        assert caplog.text.count(ADVISORY) == 1

    def test_a_starved_interpreter_beside_a_busy_one_fires_and_is_named(
        self, monitor: EventsMonitor, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Interpreter 0 reads everything it ran; interpreter 1 reads two of
        ten. Folded into one figure the process clears the floor, which is
        what kept this quiet while coverage was keyed per process.
        """
        poll_rings(monitor, PID, ring(range(1, 100), iid=0) + ring([1], iid=1))
        poll_rings(monitor, PID, ring([100], iid=0) + ring([10], iid=1))

        blended = monitor._stats.pause_totals_by_gen()[0]
        assert blended.coverage > 0.9, "the process-wide figure has to clear the floor"
        assert f"PID {PID} interpreter 1 generation 0: only 20% {ADVISORY}" in caplog.text

    def test_it_names_the_worst_interpreter_of_the_two_under_the_floor(
        self, monitor: EventsMonitor, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Interpreter 0 reads 88 of 100, interpreter 1 two of twenty. One
        warning per run, so it goes to the one worth reading."""
        poll_rings(monitor, PID, ring(range(1, 88), iid=0) + ring([1], iid=1))
        poll_rings(monitor, PID, ring([100], iid=0) + ring([20], iid=1))

        assert f"PID {PID} interpreter 1 generation 0: only 10% {ADVISORY}" in caplog.text
        assert "interpreter 0" not in caplog.text

    def test_this_polls_own_records_count_before_it_fires(
        self, monitor: EventsMonitor, caplog: pytest.LogCaptureFixture
    ) -> None:
        """`_ingest` records every ring's gap before it updates any of them, so
        a check made at `record_loss` would divide this poll's gap into the
        sample as it stood before the poll. Two polls, 2 records then 100 with
        1 lost: measured that early the coverage reads 2/3, and the latch would
        keep that figure for a run that ends at 99%.
        """
        poll(monitor, PID, [1, 2])
        poll(monitor, PID, range(4, 104))

        assert ADVISORY not in caplog.text

    def test_it_says_what_survives_and_leaves_the_remedy_to_the_summary(
        self, monitor: EventsMonitor, caplog: pytest.LogCaptureFixture
    ) -> None:
        """This fires once, early, possibly before the loop has run enough
        ticks to know whether it is keeping up -- so it cannot say whether a
        smaller `--rate` would help. The end-of-run summary can, and does."""
        poll(monitor, PID, [1])
        poll(monitor, PID, [10])

        assert "reconstructed and exact" in caplog.text
        assert "--rate" not in caplog.text

    def test_it_reads_as_a_running_figure(self, monitor: EventsMonitor, caplog: pytest.LogCaptureFixture) -> None:
        """The latch keeps the first figure that dipped, which a run that
        recovers ends far above. Stated flatly, it would read as contradicting
        the end-of-run summary's final one."""
        poll(monitor, PID, [1])
        poll(monitor, PID, [10])
        for collections in range(11, 2_000):
            poll(monitor, PID, [collections])

        assert "only 20% of collections observed so far" in caplog.text

    def test_it_names_no_ring_geometry(self, monitor: EventsMonitor, caplog: pytest.LogCaptureFixture) -> None:
        """An operator cannot act on how many slots the target's ring holds,
        which is why the wording moved."""
        poll(monitor, PID, [1])
        poll(monitor, PID, [10])

        assert ADVISORY in caplog.text, "the warning has to fire for this to test anything"
        assert "ring" not in caplog.text
        assert "record" not in caplog.text
