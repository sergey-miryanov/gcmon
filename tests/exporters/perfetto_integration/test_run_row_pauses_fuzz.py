"""Fuzz for the pauses on the run rows.

Every interpreter's pauses on a generation's row go on a track of its own,
which the row draws as a lane of its own. A sub-phase stays inside its pause
because the trace processor nests it under the slice open on its track, and
whether that holds across random overlaps is a question for the trace
processor, not for a wire-level test.

Marked ``fuzz``: each trial loads a trace, costing seconds rather than
milliseconds. Seeds are fixed, so a failure reproduces.
"""

import random
from collections.abc import Iterator

import pytest
from perfetto.trace_processor import TraceProcessor

from gcmon.exporters import PerfettoExporter
from gcmon.exporters.encoder import Layout
from gcmon.exporters.perfetto_run_rows import _ALL_PAUSES_GROUP_NAME, _generation_row_name
from gcmon.exporters.trace_converter import convert_item_to_trace_format
from gcmon.model.data import GCStatsInfo
from gcmon.model.names import GENERATIONS, IID, PAUSE, PID, PID_EPOCH
from gcmon.model.process import Process
from gcmon.model.trace_event import Slice
from tests.exporters.perfetto_integration.traces import _rows_and_the_one_at, _shifted, flat_key
from tests.helpers import (
    create_mock_incremental_item,
    create_mock_stats_item,
    misplaced_end_events,
    open_trace_processor,
    proc,
)

pytestmark = pytest.mark.fuzz

TRIALS = 12

# A pause is 5 ms wide and its sub-phases step by 500 us. Interpreters start
# on a 250 us grid, so equal starts and sub-phases meeting another's edges are
# common rather than rare.
_GRID_NS = 250_000

_GRID_STEPS = 20

_MAX_PAUSES = 4

# An interpreter's collections serialize (ADR-0015): its next pause starts
# this far after the last one started, past the 5 ms it lasts.
_SPACING_NS = 6_000_000

# Where a pid's second holder starts: after every pause of the first, since a
# pid passes on only once its holder has exited.
_REUSE_NS = _GRID_STEPS * _GRID_NS + _MAX_PAUSES * _SPACING_NS

type _Pauses = list[tuple[Process, GCStatsInfo]]

type _Trial = tuple[_Pauses, TraceProcessor]

type _PauseKey = tuple[int, int, int, int, int]
"""A pause's generation, pid, pid epoch, iid and start."""


def _random_pauses(rng: random.Random) -> _Pauses:
    """Pauses from one to three processes of one to three interpreters each,
    in the order a monitor reads them, the first pid sometimes passing to a
    second holder. A build reports every sub-phase or none, so a pause carries
    all or none."""
    pids = rng.sample(range(100, 110), rng.randint(1, 3))
    holders = [(proc(pid), 0) for pid in pids]
    if rng.random() < 0.5:
        holders.append((proc(pids[0], 2), _REUSE_NS))
    pauses: list[tuple[int, Process, GCStatsInfo]] = []
    for process, offset in holders:
        for iid in range(rng.randint(1, 3)):
            start = offset + rng.randrange(_GRID_STEPS) * _GRID_NS
            for n in range(rng.randint(1, _MAX_PAUSES)):
                gen = rng.randrange(3)
                if rng.random() < 2 / 3:
                    record = create_mock_incremental_item(gen=gen, iid=iid)
                else:
                    record = create_mock_stats_item(gen=gen, iid=iid)
                shift = start + n * _SPACING_NS
                pauses.append((shift, process, _shifted(record, shift)))
    return [(process, record) for _, process, record in sorted(pauses, key=lambda one: one[0])]


def _most_at_once_on_one_row(pauses: _Pauses) -> int:
    return max(
        sum(1 for _, other in pauses if other.gen == record.gen and other.ts_start <= record.ts_start < other.ts_stop)
        for _, record in pauses
    )


def _generation_rows(tp: TraceProcessor) -> dict[int, str]:
    """The tracks each generation's row under `All GC Pauses` is drawn from,
    as a list SQL's ``IN`` takes."""
    rows, group = _rows_and_the_one_at(tp, (_ALL_PAUSES_GROUP_NAME,))
    generation = {_generation_row_name(gen): gen for gen in GENERATIONS}
    return {generation[row.name]: str(row.track_ids) for row in rows if row.parent_id == group}


