"""Every track name the exporter writes, checked against the page that
lists them.

``docs/formats.md`` tells a reader what a trace carries, row by row, so it
spells out names the exporter also holds as constants. Nothing kept the two
in step: a rename landed in the code and the page went stale silently, and
only a reader following the page would find out. Renaming `Interpreters` to
`Python Interpreters` under ADR-0027 touched six pages, which is what
prompted this.

Only one direction is covered. A name the exporter writes and the page never
mentions fails here. A name the page still carries after the exporter
dropped it does not: the page is prose, and a sentence may be naming history
on purpose.

The name has to appear inside backticks, the way the page names every row it
describes. A bare substring is too weak: `Interpreters` sits inside
`Python Interpreters`, so shortening the name back would have passed.

``Interpreter {iid}`` and ``Process {pid}`` are absent below. Only their fixed
part is derivable from the code, and the page writes the row with the
placeholder, so there is nothing to match on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

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

PAGE = Path(__file__).resolve().parents[2] / "docs" / "formats.md"

_NAMES: tuple[str, ...] = (
    _PAUSE_TRACK_NAME,
    _LOSS_TRACK_NAME,
    _COUNTER_GROUP_NAME,
    _INTERPRETER_LIST_NAME,
    _PROCESS_LIFETIME_TRACK_NAME,
    _PROCESS_ROW_SLICE_NAME,
)


@pytest.mark.parametrize("name", _NAMES)
def test_the_page_names_every_row_gcmon_draws(name: str) -> None:
    assert f"`{name}`" in PAGE.read_text(encoding="utf-8"), (
        f"gcmon writes a row named {name!r} and {PAGE.name} does not name it"
    )
