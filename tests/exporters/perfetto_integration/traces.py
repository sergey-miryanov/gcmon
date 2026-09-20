"""The traces these tests drive the trace processor against.

Each ``_write_*`` builds one trace through the real exporter and returns its
path; the fixtures in `conftest.py` beside this open them. The constants are
the figures a trace is built from, which the assertions quote back.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

from gcmon.exporters import PerfettoExporter
from gcmon.exporters.perfetto_format import _interpreter_group_name
from gcmon.exporters.perfetto_process_lifetime import process_track_name
from gcmon.exporters.trace_converter import counter_display_name
from gcmon.model.names import (
    CANDIDATES,
    COLLECTED,
    COLLECTIONS,
    DURATION,
    GENERATION,
    HEAP_SIZE,
    IID,
    UNCOLLECTABLE,
    gc_pause_slice_name,
)
from gcmon.model.process import Process
from tests.conftest import DEFAULT_PID
from tests.data_helpers import create_instant_msg
from tests.helpers import (
    create_mock_incremental_item,
    create_mock_loss_item,
    create_mock_stats_item,
    proc,
)

_PAUSE_NAME: str = gc_pause_slice_name(0)

_INSTANT_NAME: str = "GC monitor started"

_SECOND_PID: int = 67890

_THIRD_PID: int = 54321

_DEFAULT_ROW_NAME: str = process_track_name(proc(DEFAULT_PID))

_SECOND_ROW_NAME: str = process_track_name(proc(_SECOND_PID))

_THIRD_ROW_NAME: str = process_track_name(proc(_THIRD_PID))

_GEN: int = 0

_IID: int = 0

_COLLECTIONS: int = 5

_HEAP_SIZE: int = 1000

_COLLECTED: int = 10

_UNCOLLECTABLE: int = 2

_CANDIDATES: int = 3

_TS_START: int = 1_500_000_000

_TS_STOP: int = 1_500_005_000

_EXPECTED_PAUSE_ARGS: dict[str, int] = {
    GENERATION: _GEN,
    IID: _IID,
    COLLECTIONS: _COLLECTIONS,
    HEAP_SIZE: _HEAP_SIZE,
    COLLECTED: _COLLECTED,
    UNCOLLECTABLE: _UNCOLLECTABLE,
    CANDIDATES: _CANDIDATES,
}

_EXPECTED_COUNTER_NAMES: frozenset[str] = frozenset(
    {
        counter_display_name(0, COLLECTED),
        counter_display_name(0, UNCOLLECTABLE),
        counter_display_name(0, CANDIDATES),
        counter_display_name(0, DURATION),
        counter_display_name(1, COLLECTED),
        counter_display_name(1, UNCOLLECTABLE),
        counter_display_name(1, CANDIDATES),
        counter_display_name(1, DURATION),
        # Three rows, one name. Each interpreter draws its own `heap_size`
        # inside its own group, and the group is what carries the iid, so a
        # set of names holds one entry (ADR-0027).
        HEAP_SIZE,
    }
)

_ARG_PREFIX: str = "debug"


def flat_key(arg: str) -> str:
    """The key the `args` table files a debug annotation under."""
    return f"{_ARG_PREFIX}.{arg}"


_FAKE_CMDLINE: tuple[str, ...] = ("python3", "-m", "fake_target")

_FAKE_CMDLINE_JOINED: str = " ".join(_FAKE_CMDLINE)


def _process_filter(pid: int) -> str:
    """SQL fragment to scope a query to a single ``pid``.

    Every track gcmon writes belongs to a process rather than to a thread
    (ADR-0027), so one join through ``process_track`` reaches them all: the
    process's own row, and the rows nested under its interpreter list, which
    the trace processor gives the same ``upid``.

    Scoped on the name, not on ``process.pid``: that column holds the pid
    gcmon writes for the row rather than the operating system's (ADR-0028).
    Every fixture using this holds one process per pid, so the unsuffixed
    name is the whole of it; a run that handed a pid on names the successors
    ``Process <pid>#2`` and up.
    """
    return (
        "JOIN process_track pt ON s.track_id = pt.id JOIN process p ON pt.upid = p.upid "
        f"WHERE p.name = '{process_track_name(proc(pid))}'"
    )


def _process_row_filter(pid: int) -> str:
    """SQL fragment to scope a query to a process's *own* row.

    :func:`_process_filter` reaches every row under the process's ``upid``,
    the ones inside its interpreter list included. This one stops at the
    process track itself, which carries the ``Lifetime`` bar, the marks
    and the RSS (ADR-0024).
    """
    return f"JOIN process_track pt ON s.track_id = pt.id WHERE pt.name = '{process_track_name(proc(pid))}'"


def _on_interpreter(iid: int) -> str:
    """SQL fragment narrowing a :func:`_process_filter` query to one
    interpreter.

    Every interpreter's pauses are drawn on a row named ``GC Pauses``, so
    what names the interpreter is the group that row parents to (ADR-0027).
    """
    return (
        f"AND EXISTS (SELECT 1 FROM track ig WHERE ig.id = pt.parent_id AND ig.name = '{_interpreter_group_name(iid)}')"
    )


def _process_filter_instant(pid: int) -> str:
    """SQL fragment to scope an instant-event query to a single ``pid``.

    :func:`_process_filter` narrowed to the zero-width slices, which is what
    an instant event (e.g. ``GC monitor started``) reads back as.
    """
    return (
        f"JOIN process_track pt ON s.track_id = pt.id "
        f"JOIN process p ON pt.upid = p.upid "
        f"WHERE p.name = '{process_track_name(proc(pid))}' AND s.dur = 0"
    )


def _write_trace(tmp: Path, cmdline: tuple[str, ...] | None = None) -> Path:
    path = tmp / "trace.pb"
    exporter = PerfettoExporter(output_path=path, flush_threshold=1000)
    exporter.add_process_cmdline(proc(DEFAULT_PID), cmdline)
    exporter.add_process_cmdline(proc(_SECOND_PID), cmdline)
    exporter.add_instant_event(
        proc(DEFAULT_PID),
        create_instant_msg(name=_INSTANT_NAME, ts=_TS_START - 1_000_000),
    )
    exporter.add_event(
        proc(DEFAULT_PID),
        create_mock_stats_item(
            gen=_GEN,
            iid=_IID,
            collections=_COLLECTIONS,
            collected=_COLLECTED,
            uncollectable=_UNCOLLECTABLE,
            candidates=_CANDIDATES,
            heap_size=_HEAP_SIZE,
        ),
    )
    exporter.add_event(proc(DEFAULT_PID), create_mock_incremental_item(gen=1, iid=1))
    exporter.add_event(
        proc(DEFAULT_PID),
        create_mock_stats_item(
            gen=_GEN,
            iid=2,
            collections=_COLLECTIONS,
            collected=_COLLECTED,
            uncollectable=_UNCOLLECTABLE,
            candidates=_CANDIDATES,
            heap_size=_HEAP_SIZE,
        ),
    )
    exporter.add_instant_event(
        proc(_SECOND_PID),
        create_instant_msg(name=_INSTANT_NAME, ts=_TS_START - 2_000_000),
    )
    exporter.add_event(
        proc(_SECOND_PID),
        create_mock_stats_item(
            gen=_GEN,
            iid=0,
            collections=_COLLECTIONS,
            collected=_COLLECTED,
            uncollectable=_UNCOLLECTABLE,
            candidates=_CANDIDATES,
            heap_size=_HEAP_SIZE,
        ),
    )
    exporter.close()
    return path


def _write_trace_no_instant(tmp: Path, cmdline: tuple[str, ...] | None) -> Path:
    path = tmp / "trace.pb"
    exporter = PerfettoExporter(output_path=path, flush_threshold=1000)
    exporter.add_process_cmdline(proc(DEFAULT_PID), cmdline)
    exporter.add_process_cmdline(proc(_SECOND_PID), cmdline)
    exporter.add_event(
        proc(DEFAULT_PID),
        create_mock_stats_item(
            gen=_GEN,
            iid=_IID,
            collections=_COLLECTIONS,
            collected=_COLLECTED,
            uncollectable=_UNCOLLECTABLE,
            candidates=_CANDIDATES,
            heap_size=_HEAP_SIZE,
        ),
    )
    exporter.add_event(
        proc(_SECOND_PID),
        create_mock_stats_item(
            gen=_GEN,
            iid=0,
            collections=_COLLECTIONS,
            collected=_COLLECTED,
            uncollectable=_UNCOLLECTABLE,
            candidates=_CANDIDATES,
            heap_size=_HEAP_SIZE,
        ),
    )
    exporter.close()
    return path


_CROSS_A_START: int = 100_000_000

_CROSS_B_START: int = 200_000_000

_CROSS_A_STOP: int = 400_000_000

_CROSS_B_STOP: int = 600_000_000


def _write_crossing_trace(tmp: Path) -> Path:
    """Write a Perfetto trace whose two pids have crossing spans."""
    path = tmp / "crossing.pb"
    exporter = PerfettoExporter(output_path=path, flush_threshold=1000)
    for pid, ts in (
        (DEFAULT_PID, _CROSS_A_START),
        (_SECOND_PID, _CROSS_B_START),
        (DEFAULT_PID, _CROSS_A_STOP),
        (_SECOND_PID, _CROSS_B_STOP),
    ):
        exporter.add_instant_event(proc(pid), create_instant_msg(name=_INSTANT_NAME, ts=ts))
    exporter.close()
    return path


_ZERO_INSTANT_TS: int = 100_000_000

_ZERO_CROSSED_START: int = 300_000_000

_ZERO_CROSSED_STOP: int = 800_000_000

_ZERO_CROSSER_START: int = 300_000_001

_ZERO_CROSSER_STOP: int = 900_000_000


def _write_zero_duration_trace(tmp: Path) -> Path:
    """Write a Perfetto trace holding a pid observed at a single instant,
    whose ``Processes`` slice is zero-length, beside two pids starting one
    nanosecond apart."""
    path = tmp / "zero.pb"
    exporter = PerfettoExporter(output_path=path, flush_threshold=1000)
    for pid, ts in (
        (_THIRD_PID, _ZERO_INSTANT_TS),
        (DEFAULT_PID, _ZERO_CROSSED_START),
        (_SECOND_PID, _ZERO_CROSSER_START),
        (DEFAULT_PID, _ZERO_CROSSED_STOP),
        (_SECOND_PID, _ZERO_CROSSER_STOP),
    ):
        exporter.add_instant_event(proc(pid), create_instant_msg(name=_INSTANT_NAME, ts=ts))
    exporter.close()
    return path


_MARK_SPAN_START: int = 700_000_000

_MARK_GC_START: int = 710_000_000

_MARK_GC_STOP: int = 715_000_000

_MARK_TS: int = 750_000_000

_MARK_SPAN_STOP: int = 800_000_000


def _write_nested_mark_trace(tmp: Path) -> Path:
    path = tmp / "nested_mark.pb"
    exporter = PerfettoExporter(output_path=path, flush_threshold=1000)
    exporter.add_process_liveness({proc(DEFAULT_PID)}, _MARK_SPAN_START)
    exporter.add_event(
        proc(DEFAULT_PID),
        create_mock_stats_item(gen=_GEN, iid=_IID, ts_start=_MARK_GC_START, ts_stop=_MARK_GC_STOP),
    )
    exporter.add_instant_event(proc(DEFAULT_PID), create_instant_msg(name=_INSTANT_NAME, ts=_MARK_TS))
    exporter.add_process_liveness({proc(DEFAULT_PID)}, _MARK_SPAN_STOP)
    exporter.close()
    return path


_LIVE_TICKS: tuple[int, ...] = (300_000_000, 400_000_000, 500_000_000)

_LIVE_GC_START: int = 100_000_000

_LIVE_GC_STOP: int = 200_000_000

_LIVE_BUSY_CMDLINE: tuple[str, ...] = ("python3", "-m", "busy_target")

_LIVE_QUIET_CMDLINE: tuple[str, ...] = ("python3", "-m", "quiet_target")


def _write_liveness_trace(tmp: Path) -> Path:
    """Write a Perfetto trace where one pid has both events and liveness
    and another has liveness only."""
    path = tmp / "liveness.pb"
    exporter = PerfettoExporter(output_path=path, flush_threshold=1000)
    exporter.add_process_cmdline(proc(DEFAULT_PID), _LIVE_BUSY_CMDLINE)
    exporter.add_process_cmdline(proc(_SECOND_PID), _LIVE_QUIET_CMDLINE)
    exporter.add_event(
        proc(DEFAULT_PID),
        create_mock_stats_item(
            gen=_GEN,
            iid=_IID,
            ts_start=_LIVE_GC_START,
            ts_stop=_LIVE_GC_STOP,
        ),
    )
    for ts in _LIVE_TICKS:
        exporter.add_process_liveness({proc(DEFAULT_PID), proc(_SECOND_PID)}, ts)
    exporter.close()
    return path


def _write_liveness_only_trace(tmp: Path) -> Path:
    """Write a Perfetto trace with no events whatsoever: every pid
    answered every poll and none of them ever collected."""
    path = tmp / "liveness_only.pb"
    exporter = PerfettoExporter(output_path=path, flush_threshold=1000)
    for ts in _LIVE_TICKS:
        exporter.add_process_liveness({proc(DEFAULT_PID), proc(_SECOND_PID)}, ts)
    exporter.close()
    return path


_KILL_GC_START: int = 600_000_000

_KILL_GC_STOP: int = 610_000_000

_KILL_TICK: int = 650_000_000

_KILL_LAST_GC_START: int = 700_000_000

_KILL_LAST_GC_STOP: int = 710_000_000


def _write_killed_run_trace(tmp: Path) -> Path:
    path = tmp / "killed.pb"
    exporter = PerfettoExporter(output_path=path, flush_threshold=1)
    for pid in (DEFAULT_PID, _SECOND_PID):
        exporter.add_process_cmdline(proc(pid), _FAKE_CMDLINE)
        exporter.add_event(
            proc(pid),
            create_mock_stats_item(gen=_GEN, iid=_IID, ts_start=_KILL_GC_START, ts_stop=_KILL_GC_STOP),
        )
    exporter.add_process_liveness({proc(DEFAULT_PID), proc(_SECOND_PID)}, _KILL_TICK)
    exporter.add_process_retired(proc(_SECOND_PID))
    exporter.add_event(
        proc(DEFAULT_PID),
        create_mock_stats_item(gen=_GEN, iid=_IID, ts_start=_KILL_LAST_GC_START, ts_stop=_KILL_LAST_GC_STOP),
    )
    # No close(). Everything after this point is what the kill takes.
    return path


_REUSED_PID: int = 24680

_LOSS_WINDOW_NS: int = 10_000_000

_LOST_PAUSE_NS: int = 3_316_458_100


class Epoch(NamedTuple):
    """One of the processes that held `_REUSED_PID`, and what it collected.

    `pid_epoch` is the field that tells the two apart (ADR-0025), so it is
    what the pair below is keyed on.
    """

    pid_epoch: int
    cmdline: tuple[str, ...]
    ts_start: int
    ts_stop: int
    collected: int

    @property
    def process(self) -> Process:
        return proc(_REUSED_PID, self.pid_epoch)

    @property
    def track_name(self) -> str:
        return process_track_name(self.process)


_FIRST_EPOCH = Epoch(1, ("python3", "-m", "first_target"), 100_000_000, 140_000_000, 11)

_SECOND_EPOCH = Epoch(2, ("python3", "-m", "second_target"), 300_000_000, 340_000_000, 22)


def _write_reused_pid_trace(tmp: Path) -> Path:
    """Write a Perfetto trace for a run where one pid named two
    processes."""
    path = tmp / "reused.pb"
    exporter = PerfettoExporter(output_path=path, flush_threshold=1000)
    for epoch in (_FIRST_EPOCH, _SECOND_EPOCH):
        exporter.add_process_cmdline(epoch.process, epoch.cmdline)
        exporter.add_event(
            epoch.process,
            create_mock_stats_item(
                gen=_GEN,
                iid=_IID,
                ts_start=epoch.ts_start,
                ts_stop=epoch.ts_stop,
                collected=epoch.collected,
            ),
        )
        exporter.add_loss_event(
            epoch.process,
            create_mock_loss_item(
                iid=_IID,
                gen=_GEN,
                ts_start=epoch.ts_stop,
                ts_stop=epoch.ts_stop + _LOSS_WINDOW_NS,
                lost_count=epoch.collected,
                lost_pause_ns=_LOST_PAUSE_NS,
            ),
        )
    exporter.close()
    return path


_HELD_PID: int = 31415

_HELD_EPOCHS: int = 4

_HELD_STEP: int = 100_000_000

_HELD_SPAN: int = 40_000_000


def _held_start(epoch: int) -> int:
    """Where the *epoch*-th process on ``_HELD_PID`` was first observed."""
    return epoch * _HELD_STEP


def _held_name(epoch: int) -> str:
    return process_track_name(proc(_HELD_PID)) + ("" if epoch == 1 else f"#{epoch}")


def _write_pid_held_four_times_trace(tmp: Path) -> Path:
    """Write a trace for a run where one pid named four processes, one
    after another with no overlap."""
    path = tmp / "held.pb"
    exporter = PerfettoExporter(output_path=path, flush_threshold=1000)
    for epoch in range(1, _HELD_EPOCHS + 1):
        process = proc(_HELD_PID, epoch)
        exporter.add_process_cmdline(process, ("python3", "-m", f"target{epoch}"))
        exporter.add_event(
            process,
            create_mock_stats_item(
                gen=_GEN,
                iid=_IID,
                ts_start=_held_start(epoch),
                ts_stop=_held_start(epoch) + _HELD_SPAN,
                collected=epoch,
            ),
        )
    exporter.close()
    return path


_RSS_PID_1: int = DEFAULT_PID

_RSS_PID_2: int = _SECOND_PID

_RSS_VAL_1: int = 4_194_304  # 4 MB

_RSS_VAL_2: int = 8_388_608  # 8 MB

_RSS_VAL_3: int = 2_097_152  # 2 MB

_RSS_TS_1: int = 500_000_000

_RSS_TS_2: int = 1_500_000_000

_RSS_TS_3: int = 2_500_000_000


def _write_every_row_trace(tmp: Path) -> Path:
    """One interpreter drawing every row it can: a pause with each counter
    set, a loss window after it, and the process's `rss` beside them."""
    path = tmp / "trace_every_row.pb"
    exporter = PerfettoExporter(output_path=path, flush_threshold=1000)
    pause = create_mock_incremental_item(gen=_GEN, iid=_IID, uncollectable=_UNCOLLECTABLE)
    exporter.add_event(proc(DEFAULT_PID), pause)
    exporter.add_loss_event(
        proc(DEFAULT_PID),
        create_mock_loss_item(
            iid=_IID, gen=_GEN, ts_start=pause.ts_stop + 5_000_000, ts_stop=pause.ts_stop + 15_000_000
        ),
    )
    exporter.add_rss_sample(proc(DEFAULT_PID), _RSS_VAL_1, pause.ts_start)
    exporter.close()
    return path


def _write_trace_with_rss(tmp: Path) -> Path:
    path = tmp / "trace_with_rss.pb"
    exporter: PerfettoExporter = PerfettoExporter(output_path=path, flush_threshold=1000)
    exporter.add_rss_sample(proc(_RSS_PID_1), _RSS_VAL_1, _RSS_TS_1)
    exporter.add_rss_sample(proc(_RSS_PID_2), _RSS_VAL_2, _RSS_TS_2)
    exporter.add_rss_sample(proc(_RSS_PID_1), _RSS_VAL_3, _RSS_TS_3)
    exporter.close()
    return path
