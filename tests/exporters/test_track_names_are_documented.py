"""Every name gcmon draws, checked against the page that lists them.

The pages tell a reader what a trace carries, row by row and field by field,
so they spell out names the code also holds as constants. Nothing kept the
two in step: a rename landed in the code and the page went stale silently,
and only a reader following the page would find out. Renaming `Interpreters`
to `Python Interpreters` under ADR-0027 touched six pages, which is what
prompted this.

The page holds the literal, not this file. Renaming a row means editing the
page, which is a visible act that also earns a CHANGELOG line; this only
stops the code drifting away from what the page already says. A copy of the
name here would add nothing and make a rename edit two copies.

Only one direction is covered. A name gcmon draws and the page never
mentions fails here. A name the page still carries after gcmon dropped it
does not: the page is prose, and a sentence may be naming history on
purpose.

The name has to appear inside backticks, the way the pages name every row
and slice they describe. A bare substring is too weak: `Interpreters` sits
inside `Python Interpreters`, so shortening the name back would have passed.

The heap counter is here because it is a row of its own, drawn beside the
``GC Metrics`` group rather than inside it (ADR-0004, ADR-0027). The
per-generation counters under the group are not: their names come from the
record fields, not from a constant the exporter holds.

The slices and the counter metrics are here too, not only the rows. Every
phase in ``GC_PHASES`` is drawn twice, once on the timeline and once as a
``--stats`` row, and the two spell it the same way (`gcmon.model.names`).
Adding a phase or a metric there fails here until the page describes it.

A metric is checked as the bare word, not as the counter row's
``G{gen} collected``: the generation is the caller's and the page writes the
row with the placeholder.

``JSONL_FIELDS`` and ``SLICE_ARGS`` are here for the same reason as the
rows. A field gcmon writes and the page never names is a figure nobody can
look up, and `gcmon combine` reads the same words back, so the page is the
only place both halves are described together.

``Interpreter {iid}`` and ``Process {pid}`` are absent below. Only their
fixed part is derivable from the code, and the pages write the row with the
placeholder, so there is nothing to match on. So are the control plane's
`start` and `stop`, which no page describes because no page needs to: one
module owns them and both sides import it, so there is no second copy to
drift. What a page does describe is the pair of instants they produce.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gcmon.control.protocol import START_EVENT, STOP_EVENT
from gcmon.exporters.perfetto_format import (
    _COUNTER_GROUP_NAME,
    _INTERPRETER_LIST_NAME,
    _LOSS_TRACK_NAME,
    _PAUSE_TRACK_NAME,
)
from gcmon.exporters.perfetto_process_lifetime import (
    _PROCESS_LIFETIME_TRACK_NAME,
    _PROCESS_ROW_SLICE_NAME,
)
from gcmon.model.names import (
    GC_LOSS_CATEGORY,
    GC_LOSS_NAME,
    GC_PHASES,
    GEN_COUNTER_METRICS,
    HEAP_SIZE,
    JSONL_FIELDS,
    RSS,
    SLICE_ARGS,
)
from gcmon.support.vocabulary import ENCODING

DOCS = Path(__file__).resolve().parents[2] / "docs"
FORMATS = DOCS / "formats.md"
SQL = DOCS / "perfetto-sql.md"

# Where a reader looks each name up. A category belongs to the SQL page
# because filtering is the only thing anyone does with one.
DOCUMENTED: tuple[tuple[str, Path], ...] = (
    *(
        (name, FORMATS)
        for name in (
            _PAUSE_TRACK_NAME,
            _LOSS_TRACK_NAME,
            HEAP_SIZE,
            _COUNTER_GROUP_NAME,
            _INTERPRETER_LIST_NAME,
            _PROCESS_LIFETIME_TRACK_NAME,
            _PROCESS_ROW_SLICE_NAME,
            RSS,
            GC_LOSS_NAME,
            START_EVENT,
            STOP_EVENT,
            *(phase.label for phase in GC_PHASES),
            *GEN_COUNTER_METRICS,
            *JSONL_FIELDS,
            *SLICE_ARGS,
        )
    ),
    *((phase.category, SQL) for phase in GC_PHASES),
    (GC_LOSS_CATEGORY, SQL),
)


@pytest.mark.parametrize(
    ("name", "page"),
    DOCUMENTED,
    ids=[f"{name}:{page.stem}" for name, page in DOCUMENTED],
)
def test_the_page_names_every_name_gcmon_draws(name: str, page: Path) -> None:
    assert f"`{name}`" in page.read_text(encoding=ENCODING), f"gcmon writes {name!r} and {page.name} does not name it"
