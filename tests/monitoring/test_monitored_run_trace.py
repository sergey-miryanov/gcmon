"""A whole monitored run, pinned packet by packet.

Everything else in the suite checks one piece of a tick. `test_loss_replay`
drives `EventsMonitor.poll` off the capture and never runs the loop;
`test_monitor_loop` runs the loop against a `MagicMock` monitor that produces no
trace. This file covers the seam between them: discovery, prune, poll order, the
policy verdict, liveness, and the trace that comes out the other end.

It runs the real `MonitorLoop` over the real `EventsMonitor` over the real
Perfetto exporter, feeds it the `SSL_CONTEXT_SIZE` capture through a scripted
process tree and clock, and compares the decoded trace to
`tests/fixtures/monitored_run_perfetto_trace.txt`.

**This test exists to be broken.** A red run means the trace an
operator opens is not the trace they opened yesterday. Read the diff, convince
yourself every changed line is one you meant, then regenerate the fixture with

    PYTHONPATH=src python -m tests.monitoring.test_monitored_run_trace

and commit it alongside the change that moved it. Never regenerate to clear a
red run.

Determinism comes down to five things.

*One clock, two consumers.* `monitor_loop` and `monitor` both do `import time`,
so one patch of `time.monotonic_ns` feeds both. The loop reads once before the
run to seed position zero. Per tick it reads once to stamp the tick, then each
polled pid costs the monitor two reads either side of `get_gc_stats`, then the
loop reads once more to pace itself
([ADR-0019](../../docs/adr/0019-schedule-tick-starts-on-a-fixed-grid.md)).
`_script` lays that sequence out in advance and `_ScriptedClock` hands it out in
order, raising rather than inventing a value. `test_the_clock_was_spent_exactly`
checks none was left over: reading fewer instants would shift every timestamp
downstream and still pass.

Only the stamping read reaches an event, so the pacing read's value is free and
the fixture did not move when it was added.

*Nothing else reads the machine.* `no_wait_policy` instead of
`StartupTimeoutPolicy`, which reads `time.monotonic`; a fixed-tick runner
instead of `DurationRunner`, which reads it too; a 1 ms rate; and no RSS sampler.

*The capture drives both pids.* The target replays `SSL_CONTEXT_SIZE` as
recorded. The child replays the same collections `CHILD_SKEW_NS` later, so its
ring lands on different instants and its loss windows fall elsewhere. Two pids
with different per-pid state, not one counted twice.

*The one value the encoder would have invented is not left to it.*
`sequence_id` is passed in rather than derived from `id(self)`. Track uuids
count from 1 in allocation order, which the script above already fixes, and
the registry the monitor builds for itself has no cmdline provider, so psutil
is never asked what a pid this machine does not have was running.

*Liveness arrives in the order the script produced.* The `Processes` track
clips one pid's span against another's, so its slices depend on the sequence of
ticks and not only on their contents. That sequence is `_poll_order`, written
down.

The fixture is every `TracePacket` in the file, decoded through Perfetto's own
generated schema and written out as text. Decoding reads each field back
through a field number gcmon did not supply, which is the failure
[ADR-0001](../../docs/adr/0001-hand-rolled-perfetto-protobuf-encoder.md) exists
for and the one a round trip through gcmon's own constants cannot see. One
stanza per packet, so the stored form is both the thing asserted and a
per-packet diff a human can read.
"""

from __future__ import annotations

from collections.abc import Callable, Generator, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, override
from unittest.mock import patch

import pytest
from perfetto.protos.perfetto.trace.perfetto_trace_pb2 import TracePacket, TrackEvent

from gcmon.exporters.perfetto_exporter import PerfettoExporter
from gcmon.model.data import GCStatsInfo
from gcmon.monitoring.monitor import EventsMonitor
from gcmon.monitoring.monitor_loop import MonitorLoop
from gcmon.monitoring.run_policy import Runner
from gcmon.monitoring.target_process import ExternalProcess
from gcmon.monitoring.wait_policy import no_wait_policy
from gcmon.stats.streaming_stats import StreamingStats
from tests.helpers import FakeEventsReader, perfetto_packets
from tests.test_loss_replay import MS, READ_COST_NS, RING_SIZES, capture_records, ring_at

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "monitored_run_perfetto_trace.txt"

TARGET_PID = 33328
CHILD_PID = 33512

# 600 ms is the period that puts both paths in the fixture. The target collects
# gen 0 twelve or thirteen times in one, against a ring of eleven slots, so
# almost every tick draws records *and* a loss span; gen 1 and gen 2 are
# outrun comfortably, so the same run carries generations that lose nothing.
TICK_INTERVAL_NS = 600 * MS

