"""Which benchmark each process ran, from pyperformance's results file (spec 0076)."""

import gzip
import json
from pathlib import Path
from typing import Any

from gcmon.analysis.pyperformance import RUN_DURATION, Run, benchmark_names, compare_key, read_runs
from gcmon.analysis.trace_rows import ProcessRow
from gcmon.model.process import Process
from gcmon.support.vocabulary import ENCODING
from tests.helpers import proc

_MS: int = 1_000_000

# Quoted from a pyperformance capture of async_tree_io_tg on Windows: two
# measuring workers and the calibration worker before them.
_PREFIX: str = (
    r"X:\Work\cpython-gc-bench\venv\cpython3.16-2bcc84a85b9f-compat-31b33d68c68a\Scripts\python.exe "
    r"X:\Work\cpython-gc-bench\.ms-inc-gc\Lib\site-packages\pyperformance\data-files\benchmarks"
    r"\bm_async_tree\run_benchmark.py"
)
_WORKER_A: str = (
    f"{_PREFIX} --worker --pipe 588 --worker-task=0 --values 3 --min-time 0.1 --loops 1 --warmups 1 io --task-groups"
)
_WORKER_B: str = (
    f"{_PREFIX} --worker --pipe 604 --worker-task=0 --values 3 --min-time 0.1 --loops 1 --warmups 1 io --task-groups"
)
_CALIBRATOR: str = (
    f"{_PREFIX} --worker --pipe 572 --worker-task=0 --values 3 --min-time 0.1 --calibrate-loops --warmups 1 io "
    "--task-groups"
)
_MANAGER: str = r"X:\Work\cpython-gc-bench\.ms-inc-gc\Scripts\python.exe -u -m pyperformance run -o .\run.json"


def _span(process: Process, start_ms: int, stop_ms: int, cmdline: str | None = None) -> ProcessRow:
    return ProcessRow(process, cmdline, start_ms * _MS, (stop_ms - start_ms) * _MS)


def _run(name: str, start_ms: int, stop_ms: int) -> Run:
    return Run(name, start_ms * _MS, stop_ms * _MS)


class TestTheJoin:
    def test_a_run_goes_to_its_worker_over_the_manager_spanning_every_run(self) -> None:
        manager = _span(proc(1), 0, 1000, _MANAGER)
        first = _span(proc(2), 100, 400, _WORKER_A)
        second = _span(proc(3), 500, 800, _WORKER_B)

        names = benchmark_names([manager, first, second], [_run("one", 150, 350), _run("two", 550, 750)])

        assert names == {proc(2): "one", proc(3): "two"}

    def test_a_run_starting_before_its_workers_span_still_goes_to_it(self) -> None:
        # gcmon discovers a process at a poll, a few milliseconds after it starts.
        worker = _span(proc(2), 105, 400, _WORKER_A)
        previous = _span(proc(3), 0, 98, _WORKER_B)

        names = benchmark_names([previous, worker], [_run("bench", 100, 390)])

        assert names[proc(2)] == "bench"

    def test_a_missed_worker_takes_the_name_its_command_agrees_on(self) -> None:
        named = _span(proc(2), 100, 400, _WORKER_A)
        missed = _span(proc(3), 500, 800, _WORKER_B)

        names = benchmark_names([named, missed], [_run("bench", 150, 350)])

        assert names[proc(3)] == "bench"

    def test_a_missed_worker_whose_command_disagrees_gets_no_name(self) -> None:
        one = _span(proc(2), 100, 400, _WORKER_A)
        two = _span(proc(3), 500, 800, _WORKER_B)
        missed = _span(proc(4), 900, 1200, _WORKER_A.replace("--pipe 588", "--pipe 620"))

        names = benchmark_names([one, two, missed], [_run("one", 150, 350), _run("two", 550, 750)])

        assert proc(4) not in names

    def test_a_process_without_a_command_line_takes_no_borrowed_name(self) -> None:
        named = _span(proc(2), 100, 400, _WORKER_A)
        unread = _span(proc(3), 500, 800)

        assert proc(3) not in benchmark_names([named, unread], [_run("bench", 150, 350)])

    def test_two_benchmarks_of_one_script_keep_their_own_names(self) -> None:
        parse = _WORKER_A
        iterparse = _WORKER_A.replace("--worker-task=0", "--worker-task=1")
        spans = [
            _span(proc(2), 100, 400, parse),
            _span(proc(3), 500, 800, iterparse),
            _span(proc(4), 900, 1200, iterparse.replace("--pipe 588", "--pipe 640")),
        ]

        names = benchmark_names(spans, [_run("xml_etree_parse", 150, 350), _run("xml_etree_iterparse", 550, 750)])

        assert names == {proc(2): "xml_etree_parse", proc(3): "xml_etree_iterparse", proc(4): "xml_etree_iterparse"}

    def test_a_calibration_worker_is_named(self) -> None:
        calibrator = _span(proc(2), 0, 90, _CALIBRATOR)
        worker = _span(proc(3), 100, 400, _WORKER_A)

        names = benchmark_names([calibrator, worker], [_run("bench", 10, 80), _run("bench", 150, 350)])

        assert names == {proc(2): "bench", proc(3): "bench"}

    def test_a_run_no_process_overlaps_names_none(self) -> None:
        worker = _span(proc(2), 100, 400, _WORKER_A)

        assert benchmark_names([worker], [_run("one", 150, 350), _run("late", 500, 800)]) == {proc(2): "one"}

    def test_a_process_without_a_command_line_keeps_the_name_its_run_gives(self) -> None:
        unread = _span(proc(2), 100, 400)

        assert benchmark_names([unread], [_run("bench", 150, 350)]) == {proc(2): "bench"}


