"""Tests for stats_output module."""

import re
from collections.abc import Callable
from pathlib import Path

import pytest

from gcmon.model.data import GCStatsInfo
from gcmon.model.names import (
    CLEAR_WEAKREFS,
    DEDUCE_UNREACHABLE,
    DELETE_GARBAGE,
    FILL_INCREMENT,
    FINALIZE_GARBAGE,
    HANDLE_RESURRECTED,
    HANDLE_WEAKREFS,
    MARK_ALIVE,
    gc_pause_slice_name,
    phase_slice_name,
)
from gcmon.model.run_report import RunReport
from gcmon.stats.stats import Stats
from gcmon.stats.stats_output import (
    READ_TIME_LABEL,
    TOTAL_LABEL,
    _build_rows,
    _print_table,
    print_stats,
    summary_lines,
)
from gcmon.stats.streaming_stats import StreamingStats
from gcmon.stats.views import StatsView, TableFormat
from tests.conftest import DEFAULT_PID
from tests.helpers import create_mock_incremental_item, create_mock_stats_item, proc

# The process whose rows these tests render.
TARGET_PID: int = 1

PAUSE_NS: int = 1_000_000
"""One pause's length, chosen so a rendered row reads in milliseconds."""


def _pause(ns: int = PAUSE_NS, iid: int = 0, heap_size: int | None = None) -> GCStatsInfo:
    """A gen-0 pause *ns* long, starting at zero."""
    if heap_size is None:
        return create_mock_stats_item(iid=iid, gen=0, ts_start=0, ts_stop=ns)
    return create_mock_stats_item(iid=iid, gen=0, ts_start=0, ts_stop=ns, heap_size=heap_size)


def _one_interpreter() -> StreamingStats:
    """Three pauses on one interpreter of one process."""
    stats = StreamingStats()
    for _ in range(3):
        stats.update(proc(DEFAULT_PID), _pause())
    return stats


def _notes(out: str) -> list[str]:
    """The numbered footer notes in a printed table."""
    return [line for line in out.splitlines() if line[:1].isdigit()]