# How long after the tick instant each polled pid's read begins. The loop polls
# the target first and the children after it, and a real run's reads are spread
# across the tick rather than simultaneous; a slot per pid says so, and gives
# the two pids' spans distinguishable edges.
READ_SLOT_NS = 1 * MS

# The capture runs 11 s. Nine ticks cover the first five of them: long enough
# for every generation to collect and for gen 0 to go blind repeatedly, short
# enough that the fixture stays a file somebody can scroll.
TICKS = 9

# The child's collections, shifted this far later than the target's. Not a
# multiple of the tick, so the two pids' rings are never in step.
CHILD_SKEW_NS = 137 * MS

# Ticks the child is missing from the listing. It is polled on 0-3, gone on 4,
# and back on 5-8: the departure is what makes the loop prune, and the return is
# where the prune becomes visible, since a monitor that kept the child's cursors
# would have nothing new to say about the slots it had already read.
#
# One tick and not more. Gen 0 turns its whole ring over in a single 600 ms
# period, so a longer absence leaves nothing in the child's rings that it had
# read before it left, and the re-export the prune causes shrinks to gen 2's
# one collection.
CHILD_GONE = frozenset({4})

# The default. Buffering changes nothing about order or content, but a run whose
# flush points moved would be a different run, so the number is written down.
FLUSH_THRESHOLD = 1000

# Reaches every packet as `trusted_packet_sequence_id`. Left to the encoder it
# is `id(self)` masked, which is an address, so it would put this process's
# memory layout in the fixture.
SEQUENCE_ID = 1


def _tree(tick: int) -> list[int]:
    """What `get_child_pids` answers on *tick*."""
    return [] if tick in CHILD_GONE else [CHILD_PID]


def _poll_order() -> list[list[int]]:
    """The pids the loop polls each tick, in the order it polls them.

    The loop's own expression, `[self._monitor.pid, *(child_pids or [])]`,
    written out here so the script and the run can be checked against each
    other -- `one_read` asserts pid by pid that they agree.
    """
    return [[TARGET_PID, *_tree(tick)] for tick in range(TICKS)]


def _script() -> tuple[list[int], list[tuple[int, int]]]:
    """The clock to hand out, and the reads to answer, for the whole run.

    Returns the `time.monotonic_ns` values in the order they will be asked for,
    and one `(pid, ts_read_start)` per `get_gc_stats` call. The run opens with
    the loop's seeding read, served the first tick's instant so position zero
    is where the first tick starts. One tick is then: the loop's stamping read
    for the tick instant, then two reads per polled pid, one either side of its
    `get_gc_stats`, then the loop's pacing read.

    The pacing read (ADR-0019) lands one slot past the last pid's, which is
    where a real one falls: after every poll, before the wait. It stamps
    nothing, so its value reaches no event -- which is why adding it left the
    fixture unmoved.
    """
    clock: list[int] = []
    reads: list[tuple[int, int]] = []
    for tick, pids in enumerate(_poll_order()):
        instant = tick * TICK_INTERVAL_NS
        clock.append(instant)
        for slot, pid in enumerate(pids, start=1):
            ts_read_start = instant + slot * READ_SLOT_NS
            clock.append(ts_read_start)
            clock.append(ts_read_start + READ_COST_NS)
            reads.append((pid, ts_read_start))
        clock.append(instant + (len(pids) + 1) * READ_SLOT_NS)
    return [clock[0], *clock], reads


class _ScriptedClock:
    """`time.monotonic_ns`, spelled out in advance.

    Indexed rather than iterated, so overrunning raises `IndexError` naming the
    position and `spent` can be checked against the length. A change to the
    reads per tick must fail here or in `test_the_clock_was_spent_exactly`,
    never read the next tick's instants and produce a plausible trace.
    """

    def __init__(self, instants: Sequence[int]) -> None:
        self._instants = instants
        self.spent = 0

    def __call__(self) -> int:
        instant = self._instants[self.spent]
        self.spent += 1
        return instant


class FixedRunner(Runner):
    """Exactly *ticks* ticks, and no clock behind them.

    `DurationRunner` reads `time.monotonic` to decide when to stop, which would
    put this machine's speed into the length of the run.
    """

    def __init__(self, ticks: int) -> None:
        self._ticks = ticks

    @override
    def run(self, stop: Callable[[], bool]) -> Generator[None, Any]:
        for _ in range(self._ticks):
            if stop():
                return
            yield


