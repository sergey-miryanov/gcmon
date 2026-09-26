import msgspec
import pytest

from gcmon.model.data import GCStatsInfo
from gcmon.model.phases import SUB_PHASE_ROWS, sub_phase_rows
from tests.helpers import create_mock_incremental_item

_OPTIONAL_FIELDS = [field.name for field in msgspec.structs.fields(GCStatsInfo) if field.default is None]


@pytest.mark.parametrize("missing", _OPTIONAL_FIELDS)
def test_sub_phase_rows_cache_tells_apart_every_optional_field(missing: str) -> None:
    """A msgspec record's rows are cached on a subset of its optional fields.
    A record lacking any one of them still gets the rows its checks accept,
    rather than those of a fuller record cached before it."""
    full = create_mock_incremental_item(gen=1)
    assert sub_phase_rows(full) == tuple(row for row in SUB_PHASE_ROWS if row.check(full))

    item = create_mock_incremental_item(gen=1, **{missing: None})

    assert sub_phase_rows(item) == tuple(row for row in SUB_PHASE_ROWS if row.check(item))