@pytest.fixture(scope="module", params=range(TRIALS))
def trial(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> Iterator[_Trial]:
    rng = random.Random(request.param)
    pauses = _random_pauses(rng)
    path = tmp_path_factory.mktemp("run_row_pauses") / f"fuzz{request.param}.pftrace"
    exporter = PerfettoExporter(output_path=path, flush_threshold=rng.choice([1, 7, 1000]), layout=Layout.FULL)
    for process, record in pauses:
        exporter.add_event(process, record)
    exporter.close()
    with open_trace_processor(path) as tp:
        yield pauses, tp


def test_the_seeds_draw_pauses_overlapping_on_one_row() -> None:
    """What the trials below are for. A change to the generator that stopped
    the overlaps would leave them passing while guarding nothing."""
    overlapping = [seed for seed in range(TRIALS) if _most_at_once_on_one_row(_random_pauses(random.Random(seed))) > 1]

    assert len(overlapping) >= TRIALS // 2


def test_every_pause_reads_back_on_its_generation_s_row_at_its_own_width(trial: _Trial) -> None:
    pauses, tp = trial

    drawn: list[tuple[_PauseKey, int]] = []
    for gen, track_ids in _generation_rows(tp).items():
        drawn.extend(
            ((gen, r.pid, r.pid_epoch, r.iid, r.ts), r.dur)
            for r in tp.query(
                f"SELECT s.ts, s.dur, EXTRACT_ARG(s.arg_set_id, '{flat_key(PID)}') AS pid, "
                f"EXTRACT_ARG(s.arg_set_id, '{flat_key(PID_EPOCH)}') AS pid_epoch, "
                f"EXTRACT_ARG(s.arg_set_id, '{flat_key(IID)}') AS iid "
                f"FROM slice s WHERE s.track_id IN ({track_ids}) AND s.depth = 0"
            )
        )

    assert sorted(drawn) == sorted(
        (
            (record.gen, process.pid, process.pid_epoch, record.iid, record.ts_start),
            record.ts_stop - record.ts_start,
        )
        for process, record in pauses
    )


def test_every_sub_phase_sits_inside_its_own_pause(trial: _Trial) -> None:
    pauses, tp = trial

    drawn: list[tuple[str, int, int, _PauseKey]] = []
    for gen, track_ids in _generation_rows(tp).items():
        drawn.extend(
            (r.name, r.ts, r.dur, (gen, r.pid, r.pid_epoch, r.iid, r.parent_ts))
            for r in tp.query(
                f"SELECT s.name, s.ts, s.dur, p.ts AS parent_ts, "
                f"EXTRACT_ARG(p.arg_set_id, '{flat_key(PID)}') AS pid, "
                f"EXTRACT_ARG(p.arg_set_id, '{flat_key(PID_EPOCH)}') AS pid_epoch, "
                f"EXTRACT_ARG(p.arg_set_id, '{flat_key(IID)}') AS iid "
                f"FROM slice s JOIN slice p ON s.parent_id = p.id WHERE s.track_id IN ({track_ids})"
            )
        )

    assert sorted(drawn) == sorted(
        (
            event.name,
            event.ts_start,
            event.ts_stop - event.ts_start,
            (record.gen, process.pid, process.pid_epoch, record.iid, record.ts_start),
        )
        for process, record in pauses
        for event in convert_item_to_trace_format(process, record)
        if isinstance(event, Slice) and not event.cat.startswith(PAUSE.category)
    )


def test_no_end_is_left_unpaired(trial: _Trial) -> None:
    """``misplaced_end_event`` is filed under ``data_loss``, which a check on
    ``error`` alone misses, and read by name too, so a renamed counter fails
    rather than reading as none."""
    _pauses, tp = trial

    flagged = tp.query("SELECT name FROM stats WHERE severity IN ('error', 'data_loss') AND value > 0")

    assert misplaced_end_events(tp) == 0
    assert [r.name for r in flagged] == []
