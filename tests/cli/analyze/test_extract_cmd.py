"""Tests for `gcmon extract`: a trace's records as a CSV table (spec 0076).

The command runs in-process over a trace written in the test, and the table is
read back with `csv.DictReader`, as a loader outside gcmon reads it.
"""

import csv
import hashlib
import json
from pathlib import Path
from typing import override

import pytest

from gcmon.analysis.extract import BENCHMARK, CALIBRATION, COLUMNS
from gcmon.analysis.pyperformance import RUN_DURATION
from gcmon.analysis.trace_rows import TraceProcessorReader, TraceRows
from gcmon.cli.analyze import extract_cmd
from gcmon.cli.main import main
from gcmon.exporters import PerfettoExporter
from gcmon.exporters.encoder import Layout
from gcmon.model.data import GCStatsInfo
from gcmon.model.names import COLLECTED, COLLECTIONS, GENERATION, IID, PID, PID_EPOCH, RSS, TS
from gcmon.model.phases import PAUSE_ARGS, OldWorkField
from gcmon.model.process import Process
from gcmon.support.vocabulary import CMD_COMBINE, CMD_EXTRACT, ENCODING
from tests.helpers import create_jsonl_record, create_mock_incremental_item, create_mock_stats_item, proc

_WORKER: Process = proc(588)

_BENCHMARK: str = "async_tree_io_tg"

# Quoted from a pyperformance capture of async_tree_io_tg on Windows.
_WORKER_CMDLINE: str = (
    r"X:\Work\cpython-gc-bench\venv\cpython3.16-2bcc84a85b9f-compat-31b33d68c68a\Scripts\python.exe "
    r"X:\Work\cpython-gc-bench\.ms-inc-gc\Lib\site-packages\pyperformance\data-files\benchmarks"
    r"\bm_async_tree\run_benchmark.py --worker --pipe 588 --worker-task=0 --values 3 --min-time 0.1 "
    r"--loops 1 --warmups 1 io --task-groups"
)

_CALIBRATION_CMDLINE: str = (
    r"X:\Work\cpython-gc-bench\venv\cpython3.16-2bcc84a85b9f-compat-31b33d68c68a\Scripts\python.exe "
    r"X:\Work\cpython-gc-bench\.ms-inc-gc\Lib\site-packages\pyperformance\data-files\benchmarks"
    r"\bm_async_tree\run_benchmark.py --worker --pipe 572 --worker-task=0 --values 3 --min-time 0.1 "
    r"--calibrate-loops --warmups 1 io --task-groups"
)

# pyperf's other three calibration flags, in the captured calibration line's
# place: the capture holds only `--calibrate-loops`.
_CALIBRATION_FLAGS: tuple[str, ...] = (
    "--calibrate-loops",
    "--recalibrate-loops",
    "--calibrate-warmups",
    "--recalibrate-warmups",
)


def _records() -> list[GCStatsInfo]:
    """Three collections of one interpreter's generation 0, then one of
    generation 1 carrying every field."""
    gen0 = [
        create_mock_stats_item(
            gen=0,
            collections=n,
            collected=10 * n,
            candidates=20 * n,
            ts_start=1_000_000 * n,
            ts_stop=1_000_000 * n + 400_000,
        )
        for n in (1, 2, 3)
    ]
    full = create_mock_incremental_item(gen=1, collections=1, ts_start=5_000_000, ts_stop=5_600_000)
    return [*gen0, full]


# The worker's one RSS sample, taken before its last record.
_RSS_BYTES: int = 48_000_000


def _write_trace(path: Path, layout: Layout = Layout.FULL, cmdline: str = _WORKER_CMDLINE) -> Path:
    """The worker's trace, with the results file pyperformance writes beside
    it: one run spanning the worker's records."""
    exporter = PerfettoExporter(output_path=path, layout=layout)
    exporter.add_process_cmdline(_WORKER, tuple(cmdline.split(" ")))
    *before, last = _records()
    for record in before:
        exporter.add_event(_WORKER, record)
    exporter.add_rss_sample(_WORKER, _RSS_BYTES, last.ts_start - 100_000)
    exporter.add_event(_WORKER, last)
    exporter.close()
    _write_results(path.with_suffix(".json"))
    return path


_PYTHON_VERSION: str = "3.16.0a0 (64-bit) revision 5542f2021c2"

_EARLIEST: str = "2026-10-25 02:50:08.772309"