class TestStatsOutput:
    """Tests for print_stats function."""

    def test_print_stats_empty(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Test print_stats with no data."""
        stats = StreamingStats()

        print_stats(stats, StatsView.FULL)

        captured = capsys.readouterr()
        assert "No GC statistics collected." in captured.out

    def test_print_stats_with_data(
        self,
        capsys: pytest.CaptureFixture[str],
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        """Test print_stats with some GC data."""
        stats = StreamingStats()
        item = gc_stats_item_factory(ts_stop=1000)
        stats.update(proc(DEFAULT_PID), item)

        print_stats(stats, StatsView.FULL)

        captured = capsys.readouterr()
        assert gc_pause_slice_name(0) in captured.out
        assert "Metric" in captured.out
        assert "Count" in captured.out
        assert "Sum" in captured.out
        assert "Avg" in captured.out
        assert "P50" in captured.out
        assert "P90" in captured.out
        assert "P95" in captured.out
        assert "P99" in captured.out

    def test_print_stats_multiple_generations(
        self,
        capsys: pytest.CaptureFixture[str],
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        """Test print_stats with multiple GC generations."""
        stats = StreamingStats()

        for gen in range(3):
            item = gc_stats_item_factory(
                gen=gen,
                ts_stop=1000 * (gen + 1),
            )
            stats.update(proc(DEFAULT_PID), item)

        print_stats(stats, StatsView.FULL)

        captured = capsys.readouterr()
        assert gc_pause_slice_name(0) in captured.out
        assert gc_pause_slice_name(1) in captured.out
        assert gc_pause_slice_name(2) in captured.out
        assert "Metric" in captured.out
        assert "Count" in captured.out
        assert "Sum" in captured.out

    def test_print_stats_table_format_plain(
        self,
        capsys: pytest.CaptureFixture[str],
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        """The rule under the header is dashes in either format, so the ones
        that count are the rules between blocks."""
        stats = StreamingStats()
        for pid in (11111, 22222):
            stats.update(proc(pid), gc_stats_item_factory())

        print_stats(stats, StatsView.FULL, table_format=TableFormat.PLAIN)

        table = [line for line in capsys.readouterr().out.splitlines() if line.startswith("|")]
        assert [set(line) for line in table[2:] if "-" in line] == [{"|", "-"}, {"|", "-"}]

    def test_print_stats_table_format_markdown(
        self,
        capsys: pytest.CaptureFixture[str],
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        """Test markdown table format uses blank rows as separators."""
        stats = StreamingStats()
        for pid in (11111, 22222):
            stats.update(proc(pid), gc_stats_item_factory())

        print_stats(stats, StatsView.FULL, table_format=TableFormat.MARKDOWN)

        captured = capsys.readouterr()
        lines = captured.out.splitlines()
        blank = any(
            line.startswith("|") and not any(c.isalpha() or c.isdigit() or c == "-" for c in line) for line in lines[2:]
        )
        assert blank


def _pipes(line: str) -> list[int]:
    """Where a table line puts its column borders."""
    return [at for at, char in enumerate(line) if char == "|"]


class TestPrintTable:
    """Tests for _print_table function."""

    def test_empty_rows_returns_early(self, capsys: pytest.CaptureFixture[str]) -> None:
        _print_table([])

        captured = capsys.readouterr()
        assert captured.out == ""

    def test_a_wide_cell_widens_its_column_on_every_line(self, capsys: pytest.CaptureFixture[str]) -> None:
        rows = [
            ["12345", "0", "100", "1000.000", "10.000", "20.000", "30.000", "40.000", "50.000", "1.00", "1.00"],
            ["1", "a metric wider than its header", "1", "1", "1", "1", "1", "1", "1", "1", "1"],
        ]

        _print_table(rows)

        lines = capsys.readouterr().out.strip().splitlines()
        assert {tuple(_pipes(line)) for line in lines} == {tuple(_pipes(lines[0]))}

    def test_separator_full_format(self, capsys: pytest.CaptureFixture[str]) -> None:
        rows = [
            ["12345", "0", "100", "1000.000", "10.000", "20.000", "30.000", "40.000", "50.000", "1.00", "1.00"],
        ]

        _print_table(rows, table_format=TableFormat.PLAIN)

        header, rule, *_ = capsys.readouterr().out.strip().splitlines()
        assert set(rule) == {"|", "-"}
        assert _pipes(rule) == _pipes(header)

    def test_separator_phase_format(self, capsys: pytest.CaptureFixture[str]) -> None:
        from gcmon.stats.stats_output import _SEP_PHASE

        rows = [
            ["12345", "0", "100", "1000.000", "10.000", "20.000", "30.000", "40.000", "50.000", "1.00", "1.00"],
            _SEP_PHASE,
            ["12345", "1", "200", "2000.000", "20.000", "30.000", "40.000", "50.000", "60.000", "1.00", "1.00"],
        ]

        _print_table(rows, table_format=TableFormat.PLAIN)

        first, *rest = capsys.readouterr().out.strip().splitlines()[3].strip("|").split("|")
        assert first.strip() == ""
        assert [set(cell) for cell in rest] == [{"-"}] * 10

    def test_separator_blank_markdown(self, capsys: pytest.CaptureFixture[str]) -> None:
        from gcmon.stats.stats_output import _SEP_GROUP

        rows = [
            ["12345", "0", "100", "1000.000", "10.000", "20.000", "30.000", "40.000", "50.000", "1.00", "1.00"],
            _SEP_GROUP,
            ["22222", "0", "200", "2000.000", "20.000", "30.000", "40.000", "50.000", "60.000", "1.00", "1.00"],
        ]

        _print_table(rows, table_format=TableFormat.MARKDOWN)

        captured = capsys.readouterr()
        lines = captured.out.strip().splitlines()
        blank_separator_found = any(line.startswith("|") and all(c in ("|", " ") for c in line) for line in lines[2:])
        assert blank_separator_found


class TestBuildRows:
    """Tests for _build_rows function."""

    def test_skips_zero_count_stats(self) -> None:
        stats = {0: Stats()}

        rows = _build_rows(stats, "Test", {}, False)

        assert len(rows) == 0

    def test_formats_values_correctly(self) -> None:
        """Whole milliseconds in, so each percentile lands in a cell of its own."""
        s = Stats()
        for v in [1_000_000.0, 2_000_000.0, 3_000_000.0]:
            s.update(v)
        s.materialize()

        rows = _build_rows({0: s}, "Test", {}, False)

        assert rows == [["Test(0)", "3", "6.000", "2.000", "2.000", "2.800", "2.900", "2.980", "100.0%", "1.000"]]

    def test_sorted_by_generation(self) -> None:
        stats_dict: dict[int, Stats] = {}
        for gen in [2, 0, 1]:
            s = Stats()
            s.update(1000.0)
            stats_dict[gen] = s

        rows = _build_rows(stats_dict, "Test", {}, False)

        generations = [int(r[0].split("(")[1].rstrip(")")) for r in rows]
        assert generations == [0, 1, 2]


class TestPrintStatsEdgeCases:
    """Tests for print_stats edge cases."""

    def test_multiple_pids_sorted(
        self,
        capsys: pytest.CaptureFixture[str],
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        stats = StreamingStats()
        for pid in (33333, 11111, 22222):
            stats.update(proc(pid), gc_stats_item_factory())

        print_stats(stats, StatsView.FULL)

        labels = [row[0] for row in table_rows(capsys.readouterr().out)[1:] if ":" in row[0]]
        assert labels == ["11111:0", "22222:0", "33333:0"]

    def test_total_label_first_metric(
        self,
        capsys: pytest.CaptureFixture[str],
        incremental_gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        stats = StreamingStats()
        # Generation 1 runs every phase, so each has a row.
        stats.update(proc(DEFAULT_PID), incremental_gc_stats_item_factory(gen=1))

        print_stats(stats, StatsView.TOTAL)

        _header, *block = table_rows(capsys.readouterr().out)
        assert [row[0] for row in block] == [TOTAL_LABEL] + [""] * 8

    def test_incremental_metrics_output(
        self,
        capsys: pytest.CaptureFixture[str],
        incremental_gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        stats = StreamingStats()
        item = incremental_gc_stats_item_factory(
            gen=1,
            ts_mark_alive_stop=5000,
            ts_fill_increment_start=5000,
            ts_fill_increment_stop=7000,
            ts_deduce_unreachable_start=7000,
        )
        stats.update(proc(DEFAULT_PID), item)

        print_stats(stats, StatsView.FULL)

        captured = capsys.readouterr()
        assert MARK_ALIVE.label in captured.out
        assert FILL_INCREMENT.label in captured.out
        assert DEDUCE_UNREACHABLE.label in captured.out
        assert HANDLE_WEAKREFS.label in captured.out
        assert FINALIZE_GARBAGE.label in captured.out
        assert HANDLE_RESURRECTED.label in captured.out
        assert CLEAR_WEAKREFS.label in captured.out
        assert DELETE_GARBAGE.label in captured.out

    def test_pause_row_printed_in_milliseconds(
        self,
        capsys: pytest.CaptureFixture[str],
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        stats = StreamingStats()
        stats.update(proc(DEFAULT_PID), gc_stats_item_factory(ts_start=0, ts_stop=1_000_000))
        stats.update(proc(DEFAULT_PID), gc_stats_item_factory(ts_start=0, ts_stop=3_000_000))

        print_stats(stats, StatsView.FULL)

        captured = capsys.readouterr()
        pause_line = next(line for line in captured.out.splitlines() if gc_pause_slice_name(0) in line)
        cells = [c.strip() for c in pause_line.strip().strip("|").split("|")]
        # PID, Metric, Count, Sum, Avg, P50, P90, P95, P99 - durations in milliseconds
        assert cells[1] == gc_pause_slice_name(0)
        assert cells[2] == "2"
        assert cells[3] == "4.000"
        assert cells[4] == "2.000"

    def test_read_time_row_omitted_when_not_recorded(
        self,
        capsys: pytest.CaptureFixture[str],
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        stats = StreamingStats()
        stats.update(proc(DEFAULT_PID), gc_stats_item_factory())

        print_stats(stats, StatsView.FULL)

        captured = capsys.readouterr()
        assert READ_TIME_LABEL not in captured.out

    def test_read_time_row_printed(
        self,
        capsys: pytest.CaptureFixture[str],
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        stats = StreamingStats()
        stats.update(proc(DEFAULT_PID), gc_stats_item_factory())
        stats.record_read_time(1_000_000)
        stats.record_read_time(3_000_000)

        print_stats(stats, StatsView.FULL)

        captured = capsys.readouterr()
        read_time_line = next(line for line in captured.out.splitlines() if READ_TIME_LABEL in line)
        cells = [c.strip() for c in read_time_line.strip().strip("|").split("|")]
        # PID, Metric, Count, Sum, Avg, P50, P90, P95, P99 - durations in milliseconds
        assert cells[0] == ""
        assert cells[1] == READ_TIME_LABEL
        assert cells[2] == "2"
        assert cells[3] == "4.000"
        assert cells[4] == "2.000"

    def test_read_time_row_without_gc_data(self, capsys: pytest.CaptureFixture[str]) -> None:
        stats = StreamingStats()
        stats.record_read_time(2_500_000)

        print_stats(stats, StatsView.FULL)

        captured = capsys.readouterr()
        assert "No GC statistics collected." not in captured.out
        assert READ_TIME_LABEL in captured.out
        assert "2.500" in captured.out

    def test_a_markdown_table_keeps_the_rule_under_its_header(
        self,
        capsys: pytest.CaptureFixture[str],
        gc_stats_item_factory: Callable[..., GCStatsInfo],
    ) -> None:
        """Every other rule goes blank. Without this one Markdown reads the
        lines as a paragraph."""
        stats = StreamingStats()
        stats.update(proc(DEFAULT_PID), gc_stats_item_factory())

        print_stats(stats, StatsView.FULL, table_format=TableFormat.MARKDOWN)

        _header, rule, *_ = capsys.readouterr().out.strip().splitlines()
        assert set(rule) == {"|", "-"}


def table_rows(out: str) -> list[list[str]]:
    """Every row of the printed table, header first, cells stripped.

    Separators carry no letters or digits; a row whose first cell is empty
    still does.
    """
    rows = []
    for line in out.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if any(char.isalnum() for cell in cells for char in cell):
            rows.append(cells)
    return rows


class TestTheTablePrintsRings:
    """One block per `(pid, iid)`, under a `Total` block for the run.

    The per-process block is gone: its rows blended interpreters the trace
    keeps apart.
    """

    def _two_interpreters(self) -> StreamingStats:
        """Same pid, different pause distributions: 1 ms against 20 ms."""
        stats = StreamingStats()
        for _ in range(3):
            stats.update(proc(DEFAULT_PID), _pause())
            stats.update(proc(DEFAULT_PID), _pause(20_000_000, iid=1))
        return stats

    def test_the_header_names_both_fields(self, capsys: pytest.CaptureFixture[str]) -> None:
        print_stats(_one_interpreter(), StatsView.FULL)

        assert table_rows(capsys.readouterr().out)[0][0] == "PID:IID"

    def test_an_ordinary_run_still_carries_its_iid(self, capsys: pytest.CaptureFixture[str]) -> None:
        """`12345:0` on a single-interpreter run as much as on a tree."""
        print_stats(_one_interpreter(), StatsView.FULL)

        labels = [row[0] for row in table_rows(capsys.readouterr().out)]
        assert "12345:0" in labels
        assert "12345" not in labels

    def test_only_the_first_column_moves(self, capsys: pytest.CaptureFixture[str]) -> None:
        """The regression guard: one interpreter of one pid is the whole run,
        so its row and `Total` still agree cell for cell."""
        print_stats(_one_interpreter(), StatsView.FULL)

        rows = table_rows(capsys.readouterr().out)
        total = next(row for row in rows if row[0] == TOTAL_LABEL)
        ring = next(row for row in rows if row[0] == "12345:0")
        assert total[1:] == ring[1:]

    def test_two_interpreters_print_two_rows(self, capsys: pytest.CaptureFixture[str]) -> None:
        print_stats(self._two_interpreters(), StatsView.FULL)

        labels = [row[0] for row in table_rows(capsys.readouterr().out)]
        assert labels.count("12345:0") == 1
        assert labels.count("12345:1") == 1

    def test_each_ring_row_keeps_its_own_distribution(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A `P99` over both would describe neither interpreter."""
        print_stats(self._two_interpreters(), StatsView.FULL)

        rows = table_rows(capsys.readouterr().out)
        p99 = {row[0]: row[8] for row in rows if row[0] in (TOTAL_LABEL, "12345:0", "12345:1")}
        p50 = {row[0]: row[5] for row in rows if row[0] in (TOTAL_LABEL, "12345:0", "12345:1")}
        assert p99["12345:0"] == "1.000"
        assert p99["12345:1"] == "20.000"
        # The blend sits between the two, describing neither.
        assert p50[TOTAL_LABEL] not in (p50["12345:0"], p50["12345:1"])

    def _one_starved_interpreter(self) -> StreamingStats:
        """Interpreter 0 read all three of its collections; interpreter 1 read
        one of ten."""
        stats = StreamingStats()
        for _ in range(3):
            stats.update(proc(DEFAULT_PID), _pause())
        stats.update(proc(DEFAULT_PID), _pause(iid=1))
        stats.record_loss(proc(DEFAULT_PID), 1, 0, 9, 9_000_000)
        return stats

    def test_each_ring_row_carries_its_own_coverage(self, capsys: pytest.CaptureFixture[str]) -> None:
        """What an operator sees: the starved interpreter reads 10% on its own
        row instead of hiding in a process-wide 30.8%."""
        print_stats(self._one_starved_interpreter(), StatsView.FULL)
        rows = table_rows(capsys.readouterr().out)

        cov = {row[0]: row[9] for row in rows if row[0] in (TOTAL_LABEL, "12345:0", "12345:1")}

        assert cov["12345:0"] == "100.0%"
        assert cov["12345:1"] == "10.0%"
        assert cov[TOTAL_LABEL] == "30.8%"

    def test_a_ring_that_lost_nothing_prints_one_number_per_cell(self, capsys: pytest.CaptureFixture[str]) -> None:
        """`3/3` beside a neighbour's `1/10` would say nothing was lost twice
        over on a table where something was."""
        print_stats(self._one_starved_interpreter(), StatsView.FULL)
        rows = table_rows(capsys.readouterr().out)

        count = {row[0]: row[2] for row in rows if row[0] in ("12345:0", "12345:1")}

        assert count["12345:0"] == "3"
        assert count["12345:1"] == "1/10"

    def test_rings_sort_by_pid_then_interpreter(self, capsys: pytest.CaptureFixture[str]) -> None:
        stats = StreamingStats()
        for pid, iid in ((22222, 1), (12345, 1), (22222, 0), (12345, 0)):
            stats.update(proc(pid), _pause(iid=iid))

        print_stats(stats, StatsView.FULL)

        labels = [row[0] for row in table_rows(capsys.readouterr().out)[1:] if ":" in row[0]]
        assert labels == ["12345:0", "12345:1", "22222:0", "22222:1"]

    def test_read_time_belongs_to_no_ring(self, capsys: pytest.CaptureFixture[str]) -> None:
        stats = _one_interpreter()
        stats.record_read_time(500_000)

        print_stats(stats, StatsView.FULL)

        read_time = next(row for row in table_rows(capsys.readouterr().out) if row[1] == READ_TIME_LABEL)
        assert read_time[0] == ""


class TestLossColumns:
    def _lossy(self) -> StreamingStats:
        stats = StreamingStats()
        for _ in range(3):
            stats.update(proc(TARGET_PID), _pause())
        stats.record_loss(proc(TARGET_PID), 0, 0, 7, 7_000_000)
        return stats

    def test_count_and_sum_carry_both_numbers(self, capsys: pytest.CaptureFixture[str]) -> None:
        print_stats(self._lossy(), StatsView.FULL)

        out = capsys.readouterr().out
        assert "3/10" in out
        assert "3.000/10.000" in out

    def test_a_sub_phase_row_marks_its_companions_as_estimates(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Only the pause has the target's own counters behind it. A sub-phase
        is scaled, its count by the coverage and its sum by ``F``, and the
        seven lost pauses here ran twice as long as the three that were read.
        """
        stats = StreamingStats()
        for _ in range(3):
            stats.update(proc(TARGET_PID), create_mock_incremental_item(gen=0, ts_start=0, ts_stop=PAUSE_NS))
        stats.record_loss(proc(TARGET_PID), 0, 0, 7, 14 * PAUSE_NS)

        print_stats(stats, StatsView.TOTAL)

        rows = {row[1]: row for row in table_rows(capsys.readouterr().out)}
        assert rows[phase_slice_name(FILL_INCREMENT, 0)][2:4] == ["3/~10", "3.000/~17.000"]
        assert rows[gc_pause_slice_name(0)][2:4] == ["3/10", "3.000/17.000"]

    def test_cov_and_f_are_columns(self, capsys: pytest.CaptureFixture[str]) -> None:
        print_stats(self._lossy(), StatsView.FULL)

        header, total, *_ = table_rows(capsys.readouterr().out)
        assert header[9:] == ["Cov", "F"]
        assert total[9:] == ["30.0%", "3.333"]

    def test_a_lossless_run_shows_one_number_per_cell(self, capsys: pytest.CaptureFixture[str]) -> None:
        """`3/3` in every cell would say nothing was lost twice over."""
        stats = StreamingStats()
        for _ in range(3):
            stats.update(proc(TARGET_PID), _pause())

        print_stats(stats, StatsView.FULL)

        out = capsys.readouterr().out
        assert "3/3" not in out
        assert "100.0%" in out

    def test_a_lossless_run_prints_no_footer(self, capsys: pytest.CaptureFixture[str]) -> None:
        stats = StreamingStats()
        stats.update(proc(TARGET_PID), _pause())

        print_stats(stats, StatsView.FULL)

        assert "Coverage:" not in capsys.readouterr().out

    def test_the_footer_names_the_coverage(self, capsys: pytest.CaptureFixture[str]) -> None:
        print_stats(self._lossy(), StatsView.FULL)

        out = capsys.readouterr().out
        assert "Coverage: Gen0 30.0%" in out
        assert "percentiles are sampled and read high" in out

    def test_the_footer_separates_the_cumulative_counters_from_the_session(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """It is not loss and must not read as part of `Cov`."""
        stats = self._lossy()
        stats.observe_cumulative(proc(TARGET_PID), 0, 0, 5_000, 5.0)

        print_stats(stats, StatsView.FULL)

        out = capsys.readouterr().out
        assert "Since each interpreter started" in out
        assert "Gen0 5000" in out

    def test_read_time_leaves_cov_and_f_blank(self, capsys: pytest.CaptureFixture[str]) -> None:
        stats = self._lossy()
        stats.record_read_time(500_000)

        print_stats(stats, StatsView.FULL)

        read_time = next(row for row in table_rows(capsys.readouterr().out) if row[1] == READ_TIME_LABEL)
        assert read_time[9:] == ["", ""]

    def test_cov_never_rounds_up_past_a_visible_gap(self, capsys: pytest.CaptureFixture[str]) -> None:
        """1763 of 1771 is 99.5%, but a coarser format would print 100% beside
        a `Count` cell plainly showing eight missing."""
        stats = StreamingStats()
        for _ in range(1763):
            stats.update(proc(TARGET_PID), _pause())
        stats.record_loss(proc(TARGET_PID), 0, 0, 8, 8_000_000)

        print_stats(stats, StatsView.FULL)

        out = capsys.readouterr().out
        assert "1763/1771" in out
        assert "99.5%" in out
        assert "100.0%" not in out

    def test_a_gap_too_small_to_show_still_says_so(self, capsys: pytest.CaptureFixture[str]) -> None:
        """One lost past 2000 read is where both cells round to a whole."""
        stats = StreamingStats()
        for _ in range(3_000):
            stats.update(proc(TARGET_PID), _pause(1_000))
        stats.record_loss(proc(TARGET_PID), 0, 0, 1, 1_000)

        print_stats(stats, StatsView.FULL)

        out = capsys.readouterr().out
        assert "<100.0%" in out
        assert ">1.000" in out

    def test_the_footer_matches_the_column(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Two roundings of one number that disagree are worse than either."""
        stats = StreamingStats()
        for _ in range(1763):
            stats.update(proc(TARGET_PID), _pause())
        stats.record_loss(proc(TARGET_PID), 0, 0, 8, 8_000_000)

        print_stats(stats, StatsView.FULL)

        out = capsys.readouterr().out
        assert "Coverage: Gen0 99.5%" in out


class TestTheFooterNotesAreNumbered:
    """Which notes appear depends on the run, so their order teaches a reader
    nothing. The number is what separates one from the next once two of them
    wrap across a narrow terminal.
    """

    def test_every_note_present_is_numbered_in_order(self, capsys: pytest.CaptureFixture[str]) -> None:
        stats = StreamingStats()
        for _ in range(3):
            stats.update(proc(TARGET_PID), _pause())
        stats.record_loss(proc(TARGET_PID), 0, 0, 7, 7_000_000)
        stats.observe_cumulative(proc(TARGET_PID), 0, 0, 18, 0.02)

        print_stats(stats, StatsView.FULL)

        notes = _notes(capsys.readouterr().out)
        assert [note.split(".", 1)[0] for note in notes] == ["1", "2"]
        assert "Coverage:" in notes[0]
        assert "Since each interpreter started" in notes[1]

    def test_a_lone_note_is_still_numbered(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Numbering that appeared only above some threshold would make the
        footer's shape depend on its length, which is harder to scan than a
        `1.` with nothing under it."""
        stats = StreamingStats()
        for _ in range(3):
            stats.update(proc(TARGET_PID), _pause())
        stats.record_loss(proc(TARGET_PID), 0, 0, 7, 7_000_000)

        print_stats(stats, StatsView.FULL)

        notes = _notes(capsys.readouterr().out)
        assert len(notes) == 1
        assert notes[0].startswith("1. Coverage:")

    def test_a_run_with_nothing_to_explain_numbers_nothing(self, capsys: pytest.CaptureFixture[str]) -> None:
        stats = StreamingStats()
        stats.update(proc(TARGET_PID), _pause())

        print_stats(stats, StatsView.FULL)

        assert _notes(capsys.readouterr().out) == []


class TestTheCumulativeNoteNamesItsFold:
    """One line per generation whatever the size of the tree, stating what it
    summed over: interpreters start at different moments, and a reused pid
    folds two processes into one figure.
    """

    _SCOPE = re.compile(r"summed over (\d+) interpreters? in (\d+) process(?:es)?")

    def _scope(self, out: str) -> tuple[int, int]:
        """The two counts the note printed, read back off the page."""
        match = self._SCOPE.search(out)
        assert match is not None, out
        return int(match[1]), int(match[2])

    def _stats(self, rings: list[tuple[int, int]]) -> StreamingStats:
        stats = StreamingStats()
        for pid, iid in rings:
            stats.update(proc(pid), _pause(iid=iid))
            stats.observe_cumulative(proc(pid), iid, 0, 500, 0.5)
        return stats

    def test_the_counts_are_the_ones_it_summed(self, capsys: pytest.CaptureFixture[str]) -> None:
        stats = self._stats([(1, 0), (1, 1), (2, 0)])

        print_stats(stats, StatsView.FULL)

        assert self._scope(capsys.readouterr().out) == stats.cumulative_scope() == (3, 2)

    def test_an_ordinary_run_reads_in_the_singular(self, capsys: pytest.CaptureFixture[str]) -> None:
        print_stats(self._stats([(1, 0)]), StatsView.FULL)

        assert "summed over 1 interpreter in 1 process:" in capsys.readouterr().out

    def test_the_figure_beside_the_counts_is_the_fold(self, capsys: pytest.CaptureFixture[str]) -> None:
        print_stats(self._stats([(1, 0), (1, 1), (2, 0)]), StatsView.FULL)

        assert "Gen0 1500 in 1500.000 ms" in capsys.readouterr().out

    def test_it_still_says_the_window_is_included(self, capsys: pytest.CaptureFixture[str]) -> None:
        """The interval overlaps the monitored window rather than extending
        it, so the note must not read as a figure to add to `Count`."""
        print_stats(self._stats([(1, 0)]), StatsView.FULL)

        assert "monitored window included" in capsys.readouterr().out


class TestTheBlockOfAReusedPid:
    """Two processes held the pid, so the table carries two blocks and the
    heading says which is which."""

    def _reused(self) -> StreamingStats:
        stats = StreamingStats()
        stats.update(proc(DEFAULT_PID), _pause())
        stats.materialize(proc(DEFAULT_PID))
        stats.update(proc(DEFAULT_PID, 2), _pause(9_000_000))
        return stats

    def test_the_first_block_reads_plain(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Which is every block of an ordinary run, so nothing widens for a
        target that reuses no pid."""
        print_stats(self._reused(), StatsView.FULL)

        assert "12345:0 " in capsys.readouterr().out

    def test_the_second_block_says_which_process_it_is(self, capsys: pytest.CaptureFixture[str]) -> None:
        print_stats(self._reused(), StatsView.FULL)

        assert "12345:0#2" in capsys.readouterr().out

    def test_the_two_blocks_carry_their_own_figures(self, capsys: pytest.CaptureFixture[str]) -> None:
        """One heading over both sets of numbers was the defect."""
        print_stats(self._reused(), StatsView.FULL)
        rows = table_rows(capsys.readouterr().out)

        sums = {row[0]: row[4] for row in rows if row[0].startswith("12345:0")}
        assert sums == {"12345:0": "1.000", "12345:0#2": "9.000"}

    def test_an_ordinary_run_carries_no_suffix(self, capsys: pytest.CaptureFixture[str]) -> None:
        stats = StreamingStats()
        stats.update(proc(DEFAULT_PID), _pause())

        print_stats(stats, StatsView.FULL)

        assert "#" not in capsys.readouterr().out


class TestTheNoteOnRingsWithNoRow:
    """The rows can add up to less than the run, so the footer says by how
    many rings rather than leaving a reader to find the gap."""

    def _crowded(self, extra: int) -> StreamingStats:
        stats = StreamingStats()
        for pid in range(StreamingStats.MAX_ACTIVE_RINGS + extra):
            stats.update(proc(pid), _pause())
        return stats

    def test_a_run_that_fits_says_nothing(self, capsys: pytest.CaptureFixture[str]) -> None:
        print_stats(self._crowded(0), StatsView.FULL)

        assert "got no row" not in capsys.readouterr().out

    def test_it_counts_the_rings_left_out(self, capsys: pytest.CaptureFixture[str]) -> None:
        print_stats(self._crowded(3), StatsView.FULL)

        assert "3 rings got no row" in capsys.readouterr().out

    def test_one_ring_reads_in_the_singular(self, capsys: pytest.CaptureFixture[str]) -> None:
        print_stats(self._crowded(1), StatsView.FULL)

        assert "1 ring got no row" in capsys.readouterr().out

    def test_it_points_at_total(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Those records are in the run's cost, which is what the note is for:
        the detail is missing, the arithmetic is not."""
        print_stats(self._crowded(1), StatsView.FULL)

        assert "counted in Total" in capsys.readouterr().out


class TestTheTwoViews:
    """`total` is `full` minus the ring blocks, and minus one footer note."""

    def _two_interpreters(self) -> StreamingStats:
        """Same pid, 1 ms against 20 ms, and a read time under both blocks."""
        stats = StreamingStats()
        for _ in range(3):
            stats.update(proc(DEFAULT_PID), _pause())
            stats.update(proc(DEFAULT_PID), _pause(20_000_000, iid=1))
        stats.record_read_time(500_000)
        return stats

    def _crowded(self) -> StreamingStats:
        """A run with all three footer notes: a loss, a cumulative counter and
        one interpreter too many to hold a ring for."""
        stats = StreamingStats()
        for pid in range(StreamingStats.MAX_ACTIVE_RINGS + 1):
            stats.update(proc(pid), _pause())
        stats.record_loss(proc(0), 0, 0, 7, 7_000_000)
        # Two generations, so a note that dropped one shows as a shorter line.
        stats.record_loss(proc(0), 0, 1, 3, 3_000_000)
        stats.observe_cumulative(proc(0), 0, 0, 18, 0.02)
        return stats

    def _out(self, capsys: pytest.CaptureFixture[str], stats: StreamingStats, view: StatsView) -> str:
        print_stats(stats, view)
        return capsys.readouterr().out

    def test_total_prints_the_run_and_no_ring(self, capsys: pytest.CaptureFixture[str]) -> None:
        rows = table_rows(self._out(capsys, self._two_interpreters(), StatsView.TOTAL))

        labels = [row[0] for row in rows[1:]]
        assert TOTAL_LABEL in labels
        assert [label for label in labels if ":" in label] == []

    def test_total_still_ends_on_the_read_time(self, capsys: pytest.CaptureFixture[str]) -> None:
        """It is monitor-side and belongs to no ring, so the view that drops
        the rings keeps it."""
        rows = table_rows(self._out(capsys, self._two_interpreters(), StatsView.TOTAL))

        assert rows[-1][1] == READ_TIME_LABEL

    def test_total_is_the_head_of_full(self, capsys: pytest.CaptureFixture[str]) -> None:
        """The regression guard: every line the narrower view prints before
        `Read Time` is the wider view's line, to the byte.

        It reads that way where the ring labels fit under the `PID:IID`
        header. The test below covers a wider label.
        """
        stats = self._two_interpreters()

        total = self._out(capsys, stats, StatsView.TOTAL).splitlines()
        full = self._out(capsys, stats, StatsView.FULL).splitlines()

        head = total[: next(i for i, line in enumerate(total) if READ_TIME_LABEL in line)]
        assert full[: len(head)] == head

    def test_a_wider_ring_label_pads_the_first_column_of_full_alone(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A six-digit pid leaves `full` one character wider in the first
        column. Padding moved, no number did.
        """
        stats = StreamingStats()
        for _ in range(3):
            stats.update(proc(123456), _pause())

        total = table_rows(self._out(capsys, stats, StatsView.TOTAL))
        full = table_rows(self._out(capsys, stats, StatsView.FULL))

        assert full[: len(total)] == total
        assert [row[0] for row in full[len(total) :]] == ["123456:0"]

    def test_the_views_of_a_single_ring_run_differ_by_one_block(self, capsys: pytest.CaptureFixture[str]) -> None:
        """What `full` adds here is a copy of the roll-up above it."""
        stats = _one_interpreter()

        total = table_rows(self._out(capsys, stats, StatsView.TOTAL))
        full = table_rows(self._out(capsys, stats, StatsView.FULL))

        added = full[len(total) :]
        assert [row[0] for row in added] == ["12345:0"]
        assert [row[1:] for row in added] == [row[1:] for row in total if row[0] == TOTAL_LABEL]

    def test_the_untracked_note_is_full_only(self, capsys: pytest.CaptureFixture[str]) -> None:
        """It reconciles ring rows against the run, and `total` prints none."""
        stats = self._crowded()

        assert "got no row" in self._out(capsys, stats, StatsView.FULL)
        assert "got no row" not in self._out(capsys, stats, StatsView.TOTAL)

    @pytest.mark.parametrize("view", [StatsView.TOTAL, StatsView.FULL])
    def test_the_run_wide_notes_lead_either_view(self, capsys: pytest.CaptureFixture[str], view: StatsView) -> None:
        """Coverage and the lifetime totals are run-wide, so they come first
        and the numbering closes over whatever is left."""
        notes = _notes(self._out(capsys, self._crowded(), view))

        assert "Coverage:" in notes[0]
        assert "Since each interpreter started" in notes[1]
        assert [note.split(".", 1)[0] for note in notes[:2]] == ["1", "2"]

    def test_the_run_wide_notes_read_the_same_under_both(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Word for word, the number aside."""
        stats = self._crowded()

        total = _notes(self._out(capsys, stats, StatsView.TOTAL))
        full = _notes(self._out(capsys, stats, StatsView.FULL))

        assert [note.split(". ", 1)[1] for note in total[:2]] == [note.split(". ", 1)[1] for note in full[:2]]

    @pytest.mark.parametrize("view", [StatsView.TOTAL, StatsView.FULL])
    def test_a_run_that_collected_nothing_says_so_in_either_view(
        self, capsys: pytest.CaptureFixture[str], view: StatsView
    ) -> None:
        assert "No GC statistics collected." in self._out(capsys, StreamingStats(), view)


POINTER = "Run with --stats=total for the per-generation breakdown."

_COUNTS = re.compile(r"^Total events: (\d+) \(\+(\d+) reconstructed, ([\d.]+)% observed\)$")


def read_counts(lines: list[str]) -> tuple[int, int, float]:
    """The three numbers the summary printed, read back off the page.

    Reading them back beats asserting a literal string: a summary quoting the
    sampled count in both positions satisfies a literal and tells an operator
    nothing.
    """
    match = next(m for m in (_COUNTS.match(line) for line in lines) if m is not None)
    return int(match[1]), int(match[2]), float(match[3])


class TestSummaryLines:
    """What every run says about its own capture, `--stats` or not.

    The table is the breakdown; these lines are what an operator who asked for
    nothing still reads, so the count cannot stand there unqualified.
    """

    def _run(self, sampled: int, lost: int = 0) -> StreamingStats:
        stats = StreamingStats()
        for _ in range(sampled):
            stats.update(proc(TARGET_PID), _pause())
        if lost:
            stats.record_loss(proc(TARGET_PID), 0, 0, lost, lost * 1_000_000)
        return stats

    def test_a_lossless_run_says_only_what_it_read(self) -> None:
        """Today's three lines to the byte, so no scripted run or CI log
        reading them has to change."""
        assert summary_lines(self._run(1234), Path("trace.pftrace")) == [
            "Monitoring complete.",
            "Total events: 1234",
            "Trace saved to: trace.pftrace",
        ]

    def test_a_stdout_trace_names_no_file(self) -> None:
        """`--format stdout` writes the trace to stdout, so there is no path
        to name and the caller passes none."""
        assert summary_lines(self._run(3), None) == [
            "Monitoring complete.",
            "Total events: 3",
        ]

    def test_a_lossy_run_says_what_the_count_is_a_share_of(self) -> None:
        assert summary_lines(self._run(1234, lost=8566), Path("trace.pftrace")) == [
            "Monitoring complete.",
            "Total events: 1234 (+8566 reconstructed, 12.6% observed)",
            POINTER,
            "Trace saved to: trace.pftrace",
        ]

    def test_a_run_that_kept_up_says_so(self) -> None:
        """Coverage alone cannot separate "the target collects fast" from
        "gcmon never got to look", so the summary states the denominator."""
        lines = summary_lines(self._run(3), None, pacing=RunReport(ticks_run=600, ticks_scheduled=600))

        assert "Ticks: 600 of 600 scheduled" in lines

    def test_a_run_that_overran_says_how_far_short_it_fell(self) -> None:
        lines = summary_lines(self._run(3), None, pacing=RunReport(ticks_run=188, ticks_scheduled=600))

        assert "Ticks: 188 of 600 scheduled" in lines

    def test_a_lossy_run_that_kept_up_is_told_polling_more_may_help(self) -> None:
        lines = summary_lines(self._run(3, lost=7), None, pacing=RunReport(ticks_run=600, ticks_scheduled=600))

        assert any("may observe more" in line for line in lines)
        assert not any("will not help" in line for line in lines)

    def test_a_lossy_run_that_overran_is_told_the_rate_is_not_the_problem(self) -> None:
        """The advice the monitor used to give unconditionally. Lowering the
        rate cannot add ticks the loop already could not reach."""
        lines = summary_lines(self._run(3, lost=7), None, pacing=RunReport(ticks_run=188, ticks_scheduled=600))

        assert any("will not help" in line for line in lines)
        assert not any("may observe more" in line for line in lines)

    def test_a_run_that_lost_nothing_is_given_no_remedy(self) -> None:
        """Nothing to remedy, whether or not the loop kept up."""
        lines = summary_lines(self._run(3), None, pacing=RunReport(ticks_run=188, ticks_scheduled=600))

        assert not any("--rate" in line for line in lines)

    def test_a_caller_with_nothing_to_say_about_pacing_says_nothing(self) -> None:
        """The summary is built in tests and by callers that never ran a loop;
        no report means no line, rather than a line full of zeroes."""
        assert not [line for line in summary_lines(self._run(3), None) if line.startswith("Ticks:")]

    def test_the_printed_numbers_come_from_the_stats(self) -> None:
        stats = self._run(1234, lost=8566)
        totals = stats.pause_totals_by_gen()[0]

        sampled, reconstructed, _observed = read_counts(summary_lines(stats, None))

        assert sampled == stats.count()
        assert reconstructed == totals.lost_count

    def test_the_percentage_divides_the_two_numbers_beside_it(self) -> None:
        """A reader who does the arithmetic on the page gets the figure on the
        page. It may sit a fraction off the `Cov` column, which counts only
        records carrying a pause, but it cannot contradict its own line."""
        sampled, reconstructed, observed = read_counts(summary_lines(self._run(1234, lost=8566), None))

        assert observed == pytest.approx(100 * sampled / (sampled + reconstructed), abs=0.05)

    def test_it_counts_the_loss_of_every_generation_and_pid(self) -> None:
        """`Total events` counts every record of every pid, so the number
        beside it has to cover the same ground."""
        stats = self._run(10)
        stats.record_loss(proc(TARGET_PID), 0, 1, 5, 5_000_000)
        stats.record_loss(proc(2), 0, 0, 5, 5_000_000)

        _sampled, reconstructed, observed = read_counts(summary_lines(stats, None))

        assert reconstructed == 10
        assert observed == pytest.approx(50.0)

    def test_a_gap_too_small_to_show_still_says_so(self) -> None:
        """2000 read of 2001 rounds to 100.0%, on a line showing one
        reconstructed. `Cov` has the same problem and the same answer."""
        assert "Total events: 2000 (+1 reconstructed, <100.0% observed)" in summary_lines(self._run(2000, lost=1), None)

    def test_the_pointer_appears_once_and_only_when_qualified(self) -> None:
        """It points at the breakdown of a figure the line above it just
        raised. A run with nothing to break down has nothing to point at."""
        assert summary_lines(self._run(3, lost=7), Path("trace.pftrace")).count(POINTER) == 1
        assert POINTER not in summary_lines(self._run(3), Path("trace.pftrace"))

    def test_a_run_that_asked_for_the_table_is_not_sent_for_it(self) -> None:
        """`--stats` prints the breakdown two lines further down, so pointing
        the reader at it there would be pointing at the next paragraph."""
        lines = summary_lines(self._run(3, lost=7), Path("trace.pftrace"), show_stats=True)

        assert POINTER not in lines
        assert "Total events: 3 (+7 reconstructed, 30.0% observed)" in lines

    def test_it_prints_nothing_itself(self, capsys: pytest.CaptureFixture[str]) -> None:
        """`--format stdout` puts the JSONL trace on stdout, so a summary that
        printed would land in the middle of the stream."""
        summary_lines(self._run(3, lost=7), None)

        assert capsys.readouterr().out == ""
