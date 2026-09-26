"""The GC phases `--stats` measures, under the keys its tables use."""

from typing import Final

from ..model.phases import (
    PAUSE_ROW,
    ClearWeakrefsData,
    DeduceUnreachableData,
    DeleteGarbageData,
    FinalizeGarbageData,
    HandleResurrectedData,
    HandleWeakrefsData,
    IncrementalData,
    MarkAliveData,
    PhaseRow,
    sub_phase_rows,
)
from ..model.protocol import TGCStatsInfo

# The key a caller reaches the pause metric by. Two modules single it
# out, one for the row it labels and one for the percentiles it owns.
PAUSE_KEY: Final = "pause"

METRICS: Final[dict[str, type[PhaseRow]]] = {
    PAUSE_KEY: PAUSE_ROW,
    "mark_alive": MarkAliveData,
    "fill_increment": IncrementalData,
    "deduce_unreachable": DeduceUnreachableData,
    "handle_weakrefs": HandleWeakrefsData,
    "finalize_garbage": FinalizeGarbageData,
    "handle_resurrected": HandleResurrectedData,
    "clear_weakrefs": ClearWeakrefsData,
    "delete_garbage": DeleteGarbageData,
}


# Each row's key, for the rows `sub_phase_rows` hands back.
_KEYS: Final[dict[type[PhaseRow], str]] = {row: key for key, row in METRICS.items()}


def phase_bounds(row: type[PhaseRow], item: object) -> tuple[int, int]:
    """Where *row*'s phase ran in *item*, or `(0, 0)` when *item* does not
    carry it."""
    return row.bounds(item) if row.check(item) else (0, 0)


def phase_spans(item: TGCStatsInfo) -> list[tuple[str, int, int]]:
    """The key, start and stop of the pause and of every sub-phase *item*
    carries."""
    spans = [(PAUSE_KEY, *phase_bounds(PAUSE_ROW, item))]
    spans.extend((_KEYS[row], *row.bounds(item)) for row in sub_phase_rows(item))
    return spans
