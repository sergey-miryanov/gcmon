"""A trace's records as a table, from slices built in the test (spec 0076).

The command's tests read gcmon's own traces, where every pause names its
process; these cover the slices those traces never hold.
"""

from gcmon.analysis.extract import COLUMNS, record_rows
from gcmon.analysis.trace_rows import ArgValue, SliceRow, TraceRows
from gcmon.model.names import GENERATION, IID, PAUSE, PID, PID_EPOCH
from tests.helpers import proc


def _pause(args: dict[str, ArgValue]) -> SliceRow:
    """A generation-0 pause on a row no process holds."""
    return SliceRow("Gen0", None, PAUSE.names[0][0], 1_000, 400, {GENERATION: 0, IID: 0, **args}, None)


def test_a_pause_on_a_run_row_takes_its_process_from_its_args() -> None:
    worker = proc(588)
    rows = TraceRows([], [_pause({PID: worker.pid, PID_EPOCH: worker.pid_epoch})])

    assert [dict(zip(COLUMNS, row, strict=True))[PID] for row in record_rows(rows, {})] == [worker.pid]


def test_a_pause_naming_no_process_gives_no_row() -> None:
    assert record_rows(TraceRows([], [_pause({})]), {}) == []
