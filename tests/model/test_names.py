"""What the name table has to hold true, as opposed to what it says.

Reading a constant back and asserting it equals its own literal proves
nothing: the rename that changes the constant changes the assertion in the
same edit. So nothing here spells a name out. What is checked instead is
either a property every row of the table must have, or agreement between the
table and something written independently of it, which is the code that
converts a record.

The names themselves are pinned in
`tests/exporters/test_track_names_are_documented.py`, against the pages a
reader looks them up in.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from gcmon.analysis.jsonl_io import write_jsonl
from gcmon.exporters.trace_converter import convert_item_to_trace_format, convert_loss_to_trace_format
from gcmon.model.data import GCStatsInfo
from gcmon.model.names import (
    GC_LOSS_NAME,
    GC_PHASES,
    GEN_COUNTER_METRICS,
    GENERATIONS,
    HEAP_SIZE,
    JSONL_FIELDS,
    PAUSE,
    SLICE_ARGS,
    gc_loss_slice_name,
)
from gcmon.model.trace_event import Counter, Slice
from gcmon.support.vocabulary import ENCODING
from tests.helpers import create_mock_loss_item, create_mock_stats_item, proc


def _keys(records: Iterable[Any]) -> set[str]:
    """Every key in *records*, at any depth. The loss figures are nested one
    level down, inside `gens`."""
    out: set[str] = set()
    stack = list(records)
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            out |= set(node)
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return out


class TestEveryPhaseIsShapedLikeThePhasesBesideIt:
    """The table is read by two subsystems that never compare notes. A row
    shaped unlike its neighbours is how the `--stats` label and the slice
    drifted apart before the table existed."""

    def test_every_label_carries_the_gc_prefix(self) -> None:
        """A `--stats` row and a slice read as the same phase only because
        of it. `Fill increment` lacked it, and read as a different one."""
        assert [phase.label for phase in GC_PHASES if not phase.label.startswith("GC ")] == []

    def test_every_category_sits_under_the_gc_prefix(self) -> None:
        """A reader filters on the prefix to reach every phase at once
        (`docs/perfetto-sql.md`)."""
        assert [phase.category for phase in GC_PHASES if not phase.category.startswith("gc.")] == []

    def test_no_two_phases_share_a_label(self) -> None:
        """Two rows under one name are one row a reader cannot take apart."""
        assert len({phase.label for phase in GC_PHASES}) == len(GC_PHASES)

    def test_no_two_phases_share_a_category(self) -> None:
        assert len({phase.category for phase in GC_PHASES}) == len(GC_PHASES)


class TestTheTableAgreesWithWhatAConversionWrites:
    """Both halves are hand-written and nothing makes them agree. A name in
    the table that no conversion emits is a row the pages promise and the
    trace never draws; one a conversion emits and the table omits escapes
    every check that reads the table."""

    def _counters(self) -> set[str]:
        events = convert_item_to_trace_format(proc(1), create_mock_stats_item(uncollectable=2))
        return {e.metric for e in events if isinstance(e, Counter)}

    def test_a_pause_writes_every_counter_metric_the_table_names(self) -> None:
        assert set(GEN_COUNTER_METRICS) - self._counters() == set()

    def test_a_pause_writes_no_per_generation_counter_the_table_omits(self) -> None:
        """`heap_size` is the one counter outside the set: it is a gauge for
        the whole interpreter rather than for one generation (ADR-0004)."""
        assert self._counters() - set(GEN_COUNTER_METRICS) == {HEAP_SIZE}

    def test_every_counter_metric_is_a_field_a_record_carries(self) -> None:
        """A metric nothing reads off a record draws an empty track."""
        assert [m for m in GEN_COUNTER_METRICS if m not in GCStatsInfo.__annotations__] == []

    def test_every_jsonl_field_is_one_a_written_line_carries(self, tmp_path: Path) -> None:
        """`gcmon combine` reads these back, so a field named here and never
        written is one the reader waits for and never sees.

        Written out and read back rather than taken off `to_mapping`: `pid`
        is the writer's, and the loss figures sit one level down inside
        `gens`, so neither shows in what one record serialises to.
        """
        path = tmp_path / "written.jsonl"
        write_jsonl(path, {1: [create_mock_stats_item(), create_mock_loss_item()]})
        written = _keys(json.loads(line) for line in path.read_text(encoding=ENCODING).splitlines())
        assert [f for f in JSONL_FIELDS if f not in written] == []

    def test_a_loss_slice_carries_the_annotations_the_table_names(self) -> None:
        events = convert_loss_to_trace_format(proc(1), create_mock_loss_item())
        args = {key for e in events if isinstance(e, Slice) for key in e.args}
        assert set(SLICE_ARGS) & args != set(), "no slice carried an annotation the table names"


class TestALossSliceIsNamedForWhatItLost:
    """A function with branches rather than a constant: what is asserted is
    the shape the name takes, which `docs/formats.md` shows as
    `GC Loss(0,2)`."""

    def test_the_generations_run_together_so_each_set_takes_its_own_colour(self) -> None:
        assert gc_loss_slice_name([0, 2]) == f"{GC_LOSS_NAME}(0,2)"

    def test_an_interval_that_lost_nothing_carries_the_bare_word(self) -> None:
        assert gc_loss_slice_name([]) == GC_LOSS_NAME


class TestAGenerationTheTableNeverHeld:
    """Every per-generation name is rendered for `GENERATIONS` at import.
    A collector emits nothing outside them, so what is asked here is what a
    malformed capture meets: a name rather than a `KeyError`, and a table
    the same size afterwards."""

    def test_a_phase_renders_a_name_of_its_own_for_it(self) -> None:
        """Handing back a held generation's name would draw the record as
        that generation, which is worse than raising."""
        beyond = max(GENERATIONS) + 1
        assert PAUSE.slice_names[beyond] not in PAUSE.slice_names.values()
        assert PAUSE.categories[beyond] not in PAUSE.categories.values()

    def test_rendering_one_does_not_grow_the_table(self) -> None:
        """A capture can name as many generations as it likes, and each one
        kept would be a row it grew the table by."""
        beyond = max(GENERATIONS) + 1
        assert PAUSE.slice_names[beyond]
        assert PAUSE.categories[beyond]
        assert set(PAUSE.slice_names) == set(GENERATIONS)
        assert set(PAUSE.categories) == set(GENERATIONS)
