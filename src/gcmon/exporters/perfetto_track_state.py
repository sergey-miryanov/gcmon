"""Per-trace uuid allocation and bookkeeping, with no protobuf knowledge.

``PerfettoTrackState`` keeps descriptor emission idempotent across the
many convert calls a buffered export makes.

It also holds the ``Processes``-track span accumulator (ADR-0011).
"""

from collections.abc import Iterable
from typing import NamedTuple

from ..model.process import Process
from ..model.trace_event import InterpreterTrack, Track


class ProcessSpan(NamedTuple):
    """The interval one process was observed over (ADR-0011)."""

    process: Process
    start_ts: int
    end_ts: int


class PerfettoTrackState:
    def __init__(self) -> None:
        self._described_processes: set[Process] = set()
        self._tracks: set[Track] = set()
        self._counter_tracks: dict[tuple[Track, str], int] = {}
        self._counter_group_uuids: dict[Track, int] = {}
        self._interpreter_list_uuids: dict[Process, int] = {}
        self._interpreter_group_uuids: dict[tuple[Process, int], int] = {}
        self._process_uuids: dict[Process, int] = {}
        self._row_pids: dict[Process, int] = {}
        self._track_uuids: dict[Track, int] = {}
        self._process_lifetime_track_uuid: int | None = None
        self._cmdlines: dict[Process, tuple[str, ...]] = {}
        self._process_lifetime_start: dict[Process, int] = {}
        self._sampled_counts: dict[Process, int] = {}
        self._lost_counts: dict[Process, int] = {}
        self._lost_pause_ns: dict[Process, int] = {}
        self._process_ranks: dict[Process, int] = {}
        self._next_rank: int = 0
        self._process_lifetime_end: dict[Process, int] = {}
        self._process_lifetime_emitted: bool = False
        self._process_rows_drawn: set[Process] = set()
        self._root_descriptor_emitted: bool = False
        self._next_uuid: int = 1
        self._next_row_pid: int = 1

    def _alloc_uuid(self) -> int:
        uuid = self._next_uuid
        self._next_uuid += 1
        return uuid

    def has_process_descriptor(self, process: Process) -> bool:
        return process in self._described_processes

    def mark_process_descriptor(self, process: Process) -> None:
        self._described_processes.add(process)

    def has_track(self, track: Track) -> bool:
        return track in self._tracks

    def mark_track(self, track: Track) -> None:
        self._tracks.add(track)

    def get_row_pid(self, process: Process) -> int:
        """The pid *process*'s row is written under, gcmon's own.

        One per process rather than one per operating-system pid (ADR-0011).
        ``Process.pid`` stays the operating system's and reaches the trace as
        an annotation.
        """
        if process not in self._row_pids:
            self._row_pids[process] = self._next_row_pid
            self._next_row_pid += 1
        return self._row_pids[process]

    def get_process_track_uuid(self, process: Process) -> int:
        if process not in self._process_uuids:
            self._process_uuids[process] = self._alloc_uuid()
        return self._process_uuids[process]

    def get_track_uuid(self, track: Track) -> int:
        if track not in self._track_uuids:
            self._track_uuids[track] = self._alloc_uuid()
        return self._track_uuids[track]

    def get_interpreter_count(self, process: Process) -> int:
        """How many of *process*'s interpreters gcmon read a record from.

        `convert_item_to_trace_format` is the only place an
        `InterpreterTrack` is built and it takes the iid off a record, so an
        interpreter that produced nothing reaches no part of the exporter.
        """
        return sum(1 for track in self._tracks if isinstance(track, InterpreterTrack) and track.process == process)

    def record_sampled(self, process: Process) -> None:
        """Count one more record read for *process*.

        One call per record, not per event: a record becomes a pause slice,
        its sub-phase slices and its counters, and counting those would count
        phases.
        """
        self._sampled_counts[process] = self._sampled_counts.get(process, 0) + 1

    def get_sampled_count(self, process: Process) -> int:
        """How many records gcmon read for *process*."""
        return self._sampled_counts.get(process, 0)

    def record_loss(self, process: Process, lost_count: int, lost_pause_ns: int) -> None:
        """Fold one poll interval's loss into *process*'s totals.

        `EventsMonitor` reports an interval only where something went
        missing, so no call here is a no-op, though a `lost_pause_ns` of
        zero is ordinary. Every interval is one interpreter's, so the totals
        sum across a process's interpreters as well as across its polls.
        """
        self._lost_counts[process] = self._lost_counts.get(process, 0) + lost_count
        self._lost_pause_ns[process] = self._lost_pause_ns.get(process, 0) + lost_pause_ns

    def get_lost_count(self, process: Process) -> int:
        """How many of *process*'s records were overwritten before gcmon
        reached them."""
        return self._lost_counts.get(process, 0)

    def get_lost_pause_ns(self, process: Process) -> int:
        """How much GC pause sits inside the records *process* lost."""
        return self._lost_pause_ns.get(process, 0)

    def has_counter_track(self, track: Track, display_name: str) -> bool:
        return (track, display_name) in self._counter_tracks

    def get_or_create_counter_track_uuid(self, track: Track, display_name: str) -> int:
        key = (track, display_name)
        if key not in self._counter_tracks:
            self._counter_tracks[key] = self._alloc_uuid()
        return self._counter_tracks[key]

    def has_counter_group_track(self, track: Track) -> bool:
        return track in self._counter_group_uuids

    def get_or_create_counter_group_track_uuid(self, track: Track) -> int:
        if track not in self._counter_group_uuids:
            self._counter_group_uuids[track] = self._alloc_uuid()
        return self._counter_group_uuids[track]

    def has_interpreter_list_track(self, process: Process) -> bool:
        return process in self._interpreter_list_uuids

    def get_or_create_interpreter_list_track_uuid(self, process: Process) -> int:
        if process not in self._interpreter_list_uuids:
            self._interpreter_list_uuids[process] = self._alloc_uuid()
        return self._interpreter_list_uuids[process]

    def has_interpreter_group_track(self, process: Process, iid: int) -> bool:
        return (process, iid) in self._interpreter_group_uuids

    def get_or_create_interpreter_group_track_uuid(self, process: Process, iid: int) -> int:
        """The uuid of the group holding every row interpreter *iid* owns.

        Keyed on the pair rather than on a `Track`, because an
        `InterpreterTrack` and a `LossTrack` naming the same interpreter
        hang off the same group (ADR-0027).
        """
        key = (process, iid)
        if key not in self._interpreter_group_uuids:
            self._interpreter_group_uuids[key] = self._alloc_uuid()
        return self._interpreter_group_uuids[key]

    def has_process_lifetime_track(self) -> bool:
        return self._process_lifetime_track_uuid is not None

    def get_or_create_process_lifetime_track_uuid(self) -> int:
        if self._process_lifetime_track_uuid is None:
            self._process_lifetime_track_uuid = self._alloc_uuid()
        return self._process_lifetime_track_uuid

    def set_cmdline(self, process: Process, cmdline: tuple[str, ...] | None) -> None:
        """Record what *process* is running, for its descriptor and its
        ``Processes``-track span to name."""
        if cmdline is not None:
            self._cmdlines[process] = cmdline

    def get_cmdline(self, process: Process) -> tuple[str, ...] | None:
        """What *process* is running, or ``None``.

        ``None`` on every offline path: a capture carries no command line
        and `combine` creates no process (ADR-0024).
        """
        return self._cmdlines.get(process)

    def has_process_lifetime(self, process: Process) -> bool:
        return process in self._process_lifetime_start

    def update_process_lifetime(self, process: Process, ts: int) -> None:
        """Fold *ts* into the recorded span for *process*: a plain min/max
        over every event and every liveness observation, with no
        event-kind exception (ADR-0011).
        """
        start_ts = self._process_lifetime_start.get(process)
        if start_ts is None or ts < start_ts:
            self._process_lifetime_start[process] = ts
        end_ts = self._process_lifetime_end.get(process)
        if end_ts is None or ts > end_ts:
            self._process_lifetime_end[process] = ts

    def get_process_lifetime_start_ts(self, process: Process) -> int | None:
        """When *process*'s Perfetto row opens."""
        return self._process_lifetime_start.get(process)

    def get_process_lifetimes(self) -> list[ProcessSpan]:
        """Every process with a recorded span.

        The two dicts carry identical key sets, so this is every process
        ever folded in -- including one known only from liveness.
        """
        return [
            ProcessSpan(process, self._process_lifetime_start[process], end)
            for process, end in self._process_lifetime_end.items()
        ]

    def has_process_row_drawn(self, process: Process) -> bool:
        return process in self._process_rows_drawn

    def mark_process_row_drawn(self, process: Process) -> None:
        self._process_rows_drawn.add(process)

    def get_process_lifetime(self, process: Process) -> ProcessSpan | None:
        """*process*'s recorded span, or ``None`` where gcmon never observed
        it."""
        start_ts = self._process_lifetime_start.get(process)
        if start_ts is None:
            return None
        return ProcessSpan(process, start_ts, self._process_lifetime_end[process])

    def has_process_lifetime_emitted(self) -> bool:
        return self._process_lifetime_emitted

    def mark_process_lifetime_emitted(self) -> None:
        self._process_lifetime_emitted = True

    def rank_processes(self, processes: Iterable[Process]) -> None:
        """Give each of *processes* still without a rank the next ones, in
        ascending ``(start_ts, process)``.

        A rank is handed out once and never revised, because the descriptor
        carrying it is written once (ADR-0011). Sorting here is what settles
        the order among the processes described together; the counter settles
        it between one group and the next, in the order gcmon reached them. A
        process gcmon has not observed yet is left unranked rather than
        ranked from nothing.
        """
        starts = self._process_lifetime_start
        fresh = [process for process in processes if process not in self._process_ranks and process in starts]
        for process in sorted(set(fresh), key=lambda process: (starts[process], process)):
            self._process_ranks[process] = self._next_rank
            self._next_rank += 1

    def get_process_track_rank(self, process: Process) -> int | None:
        """Where *process*'s row sorts, or ``None`` where it has no rank."""
        return self._process_ranks.get(process)

    def has_root_descriptor(self) -> bool:
        return self._root_descriptor_emitted

    def mark_root_descriptor_emitted(self) -> None:
        self._root_descriptor_emitted = True
