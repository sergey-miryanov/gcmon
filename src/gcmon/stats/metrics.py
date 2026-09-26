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
)

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


def phase_bounds(row: type[PhaseRow], item: object) -> tuple[int, int]:
    """Where *row*'s phase ran in *item*, or `(0, 0)` when *item* does not
    carry it."""
    return row.bounds(item) if row.check(item) else (0, 0)
