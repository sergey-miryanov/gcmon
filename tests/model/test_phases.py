from typing import get_type_hints

import msgspec
import pytest

from gcmon.model.data import GCStatsInfo
from gcmon.model.names import TS_START, TS_STOP
from gcmon.model.phases import PAUSE_ROW, SUB_PHASE_ROWS, sub_phase_rows
from tests.helpers import create_mock_incremental_item, create_mock_stats_item

_OPTIONAL_FIELDS = [field.name for field in msgspec.structs.fields(GCStatsInfo) if field.default is None]

_RECORD_FIELDS = GCStatsInfo.__struct_fields__

_INFO_FIELDS = {field for row in SUB_PHASE_ROWS for field in get_type_hints(row.Info)}


@pytest.mark.parametrize("missing", _OPTIONAL_FIELDS)
def test_sub_phase_rows_cache_tells_apart_every_optional_field(missing: str) -> None:
    """A msgspec record's rows are cached on a subset of its optional fields.
    A record lacking any one of them still gets the rows its checks accept,
    rather than those of a fuller record cached before it."""
    full = create_mock_incremental_item(gen=1)
    assert sub_phase_rows(full) == tuple(row for row in SUB_PHASE_ROWS if row.check(full))

    item = create_mock_incremental_item(gen=1, **{missing: None})

    assert sub_phase_rows(item) == tuple(row for row in SUB_PHASE_ROWS if row.check(item))


def test_every_sub_phase_timestamp_is_read_by_a_row() -> None:
    """The pause reads `ts_start` and `ts_stop`; every other timestamp
    bounds a sub-phase."""
    timestamps = {field for field in _RECORD_FIELDS if field.startswith("ts_")} - {TS_START, TS_STOP}

    assert timestamps <= _INFO_FIELDS


def test_every_field_no_row_reads_annotates_the_pause() -> None:
    pause_args = PAUSE_ROW.args(0, create_mock_stats_item())
    rest = {field for field in _RECORD_FIELDS if not field.startswith("ts_")} - _INFO_FIELDS

    assert rest <= set(pause_args)


def test_a_pause_emits_its_counters_in_the_order_they_draw() -> None:
    """The `GC Metrics` group ranks its counters by `counter_metrics`, and
    `counters` is written out by hand beside it."""
    counters = PAUSE_ROW.counters(0, create_mock_stats_item(uncollectable=2))

    assert [metric for metric, _, _ in counters if metric in PAUSE_ROW.counter_metrics] == list(
        PAUSE_ROW.counter_metrics
    )