def _shifted(records: Mapping[int, list[GCStatsInfo]], skew_ns: int) -> dict[int, list[GCStatsInfo]]:
    """The capture again, every collection *skew_ns* later.

    A second process rather than a second capture: the child ran the same work
    the target did, a fraction of a second behind it. `duration` is cumulative
    pause and carries no instant, so it comes over untouched.
    """
    return {
        gen: [
            GCStatsInfo(
                gen=record.gen,
                iid=record.iid,
                ts_start=record.ts_start + skew_ns,
                ts_stop=record.ts_stop + skew_ns,
                heap_size=record.heap_size,
                collections=record.collections,
                collected=record.collected,
                uncollectable=record.uncollectable,
                candidates=record.candidates,
                duration=record.duration,
            )
            for record in gen_records
        ]
        for gen, gen_records in records.items()
    }


@dataclass(frozen=True)
class MonitoredRun:
    """One run of the loop, and what it is fair to ask about it afterwards."""

    trace: bytes
    clock_spent: int
    clock_scripted: int
    reads: list[tuple[int, int]]

    def packets(self) -> list[TracePacket]:
        return perfetto_packets(self.trace)

    def text(self) -> str:
        """The whole trace as text, one stanza per packet.

        The stanzas are unnumbered: a packet inserted anywhere renumbers
        every packet after it, and the change disappears into the
        renumbering.
        """
        return "".join(f"--- packet ---\n{packet}" for packet in self.packets())

    def row_pid_by_track(self) -> dict[int, int]:
        """The row pid each track belongs to, by walking `parent_uuid` up to
        the one descriptor that carries a pid.

        Only a `ProcessDescriptor` carries one: everything an interpreter
        owns is a plain custom track under that process's interpreter list
        (ADR-0027), so the walk is what attributes it.

        It is the pid gcmon writes for the row, one per process rather than
        one per operating-system pid (ADR-0011). The operating system's
        reaches the trace on the spans.
        """
        pids: dict[int, int] = {}
        parents: dict[int, int] = {}
        for packet in self.packets():
            if not packet.HasField("track_descriptor"):
                continue
            descriptor = packet.track_descriptor
            if descriptor.HasField("process"):
                pids[descriptor.uuid] = descriptor.process.pid
            elif descriptor.parent_uuid:
                parents[descriptor.uuid] = descriptor.parent_uuid
        by_track: dict[int, int] = dict(pids)
        for uuid in parents:
            walk = uuid
            while walk in parents:
                walk = parents[walk]
            if walk in pids:
                by_track[uuid] = pids[walk]
        return by_track

    def row_pids_on(self, pid: int) -> set[int]:
        """The row pids of every process the operating system gave *pid* to.

        Read off the process descriptors, whose name carries the operating
        system's pid and the epoch and is the only place either of them
        appears on a descriptor (ADR-0011).
        """
        prefix = f"Process {pid}"
        return {
            packet.track_descriptor.process.pid
            for packet in self.packets()
            if packet.HasField("track_descriptor")
            and packet.track_descriptor.HasField("process")
            and packet.track_descriptor.name in (prefix, f"{prefix}#2")
        }

    def slice_names(self) -> list[str]:
        return [
            packet.track_event.name
            for packet in self.packets()
            if packet.HasField("track_event") and packet.track_event.name
        ]

    def track_uuid(self, name: str) -> int:
        uuids = [
            packet.track_descriptor.uuid
            for packet in self.packets()
            if packet.HasField("track_descriptor") and packet.track_descriptor.name == name
        ]
        assert len(uuids) == 1, f"expected one {name!r} track, found {len(uuids)}"
        return int(uuids[0])

    def begins_on(self, uuid: int) -> list[str]:
        return [
            packet.track_event.name
            for packet in self.packets()
            if packet.HasField("track_event")
            and packet.track_event.track_uuid == uuid
            and packet.track_event.type == TrackEvent.Type.TYPE_SLICE_BEGIN
        ]


