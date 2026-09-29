from typing import get_type_hints

import msgspec
import pytest

from gcmon.model.data import GCStatsInfo
from gcmon.model.names import GEN, GENERATION, IID, TS_START, TS_STOP
from gcmon.model.phases import PAUSE_FIELDS, SUB_PHASES, Counters, PausePhase, sub_phases_and_fields
from tests.helpers import create_mock_incremental_item, create_mock_stats_item

_OPTIONAL_FIELDS = [field.name for field in msgspec.structs.fields(GCStatsInfo) if field.default is None]

_RECORD_FIELDS = GCStatsInfo.__struct_fields__

_INFO_FIELDS = {field for kind in (*SUB_PHASES, *PAUSE_FIELDS) for field in get_type_hints(kind.Info)}


def _checked(item: GCStatsInfo) -> tuple[tuple[type, ...], tuple[type, ...]]:
    return (
        tuple(phase for phase in SUB_PHASES if phase.check(item)),
        tuple(field for field in PAUSE_FIELDS if field.check(item)),
    )


@pytest.mark.parametrize("missing", _OPTIONAL_FIELDS)
def test_the_cache_tells_apart_every_optional_field(missing: str) -> None:
    """What a msgspec record carries is cached on a subset of its optional
    fields. A record lacking any one of them still gets what its checks accept,
    rather than those of a fuller record cached before it."""
    full = create_mock_incremental_item(gen=1)
    assert sub_phases_and_fields(full) == _checked(full)

    item = create_mock_incremental_item(gen=1, **{missing: None})

    assert sub_phases_and_fields(item) == _checked(item)


def test_every_sub_phase_timestamp_bounds_a_sub_phase() -> None:
    """The pause reads `ts_start` and `ts_stop`; every other timestamp
    bounds a sub-phase."""
    timestamps = {field for field in _RECORD_FIELDS if field.startswith("ts_")} - {TS_START, TS_STOP}

    assert timestamps <= _INFO_FIELDS


def test_every_field_nothing_else_reads_annotates_the_pause() -> None:
    pause_args = PausePhase.args(0, create_mock_stats_item())
    # `gen` reaches every phase as a parameter and the pause reads `iid` off
    # the record, neither through an `Info`; the pause annotates `gen` as
    # `generation`.
    rest = {field for field in _RECORD_FIELDS if not field.startswith("ts_")} - _INFO_FIELDS - {GEN, IID}

    assert rest | {GENERATION, IID} <= set(pause_args)


def test_a_pause_emits_its_counters_in_the_order_they_draw() -> None:
    """The `GC Metrics` group ranks its counters by `counter_metrics`, and
    `counters` is written out by hand beside it."""
    item = create_mock_stats_item(uncollectable=2)
    counters = Counters.counters(0, item.ts_start, item.ts_stop, item)

    assert [metric for metric, _, _, _ in counters if metric in Counters.counter_metrics] == list(
        Counters.counter_metrics
    )