def _write_results(path: Path, name: str = _BENCHMARK) -> Path:
    """One run spanning the worker's records, and a calibration run dated
    before it, as pyperf writes them."""
    first, last = _records()[0], _records()[-1]
    run = {
        "metadata": {
            "uptime": last.ts_stop / 1e9,
            RUN_DURATION: (last.ts_stop - first.ts_start) / 1e9,
            "date": "2026-10-25 02:50:11.132826",
        }
    }
    calibration = {"metadata": {"date": _EARLIEST, "calibrate_loops": 1}}
    metadata = {
        "name": name,
        "python_version": _PYTHON_VERSION,
        "performance_version": "1.14.0",
        "perf_version": "2.10.0",
    }
    document = {"version": "1.0", "metadata": metadata, "benchmarks": [{"runs": [calibration, run]}]}
    path.write_text(json.dumps(document), encoding=ENCODING)
    return path


def _extract(*argv: str) -> int:
    return main([CMD_EXTRACT, *argv])


def _read(path: Path) -> list[dict[str, str]]:
    with path.open(encoding=ENCODING, newline="") as handle:
        return list(csv.DictReader(handle))


def _cell(value: object) -> str:
    return "" if value is None else str(value)


@pytest.mark.parametrize("layout", [Layout.FULL, Layout.PROCESS], ids=lambda layout: layout.value)
def test_every_record_reads_back_as_one_row(tmp_path: Path, layout: Layout) -> None:
    trace = _write_trace(tmp_path / "run.pftrace", layout)

    assert _extract(str(trace)) == 0

    rows = _read(tmp_path / "run.csv")
    expected = [
        {name: _cell(value) for name, value in zip(PAUSE_ARGS, _pause_args(record), strict=True)}
        for record in _records()
    ]
    assert [{name: row[name] for name in PAUSE_ARGS} for row in rows] == expected
    assert {(row[PID], row[PID_EPOCH]) for row in rows} == {(str(_WORKER.pid), str(_WORKER.pid_epoch))}
    assert [int(row[TS]) for row in rows] == [record.ts_start for record in _records()]


def _pause_args(record: GCStatsInfo) -> list[object]:
    """What the record holds under each of `PAUSE_ARGS`, `generation` from `gen`."""
    return [record.gen if name == GENERATION else getattr(record, name) for name in PAUSE_ARGS]


def test_a_stock_record_leaves_the_incremental_fields_empty(tmp_path: Path) -> None:
    trace = _write_trace(tmp_path / "run.pftrace")

    _extract(str(trace))

    (stock, *_) = _read(tmp_path / "run.csv")
    assert list(stock) == list(COLUMNS)
    assert stock[OldWorkField.OLD_WORK] == ""
    assert stock[COLLECTIONS] == "1"


def test_the_counters_stay_lifetime_totals(tmp_path: Path) -> None:
    trace = _write_trace(tmp_path / "run.pftrace")

    _extract(str(trace))

    gen0 = [row for row in _read(tmp_path / "run.csv") if row[GENERATION] == "0" and row[IID] == "0"]
    assert [row[COLLECTED] for row in gen0] == ["10", "20", "30"]


def test_a_record_carrying_every_field_fills_every_column(tmp_path: Path) -> None:
    trace = _write_trace(tmp_path / "run.pftrace")

    _extract(str(trace))

    full = _read(tmp_path / "run.csv")[-1]
    assert list(full) == list(COLUMNS)
    assert [name for name, value in full.items() if value == ""] == []


@pytest.mark.parametrize("layout", [Layout.FULL, Layout.PROCESS], ids=lambda layout: layout.value)
def test_a_record_carries_the_rss_sample_before_it(tmp_path: Path, layout: Layout) -> None:
    trace = _write_trace(tmp_path / "run.pftrace", layout)

    _extract(str(trace))

    assert [row[RSS] for row in _read(tmp_path / "run.csv")] == ["", "", "", str(_RSS_BYTES)]


def test_each_row_names_the_benchmark_from_the_results_beside_the_trace(tmp_path: Path) -> None:
    trace = _write_trace(tmp_path / "run.pftrace")

    assert _extract(str(trace)) == 0

    assert {row[BENCHMARK] for row in _read(tmp_path / "run.csv")} == {_BENCHMARK}


def test_a_results_file_named_on_the_command_line_is_read(tmp_path: Path) -> None:
    trace = _write_trace(tmp_path / "run.pftrace")
    results = _write_results(tmp_path / "suite.json", name="another")

    assert _extract(str(trace), "--results", str(results)) == 0

    assert {row[BENCHMARK] for row in _read(tmp_path / "run.csv")} == {"another"}