def run_monitored(output: Path) -> MonitoredRun:
    """Drive the real loop over the capture and return the trace."""
    truth = {
        TARGET_PID: capture_records(),
        CHILD_PID: _shifted(capture_records(), CHILD_SKEW_NS),
    }
    clock_instants, reads = _script()
    clock = _ScriptedClock(clock_instants)
    listings: Iterator[list[int]] = iter([_tree(tick) for tick in range(TICKS)])
    pending: Iterator[tuple[int, int]] = iter(reads)

    def one_listing(pid: int, recursive: bool = True) -> list[int]:
        assert pid == TARGET_PID, f"the tree was listed for {pid}, not the target"
        return next(listings)

    def one_read(pid: int) -> list[GCStatsInfo]:
        """The ring *pid* would have held when this read began.

        Asserting the pid rather than looking it up: the order the loop polls
        in is part of what this file pins, and a run that polled the child
        first would otherwise just get the child's ring and say nothing.
        """
        expected, ts_read_start = next(pending)
        assert pid == expected, f"expected a read of {expected}, got {pid}"
        records = truth[pid]
        return [slot for gen in sorted(records) for slot in ring_at(records[gen], gen, RING_SIZES[gen], ts_read_start)]

    # Built as a `PerfettoExporter` and not as a bare `ProtobufEventEncoder`:
    # `add_process_liveness` is overridden here and nowhere else, so an encoder
    # driven directly would drop every observation on the base class's no-op
    # and finish with an empty `Processes` track.
    exporter = PerfettoExporter(
        output_path=output,
        flush_threshold=FLUSH_THRESHOLD,
        sequence_id=SEQUENCE_ID,
    )
    monitor = EventsMonitor(
        ExternalProcess(pid=TARGET_PID),
        exporter,
        StreamingStats(),
        reader=FakeEventsReader(one_read),
        wait_policy_factory=no_wait_policy,
    )
    # A 1 ms rate, the point where the loop's spin-guard takes over, so the
    # between-tick wait is that guard and nothing more (ADR-0019), and no
    # `rss_sampler`,
    # which would read this machine's memory once a second. `NoWaitPolicy` per
    # pid rather than `StartupTimeoutPolicy`, whose verdict on a failed poll is
    # a `time.monotonic` reading in seconds -- a clock this file does not own.
    # The policy factory goes to the monitor, which owns per-pid lifetime.
    loop = MonitorLoop(monitor, FixedRunner(TICKS), rate=0.001)

    with (
        patch("gcmon.monitoring.monitor.get_child_pids", side_effect=one_listing),
        # On the `time` module itself, not on either importer's namespace:
        # `monitor_loop` and `monitor` both reach it through `import time`, so
        # this one patch is what makes the tick instant and the read instants
        # come off the same scripted sequence.
        patch("time.monotonic_ns", clock),
    ):
        loop.run()

    monitor.stop()

    return MonitoredRun(
        trace=output.read_bytes(),
        clock_spent=clock.spent,
        clock_scripted=len(clock_instants),
        reads=reads,
    )


@pytest.fixture(scope="module")
def run(tmp_path_factory: pytest.TempPathFactory) -> MonitoredRun:
    return run_monitored(tmp_path_factory.mktemp("trace") / "gcmon.pftrace")


class TestTheScriptIsWorthPinning:
    """A guard over a run that exercised nothing would stay green forever.
    These say the run below is the one the module docstring describes, so a
    fixture regenerated off a weakened script fails here first."""

    def test_more_than_one_pid_is_polled(self, run: MonitoredRun) -> None:
        assert {pid for pid, _ts in run.reads} == {TARGET_PID, CHILD_PID}

    def test_a_child_leaves_the_tree(self) -> None:
        """The prune path. Without a tick the child is missing from, the
        loop's `retain` and the policy deletion beside it never run."""
        polled = [set(pids) for pids in _poll_order()]

        assert any(CHILD_PID not in tick for tick in polled), "no tick prunes"
        assert polled[-1] == {TARGET_PID, CHILD_PID}, "the child never comes back"

    def test_every_process_is_described_under_a_row_pid_of_its_own(self, run: MonitoredRun) -> None:
        """Three processes on two pids, so a run written under the operating
        system's pids would describe two rows and hide one process."""
        row_pids = set(run.row_pid_by_track().values())

        assert len(row_pids) == 3
        assert not row_pids & {TARGET_PID, CHILD_PID}

    def test_both_pids_reach_the_trace(self, run: MonitoredRun) -> None:
        """The operating system's pid, which no descriptor carries any more:
        it is on the span each process draws."""
        annotated = {
            annotation.int_value
            for packet in run.packets()
            if packet.HasField("track_event")
            for annotation in packet.track_event.debug_annotations
            if annotation.name == "pid"
        }

        assert annotated == {TARGET_PID, CHILD_PID}

    def test_the_run_loses_records_as_well_as_drawing_them(self, run: MonitoredRun) -> None:
        """A poll period the ring always survives would pin the export path
        and leave ADR-0015's loss arithmetic out of the fixture entirely."""
        names = run.slice_names()

        assert any(name.startswith("GC Pause(") for name in names)
        assert any(name.startswith("GC Loss(") for name in names)

    def test_both_pids_get_a_span_on_the_processes_track(self, run: MonitoredRun) -> None:
        """The minimap, and the reason this leg is the one worth pinning.

        Liveness reaches the trace only through `add_process_liveness`, and
        only the Perfetto exporter overrides it. A run whose spans went missing
        here would still write every pause and every loss slice.
        """
        drawn = run.begins_on(run.track_uuid("Processes"))

        assert sorted(drawn) == sorted([f"Process {TARGET_PID}", f"Process {CHILD_PID}", f"Process {CHILD_PID}#2"])

    def test_the_clock_was_spent_exactly(self, run: MonitoredRun) -> None:
        """One read to seed the grid, then per tick one to stamp it, two per
        polled pid, one to pace the loop, nothing left over. A different count
        per tick moves every timestamp downstream, and this says so in one line
        instead of across the whole fixture diff."""
        assert run.clock_spent == run.clock_scripted