class TestTheCommandKey:
    def test_two_workers_of_one_benchmark_share_a_key(self) -> None:
        assert compare_key(_WORKER_A) == compare_key(_WORKER_B.replace("--loops 1", "--loops 4"))

    def test_the_worker_task_separates_two_keys(self) -> None:
        assert compare_key(_WORKER_A) != compare_key(_WORKER_A.replace("--worker-task=0", "--worker-task=1"))

    def test_the_calibration_worker_has_a_key_of_its_own(self) -> None:
        assert compare_key(_CALIBRATOR) != compare_key(_WORKER_A)


def _results(benchmarks: list[dict[str, Any]], metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"version": "1.0", "metadata": metadata or {}, "benchmarks": benchmarks}


def _run_entry(uptime: float, duration: float, **metadata: Any) -> dict[str, Any]:
    return {"metadata": {"uptime": uptime, RUN_DURATION: duration, **metadata}, "values": [1.0]}


class TestTheResultsFile:
    def test_a_one_benchmark_file_names_its_runs_from_the_top(self, tmp_path: Path) -> None:
        path = tmp_path / "run.json"
        document = _results([{"runs": [_run_entry(2.5, 2.0)]}], {"name": "async_tree_io_tg"})
        path.write_text(json.dumps(document), encoding=ENCODING)

        assert read_runs(path) == [Run("async_tree_io_tg", 500_000_000, 2_500_000_000)]

    def test_each_benchmark_names_its_own_runs(self, tmp_path: Path) -> None:
        path = tmp_path / "run.json"
        document = _results(
            [
                {"metadata": {"name": "xml_etree_parse"}, "runs": [_run_entry(2.0, 1.0)]},
                {"metadata": {"name": "xml_etree_iterparse"}, "runs": [_run_entry(4.0, 1.0)]},
            ],
            {"description": "XML benchmarks"},
        )
        path.write_text(json.dumps(document), encoding=ENCODING)

        assert [run.name for run in read_runs(path)] == ["xml_etree_parse", "xml_etree_iterparse"]

    def test_a_runs_own_metadata_wins(self, tmp_path: Path) -> None:
        path = tmp_path / "run.json"
        document = _results([{"metadata": {"name": "bench", RUN_DURATION: 9.0}, "runs": [_run_entry(3.0, 1.0)]}])
        path.write_text(json.dumps(document), encoding=ENCODING)

        assert read_runs(path) == [Run("bench", 2_000_000_000, 3_000_000_000)]

    def test_a_gzipped_file_reads_the_same(self, tmp_path: Path) -> None:
        document = _results([{"runs": [_run_entry(2.5, 2.0)]}], {"name": "bench"})
        plain = tmp_path / "run.json"
        plain.write_text(json.dumps(document), encoding=ENCODING)
        packed = tmp_path / "run.json.gz"
        packed.write_bytes(gzip.compress(json.dumps(document).encode(ENCODING)))

        assert read_runs(packed) == read_runs(plain)

    def test_a_run_without_timing_is_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "run.json"
        document = _results([{"runs": [{"metadata": {"date": "2026-10-25"}}, _run_entry(2.5, 2.0)]}], {"name": "b"})
        path.write_text(json.dumps(document), encoding=ENCODING)

        assert len(read_runs(path)) == 1
