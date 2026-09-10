"""Every SQL example in ``docs/perfetto-sql.md``, run against a real trace.

A query on that page is a promise to a reader, and nothing checked it. Three
of the examples were once passing a hand check while returning no rows at all,
and one of those was wrong: it inner-joined `process` to the `Processes` track
and dropped every process gcmon knew from liveness alone.

So running the queries is not the test. **Returning rows is.** The fixture
below is built to give every example something to find, and a query that comes
back empty fails here whether it raised or not.

The blocks are read out of the page rather than copied, so an example edited
there is the one that runs.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from perfetto.trace_processor import TraceProcessor

from gcmon.exporters import PerfettoExporter
from tests.helpers import (
    create_mock_incremental_item,
    create_mock_loss_item,
    create_mock_stats_item,
    open_trace_processor,
    proc,
)

PAGE = Path(__file__).resolve().parents[2] / "docs" / "perfetto-sql.md"

_SQL_BLOCK = re.compile(r"```sql\n(.*?)```", re.DOTALL)


def _examples() -> list[str]:
    return [block.strip() for block in _SQL_BLOCK.findall(PAGE.read_text(encoding="utf-8"))]


def _label(sql: str) -> str:
    """The example's leading `-- comment`, which is how the page names it."""
    first = sql.splitlines()[0]
    return first.removeprefix("--").strip() if first.startswith("--") else first


# A pid the operating system handed out twice, so `Process 12345#2` exists and
# the examples that scope by `upid` or pair on the name have two of everything
# to tell apart.
REUSED_PID: int = 24680
FIRST_CMDLINE: tuple[str, ...] = ("python3", "-m", "first_target")
SECOND_CMDLINE: tuple[str, ...] = ("python3", "-m", "second_target")

# Two more processes whose spans cross: A starts first and dies first, B starts
# inside A and outlives it. The sweep clips A back to `B.start - 1`, which is
# the only way an example asking for drawn < observed finds a row (ADR-0011).
CROSS_A_PID: int = 1111
CROSS_B_PID: int = 2222

# A process gcmon only ever saw answer a poll. It draws a `Processes` span and
# no `process` row, which is what the lifetime example's LEFT JOIN is for.
LIVENESS_ONLY_PID: int = 3333


def _write_trace(path: Path) -> None:
    """One trace carrying something for every example on the page."""
    exporter = PerfettoExporter(output_path=path, flush_threshold=1000)

    first, second = proc(REUSED_PID, 1), proc(REUSED_PID, 2)
    exporter.add_process_cmdline(first, FIRST_CMDLINE)
    exporter.add_process_cmdline(second, SECOND_CMDLINE)
    for process, ts_start, rss in ((first, 100_000_000, 100_000_000), (second, 300_000_000, 900_000_000)):
        # An incremental item rather than a plain one: it draws the sub-step
        # slices the statistics example groups beside the pause.
        exporter.add_event(
            process,
            create_mock_incremental_item(gen=0, iid=0, ts_start=ts_start, ts_stop=ts_start + 40_000_000),
        )
        exporter.add_loss_event(
            process,
            create_mock_loss_item(
                iid=0,
                gen=0,
                ts_start=ts_start + 40_000_000,
                ts_stop=ts_start + 50_000_000,
            ),
        )
        exporter.add_rss_sample(process, rss, ts_start + 1_000_000)

    for pid, ts_start, ts_stop in (
        (CROSS_A_PID, 500_000_000, 800_000_000),
        (CROSS_B_PID, 600_000_000, 900_000_000),
    ):
        exporter.add_event(proc(pid), create_mock_stats_item(gen=0, iid=0, ts_start=ts_start, ts_stop=ts_stop))

    for ts in (600_000_000, 700_000_000):
        exporter.add_process_liveness({proc(LIVENESS_ONLY_PID)}, ts)

    exporter.close()


@pytest.fixture(scope="module")
def documented_trace_processor(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TraceProcessor]:
    """Module-scoped: the trace processor is a subprocess, and the examples
    only read."""
    path = tmp_path_factory.mktemp("perfetto-sql-docs") / "documented.pb"
    _write_trace(path)
    with open_trace_processor(path) as tp:
        yield tp


class TestTheDocumentedQueries:
    def test_the_page_still_holds_examples(self) -> None:
        """The guard on the extraction. Parametrizing over an empty list
        collects no tests and reports success, so a regex that stops matching
        would take the whole file quiet."""
        assert len(_examples()) >= 6

    @pytest.mark.parametrize("sql", _examples(), ids=_label)
    def test_the_example_runs_and_finds_rows(self, sql: str, documented_trace_processor: TraceProcessor) -> None:
        rows = list(documented_trace_processor.query(sql))

        assert rows, (
            "the example returned no rows against a trace built to exercise it; "
            "either the query is wrong or the fixture no longer covers it"
        )

    def test_the_fixture_is_a_trace_the_processor_accepts(
        self,
        documented_trace_processor: TraceProcessor,
    ) -> None:
        """An empty result above would otherwise be ambiguous: a rejected
        packet and a wrong query look the same from the row count."""
        rows = list(
            documented_trace_processor.query(
                "SELECT name, severity, value FROM stats WHERE value != 0 AND severity != 'info'"
            )
        )

        assert [(r.name, r.severity, r.value) for r in rows] == []

    def test_the_lifetime_example_keeps_a_process_with_no_process_row(
        self,
        documented_trace_processor: TraceProcessor,
    ) -> None:
        """Why that example starts from the span and left-joins the process.
        Driven from `process` it returns rows for everything that collected,
        so a row count says nothing; the process gcmon knew from liveness
        alone is the one that disappears."""
        lifetime = next(sql for sql in _examples() if "observed lifetime" in sql)

        names = {row.name for row in documented_trace_processor.query(lifetime)}

        assert f"Process {LIVENESS_ONLY_PID}" in names, (
            f"a process with a span and no `process` row was dropped: {sorted(names)}"
        )

    def test_the_statistics_example_leaves_loss_spans_out(
        self,
        documented_trace_processor: TraceProcessor,
    ) -> None:
        """The reason that example filters on the category. A loss span's
        width is an interval gcmon went blind for, and blending it into the
        pause percentiles reads as a pause nothing measured."""
        statistics = next(sql for sql in _examples() if "PERCENTILE" in sql)

        names = {row.name for row in documented_trace_processor.query(statistics)}

        assert names, "the statistics example found nothing to group"
        assert not any(name.startswith("GC Loss") for name in names), (
            f"loss spans reached the pause statistics: {sorted(names)}"
        )