class TestTheTracesAreIdentical:
    def test_the_packets_match_the_fixture(self, run: MonitoredRun) -> None:
        """The guard. See the module docstring before regenerating."""
        expected = FIXTURE.read_text(encoding="utf-8")

        # Line by line first: the stored form is one field per line under a
        # header naming the packet, so this is the assertion that prints a diff
        # of what moved. The whole-text comparison after it covers the trailing
        # newline, which `splitlines` throws away.
        assert run.text().splitlines() == expected.splitlines()
        assert run.text() == expected

    def test_a_second_run_produces_the_same_bytes(self, run: MonitoredRun, tmp_path: Path) -> None:
        """No machine clock and no machine address anywhere, which is what
        separates a fixture from a record of one afternoon's timings."""
        again = run_monitored(tmp_path / "again.pftrace")

        assert again.trace == run.trace


class TestTheChildLeavingIsVisible:
    """What the departure costs, in the trace rather than in a state dict.

    An empty `_pids` after a prune proves the prune ran, not that it was right.
    Here is the observable consequence: the child's collections come back on
    two rows rather than one, because the pid that left the tree is a
    different process from the one that returned (ADR-0017). Every collection
    is still drawn exactly once: the returning pid loses its cursor and
    re-reads slots gcmon had already exported, and those are dropped rather
    than drawn again under a process that did not produce them (ADR-0025).
    """

    def _pauses(self, run: MonitoredRun, pid: int) -> list[tuple[int, int]]:
        """`(generation, collections)` per GC Pause slice drawn for *pid*,
        across every process that held it."""
        by_track = run.row_pid_by_track()
        row_pids = run.row_pids_on(pid)
        drawn: list[tuple[int, int]] = []
        for packet in run.packets():
            if not packet.HasField("track_event"):
                continue
            event = packet.track_event
            if event.type != TrackEvent.Type.TYPE_SLICE_BEGIN:
                continue
            if not event.name.startswith("GC Pause("):
                continue
            if by_track.get(event.track_uuid) not in row_pids:
                continue
            annotations = {ann.name: ann.int_value for ann in event.debug_annotations}
            drawn.append((annotations["generation"], annotations["collections"]))
        return drawn

    def test_the_child_draws_on_both_of_its_rows(self, run: MonitoredRun) -> None:
        """The departure, read off the trace. One row would mean no prune."""
        by_track = run.row_pid_by_track()
        row_pids = run.row_pids_on(CHILD_PID)
        rows = {
            uuid
            for packet in run.packets()
            if packet.HasField("track_event")
            and packet.track_event.type == TrackEvent.Type.TYPE_SLICE_BEGIN
            and packet.track_event.name.startswith("GC Pause(")
            and by_track.get(uuid := packet.track_event.track_uuid) in row_pids
        }

        assert len(rows) == 2, "the child's collections should split across the two processes that held its pid"

    def test_the_returning_child_draws_no_collection_twice(self, run: MonitoredRun) -> None:
        """The re-read is real -- the cursor went with the departure -- and
        every slot it hands back a second time is dropped."""
        drawn = self._pauses(run, CHILD_PID)

        assert len(drawn) == len(set(drawn)), "a re-read slot was drawn a second time"

    def test_the_target_never_draws_a_collection_twice(self, run: MonitoredRun) -> None:
        """The control. The target is in the tree throughout, so nothing
        prunes it, and a duplicate on its rows would mean the cursor is being
        dropped by something other than the departure above."""
        drawn = self._pauses(run, TARGET_PID)

        assert len(drawn) == len(set(drawn))


def _regenerate() -> None:
    """Rewrite the fixture from a fresh run. See the module docstring."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        produced = run_monitored(Path(tmp) / "gcmon.pftrace")

    text = produced.text()
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {FIXTURE} ({len(text)} chars, {len(produced.packets())} packets)")


if __name__ == "__main__":
    _regenerate()
