"""The phase table names record fields by string, which no type checker
reads. These tests are what turns a misspelled field into a failure rather
than a phase that silently never appears.
"""

from __future__ import annotations

from gcmon.model.data import GCStatsInfo
from gcmon.model.names import GC_PHASES

_FIELDS = frozenset(GCStatsInfo.__struct_fields__)


class TestThePhaseTableNamesOnlyFieldsARecordCarries:
    def test_every_endpoint_is_a_field(self) -> None:
        named = {(phase.label, field) for phase in GC_PHASES for field in (phase.start, phase.stop)}
        assert {pair for pair in named if pair[1] not in _FIELDS} == set()

    def test_every_arg_is_a_field(self) -> None:
        named = {(phase.label, field) for phase in GC_PHASES for field in phase.args}
        assert {pair for pair in named if pair[1] not in _FIELDS} == set()


class TestEveryTimestampARecordCarriesBoundsAPhase:
    def test_every_ts_field_is_some_phase_endpoint(self) -> None:
        endpoints = {field for phase in GC_PHASES for field in (phase.start, phase.stop)}
        timestamps = {field for field in _FIELDS if field.startswith("ts_")}
        assert timestamps - endpoints == set()