@pytest.mark.parametrize("named", [False, True], ids=["inferred", "named"])
def test_a_missing_results_file_stops_the_command(
    tmp_path: Path, named: bool, caplog: pytest.LogCaptureFixture
) -> None:
    trace = _write_trace(tmp_path / "run.pftrace")
    trace.with_suffix(".json").unlink()
    argv = [str(trace), "--results", str(tmp_path / "absent.json")] if named else [str(trace)]

    assert _extract(*argv) != 0

    assert "--results" in caplog.text
    assert not (tmp_path / "run.csv").exists()


def test_an_unreadable_results_file_stops_the_command(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    trace = _write_trace(tmp_path / "run.pftrace")
    trace.with_suffix(".json").write_text("{not json", encoding=ENCODING)

    assert _extract(str(trace)) != 0

    assert "run.json" in caplog.text
    assert not (tmp_path / "run.csv").exists()


@pytest.mark.parametrize("flag", _CALIBRATION_FLAGS)
def test_a_calibration_flag_marks_the_worker(tmp_path: Path, flag: str) -> None:
    cmdline = _CALIBRATION_CMDLINE.replace("--calibrate-loops", flag)
    trace = _write_trace(tmp_path / "run.pftrace", cmdline=cmdline)

    _extract(str(trace))

    assert {row[CALIBRATION] for row in _read(tmp_path / "run.csv")} == {"1"}


def test_a_measuring_worker_is_not_marked(tmp_path: Path) -> None:
    trace = _write_trace(tmp_path / "run.pftrace")

    _extract(str(trace))

    assert {row[CALIBRATION] for row in _read(tmp_path / "run.csv")} == {"0"}


def test_without_the_extra_extract_fails_naming_it(
    tmp_path: Path, without_perfetto: None, caplog: pytest.LogCaptureFixture
) -> None:
    trace = _write_trace(tmp_path / "run.pftrace")

    assert _extract(str(trace)) != 0
    assert "gcmon[analysis]" in caplog.text
    assert not (tmp_path / "run.csv").exists()


def test_without_the_extra_combine_still_runs(tmp_path: Path, without_perfetto: None) -> None:
    capture = tmp_path / "run.jsonl"
    capture.write_text(json.dumps(create_jsonl_record()) + "\n", encoding=ENCODING)

    assert main([CMD_COMBINE, str(capture), "-o", str(tmp_path / "out.pftrace")]) == 0


def test_the_output_is_named_from_the_trace(tmp_path: Path) -> None:
    trace = _write_trace(tmp_path / "run.pftrace")

    assert _extract(str(trace)) == 0

    assert (tmp_path / "run.csv").exists()


def test_an_output_named_on_the_command_line_is_written_there(tmp_path: Path) -> None:
    trace = _write_trace(tmp_path / "run.pftrace")
    output = tmp_path / "records.csv"

    assert _extract(str(trace), "-o", str(output)) == 0

    assert len(_read(output)) == len(_records())


def test_an_existing_output_stops_the_command_and_is_left_alone(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    trace = _write_trace(tmp_path / "run.pftrace")
    output = tmp_path / "run.csv"
    output.write_text("kept\n", encoding=ENCODING)

    assert _extract(str(trace)) != 0

    assert output.read_text(encoding=ENCODING) == "kept\n"
    assert "--overwrite" in caplog.text


def test_an_output_written_while_the_trace_is_read_is_left_alone(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    trace = _write_trace(tmp_path / "run.pftrace")
    output = tmp_path / "run.csv"

    class _RacedReader(TraceProcessorReader):
        @override
        def read(self, path: Path) -> TraceRows:
            output.write_text("kept\n", encoding=ENCODING)
            return super().read(path)

    monkeypatch.setattr(extract_cmd, "TraceProcessorReader", _RacedReader)

    assert _extract(str(trace)) != 0

    assert output.read_text(encoding=ENCODING) == "kept\n"
    assert not (tmp_path / "run.meta.json").exists()
    assert "--overwrite" in caplog.text


def test_overwrite_replaces_an_existing_output(tmp_path: Path) -> None:
    trace = _write_trace(tmp_path / "run.pftrace")
    output = tmp_path / "run.csv"
    output.write_text("kept\n", encoding=ENCODING)

    assert _extract(str(trace), "--overwrite") == 0

    assert len(_read(output)) == len(_records())


def test_a_missing_trace_stops_the_command(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    assert _extract(str(tmp_path / "absent.pftrace")) != 0

    assert "absent.pftrace" in caplog.text
    assert not (tmp_path / "absent.csv").exists()


_OTHER: Process = proc(604)


def _write_two_benchmarks(path: Path) -> Path:
    """Two workers, each running one benchmark, and the results naming both."""
    exporter = PerfettoExporter(output_path=path)
    exporter.add_process_cmdline(_WORKER, tuple(_WORKER_CMDLINE.split(" ")))
    exporter.add_process_cmdline(_OTHER, tuple(_WORKER_CMDLINE.replace("588", "604").split(" ")))
    spans = {"first": (_WORKER, 1_000_000), "second": (_OTHER, 20_000_000)}
    benchmarks: list[dict[str, object]] = []
    for name, (process, start) in spans.items():
        for n in (0, 1):
            exporter.add_event(
                process,
                create_mock_stats_item(
                    gen=0, collections=n + 1, ts_start=start + n * 1_000_000, ts_stop=start + n * 1_000_000 + 400_000
                ),
            )
        run = {"metadata": {"uptime": (start + 1_400_000) / 1e9, RUN_DURATION: 1_400_000 / 1e9}}
        benchmarks.append({"metadata": {"name": name}, "runs": [run]})
    exporter.close()
    document = {"version": "1.0", "benchmarks": benchmarks}
    path.with_suffix(".json").write_text(json.dumps(document), encoding=ENCODING)
    return path


def test_benchmark_keeps_only_its_rows(tmp_path: Path) -> None:
    trace = _write_two_benchmarks(tmp_path / "run.pftrace")

    assert _extract(str(trace), "--benchmark", "second") == 0

    rows = _read(tmp_path / "run.csv")
    assert {row[BENCHMARK] for row in rows} == {"second"}
    assert {row[PID] for row in rows} == {str(_OTHER.pid)}


def test_two_benchmark_flags_keep_both(tmp_path: Path) -> None:
    trace = _write_two_benchmarks(tmp_path / "run.pftrace")

    assert _extract(str(trace), "--benchmark", "first", "--benchmark", "second") == 0

    assert {row[BENCHMARK] for row in _read(tmp_path / "run.csv")} == {"first", "second"}


def test_without_benchmark_every_process_has_rows(tmp_path: Path) -> None:
    trace = _write_two_benchmarks(tmp_path / "run.pftrace")

    assert _extract(str(trace)) == 0

    assert {row[PID] for row in _read(tmp_path / "run.csv")} == {str(_WORKER.pid), str(_OTHER.pid)}


def test_an_unknown_benchmark_stops_the_command_naming_it(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    trace = _write_two_benchmarks(tmp_path / "run.pftrace")

    assert _extract(str(trace), "--benchmark", "second", "--benchmark", "third") != 0

    assert "third" in caplog.text
    assert not (tmp_path / "run.csv").exists()


def _meta(path: Path) -> dict[str, object]:
    document: dict[str, object] = json.loads(path.read_text(encoding=ENCODING))
    return document


def test_the_sidecar_says_where_the_table_came_from(tmp_path: Path) -> None:
    trace = _write_trace(tmp_path / "run.pftrace")

    assert _extract(str(trace)) == 0

    assert _meta(tmp_path / "run.meta.json") == {
        "trace": "run.pftrace",
        "trace_sha256": hashlib.sha256(trace.read_bytes()).hexdigest(),
        "python_version": _PYTHON_VERSION,
        "performance_version": "1.14.0",
        "perf_version": "2.10.0",
        "date": _EARLIEST,
    }


def test_the_sidecar_follows_a_named_output(tmp_path: Path) -> None:
    trace = _write_trace(tmp_path / "run.pftrace")

    assert _extract(str(trace), "-o", str(tmp_path / "records.csv")) == 0

    assert (tmp_path / "records.meta.json").exists()


def test_meta_adds_a_key(tmp_path: Path) -> None:
    trace = _write_trace(tmp_path / "run.pftrace")

    assert _extract(str(trace), "--meta", "gcmon_version=0.9.1", "--meta", "note=a=b") == 0

    meta = _meta(tmp_path / "run.meta.json")
    assert meta["gcmon_version"] == "0.9.1"
    assert meta["note"] == "a=b"


def test_a_meta_key_colliding_with_a_derived_one_stops_the_command(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    trace = _write_trace(tmp_path / "run.pftrace")

    assert _extract(str(trace), "--meta", "trace_sha256=0") != 0

    assert "trace_sha256" in caplog.text
    assert not (tmp_path / "run.csv").exists()
    assert not (tmp_path / "run.meta.json").exists()


def test_meta_without_a_value_is_refused(tmp_path: Path) -> None:
    trace = _write_trace(tmp_path / "run.pftrace")

    with pytest.raises(SystemExit):
        _extract(str(trace), "--meta", "gcmon_version")


def test_an_existing_sidecar_stops_the_command_and_both_are_left_alone(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    trace = _write_trace(tmp_path / "run.pftrace")
    sidecar = tmp_path / "run.meta.json"
    sidecar.write_text("kept\n", encoding=ENCODING)

    assert _extract(str(trace)) != 0

    assert sidecar.read_text(encoding=ENCODING) == "kept\n"
    assert not (tmp_path / "run.csv").exists()
    assert "--overwrite" in caplog.text


def test_overwrite_replaces_the_table_and_its_sidecar(tmp_path: Path) -> None:
    trace = _write_trace(tmp_path / "run.pftrace")
    (tmp_path / "run.csv").write_text("kept\n", encoding=ENCODING)
    (tmp_path / "run.meta.json").write_text("kept\n", encoding=ENCODING)

    assert _extract(str(trace), "--overwrite") == 0

    assert len(_read(tmp_path / "run.csv")) == len(_records())
    assert _meta(tmp_path / "run.meta.json")["trace"] == "run.pftrace"


def _header(path: Path) -> list[str]:
    with path.open(encoding=ENCODING, newline="") as handle:
        return next(csv.reader(handle))


def test_drop_removes_its_columns_and_keeps_the_rest_in_order(tmp_path: Path) -> None:
    trace = _write_trace(tmp_path / "run.pftrace")

    assert _extract(str(trace), "--drop", COLLECTED, "--drop", BENCHMARK) == 0

    assert _header(tmp_path / "run.csv") == [name for name in COLUMNS if name not in {COLLECTED, BENCHMARK}]


def test_keep_writes_only_its_columns_in_their_table_order(tmp_path: Path) -> None:
    trace = _write_trace(tmp_path / "run.pftrace")

    assert _extract(str(trace), "--keep", COLLECTED, "--keep", PID, "--keep", BENCHMARK) == 0

    rows = _read(tmp_path / "run.csv")
    assert _header(tmp_path / "run.csv") == [BENCHMARK, PID, COLLECTED]
    assert [row[COLLECTED] for row in rows] == [str(record.collected) for record in _records()]


def test_drop_and_keep_together_are_refused(tmp_path: Path) -> None:
    trace = _write_trace(tmp_path / "run.pftrace")

    with pytest.raises(SystemExit):
        _extract(str(trace), "--drop", COLLECTED, "--keep", PID)


@pytest.mark.parametrize("flag", ["--drop", "--keep"])
def test_an_unknown_column_stops_the_command_naming_it(
    tmp_path: Path, flag: str, caplog: pytest.LogCaptureFixture
) -> None:
    trace = _write_trace(tmp_path / "run.pftrace")

    assert _extract(str(trace), flag, "no_such_column") != 0

    assert "no_such_column" in caplog.text
    assert not (tmp_path / "run.csv").exists()


def test_dropping_every_column_stops_the_command(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    trace = _write_trace(tmp_path / "run.pftrace")
    argv = [str(trace)]
    for name in COLUMNS:
        argv += ["--drop", name]

    assert _extract(*argv) != 0

    assert not (tmp_path / "run.csv").exists()
    assert not (tmp_path / "run.meta.json").exists()


def test_keep_takes_a_comma_separated_list(tmp_path: Path) -> None:
    trace = _write_trace(tmp_path / "run.pftrace")

    assert _extract(str(trace), "--keep", f"{COLLECTED},{PID}", "--keep", BENCHMARK) == 0

    assert _header(tmp_path / "run.csv") == [BENCHMARK, PID, COLLECTED]


def test_drop_takes_a_comma_separated_list(tmp_path: Path) -> None:
    trace = _write_trace(tmp_path / "run.pftrace")

    assert _extract(str(trace), "--drop", f"{COLLECTED},{BENCHMARK}") == 0

    assert _header(tmp_path / "run.csv") == [name for name in COLUMNS if name not in {COLLECTED, BENCHMARK}]


def test_an_empty_name_in_a_list_is_refused(tmp_path: Path) -> None:
    trace = _write_trace(tmp_path / "run.pftrace")

    with pytest.raises(SystemExit):
        _extract(str(trace), "--keep", f"{COLLECTED},,{PID}")
