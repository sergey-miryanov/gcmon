"""Every environment variable gcmon reads, checked against the page that
documents it.

``docs/cli.md`` and ``docs/pyperf.md`` tell a reader what to put in the
environment, so they spell out names the code also holds as constants.
Nothing kept the two in step, and the same gap that let a track name drift
away from ``docs/formats.md`` is open here: a rename lands in the code and
the table goes stale silently.

With the pin in one place, every other test imports the constant instead of
repeating the spelling, so a rename touches the code, the page and this
tuple rather than forty call sites.

Only one direction is covered. A variable the code reads and the page never
names fails here. A name the page still carries after the code stopped
reading it does not: the page is prose, and a sentence may be naming history
on purpose.

The name has to appear inside backticks, the way both pages name every
variable they describe.

``GCMON_THREAD_ID``, ``GCMON_SERVER_HOST`` and ``GCMON_SERVER_PORT`` are
absent below. ``_env`` defines and exports a getter for each, and nothing in
gcmon calls it, so there is no behaviour for a page to describe.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gcmon.cli.monitor import _env
from gcmon.cli.monitor._env import (
    ENV_CONTROL_NAME,
    ENV_DURATION,
    ENV_FLUSH_THRESHOLD,
    ENV_FORMAT,
    ENV_OUTPUT,
    ENV_RATE,
    ENV_RSS,
    ENV_RSS_INTERVAL,
    ENV_STATS,
    ENV_TABLE_FORMAT,
    ENV_VERBOSE,
)
from gcmon.control.control_server import CONTROL_ADDRESS_ENV
from gcmon.pyperf.hook import ENV_PYPERF_HOOK_CONTROL_TIMEOUT, ENV_PYPERF_HOOK_VERBOSE
from gcmon.support.vocabulary import ENCODING

DOCS = Path(__file__).resolve().parents[2] / "docs"
CLI_PAGE = DOCS / "cli.md"
PYPERF_PAGE = DOCS / "pyperf.md"

DOCUMENTED: tuple[tuple[str, Path], ...] = (
    (ENV_OUTPUT, CLI_PAGE),
    (ENV_RATE, CLI_PAGE),
    (ENV_DURATION, CLI_PAGE),
    (ENV_VERBOSE, CLI_PAGE),
    (ENV_FORMAT, CLI_PAGE),
    (ENV_FLUSH_THRESHOLD, CLI_PAGE),
    (ENV_STATS, CLI_PAGE),
    (ENV_TABLE_FORMAT, CLI_PAGE),
    (ENV_CONTROL_NAME, CLI_PAGE),
    (ENV_RSS, CLI_PAGE),
    (ENV_RSS_INTERVAL, CLI_PAGE),
    (CONTROL_ADDRESS_ENV, CLI_PAGE),
    (ENV_PYPERF_HOOK_VERBOSE, PYPERF_PAGE),
    (ENV_PYPERF_HOOK_CONTROL_TIMEOUT, PYPERF_PAGE),
)

# Read by no caller, so nothing documents them. See the module docstring.
UNREAD: frozenset[str] = frozenset({"GCMON_THREAD_ID", "GCMON_SERVER_HOST", "GCMON_SERVER_PORT"})


@pytest.mark.parametrize(("name", "page"), DOCUMENTED, ids=[name for name, _ in DOCUMENTED])
def test_the_page_names_every_variable_gcmon_reads(name: str, page: Path) -> None:
    assert f"`{name}`" in page.read_text(encoding=ENCODING), f"gcmon reads {name!r} and {page.name} does not name it"


def test_every_env_constant_is_either_documented_or_named_unread() -> None:
    """A new variable forces the choice rather than slipping past this file."""
    defined = {
        value
        for name, value in vars(_env).items()
        if name.startswith("ENV_") and name != "ENV_PREFIX" and isinstance(value, str)
    }
    covered = {name for name, _ in DOCUMENTED} | UNREAD

    assert defined - covered == set(), "add it to DOCUMENTED, or to UNREAD with a reason"
