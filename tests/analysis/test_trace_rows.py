from pathlib import Path

import pytest

from gcmon.analysis.perfetto_prebuilt import MissingExtraError
from gcmon.analysis.trace_rows import SliceRow, TraceProcessorReader, TraceRows
from gcmon.exporters import PerfettoExporter
from gcmon.exporters.encoder import _CODEC, _DEFLATE, Codec, Layout
from gcmon.model.names import CMDLINE, COLLECTIONS, GENERATION, IID, PID, PID_EPOCH
from gcmon.model.phases import OldWorkField, ReasonFields
from gcmon.model.process import Process
from tests.helpers import (
    HAS_LIBZSTD,
    create_mock_incremental_item,
    create_mock_stats_item,
    gc_pause_slice_name,
    open_trace_processor,
    proc,
)

_PID: int = 4242

_CMDLINE: tuple[str, ...] = ("python", "-m", "bm_async_tree", "--worker")

_FIRST: Process = proc(_PID)

# The pid reissued to a second process, which no command line was read for.
_SECOND: Process = proc(_PID, pid_epoch=2)

_COLLECTIONS: int = 7

_OLD_WORK: int = 1234

_CODECS = [pytest.param(_DEFLATE, id="deflate")]
if HAS_LIBZSTD:
    _CODECS.append(pytest.param(_CODEC, id="zstd"))


def _write(path: Path, layout: Layout = Layout.FULL, codec: Codec = _CODEC) -> Path:
    exporter = PerfettoExporter(output_path=path, flush_threshold=1, codec=codec, layout=layout)
    exporter.add_process_cmdline(_FIRST, _CMDLINE)
    exporter.add_event(_FIRST, create_mock_stats_item(gen=0, collections=_COLLECTIONS))
    exporter.add_event(
        _FIRST,
        create_mock_incremental_item(gen=1, iid=1, old_work=_OLD_WORK, ts_start=3_000_000, ts_stop=3_500_000),
    )
    exporter.add_event(_SECOND, create_mock_stats_item(gen=0, ts_start=9_000_000, ts_stop=9_500_000))
    exporter.close()
    return path


def _pauses(rows: TraceRows, gen: int) -> list[SliceRow]:
    return [row for row in rows.slices if row.name == gc_pause_slice_name(gen)]


@pytest.mark.parametrize("codec", _CODECS)
def test_every_slice_the_trace_holds_reads_back(tmp_path: Path, codec: Codec) -> None:
    path = _write(tmp_path / "run.pftrace", codec=codec)

    rows = TraceProcessorReader().read(path)

    with open_trace_processor(path) as tp:
        held = next(iter(tp.query("SELECT COUNT(*) AS n FROM slice"))).n
    assert held > 0
    assert len(rows.slices) == held


def test_each_process_reads_back_from_its_span(tmp_path: Path) -> None:
    rows = TraceProcessorReader().read(_write(tmp_path / "run.pftrace"))

    by_process = {row.process: row for row in rows.processes}
    assert set(by_process) == {_FIRST, _SECOND}
    assert by_process[_FIRST].cmdline == " ".join(_CMDLINE)
    assert by_process[_SECOND].cmdline is None


def test_a_pause_reads_back_with_its_arguments_keyed(tmp_path: Path) -> None:
    rows = TraceProcessorReader().read(_write(tmp_path / "run.pftrace"))

    (pause, *_) = [row for row in _pauses(rows, 1) if row.process == _FIRST]
    assert pause.args[GENERATION] == 1
    assert pause.args[IID] == 1
    assert pause.args[OldWorkField.OLD_WORK] == _OLD_WORK
    assert isinstance(pause.args[ReasonFields.REASON], int)
    assert pause.ts == 3_000_000
    assert pause.dur == 500_000


def test_the_full_layout_draws_a_pause_on_both_rows(tmp_path: Path) -> None:
    rows = TraceProcessorReader().read(_write(tmp_path / "run.pftrace", Layout.FULL))

    first_gen0 = [row for row in _pauses(rows, 0) if row.args[COLLECTIONS] == _COLLECTIONS]
    on_interpreter = [row for row in first_gen0 if row.process is not None]
    on_run_row = [row for row in first_gen0 if row.process is None]
    assert [row.process for row in on_interpreter] == [_FIRST]
    assert [(row.args[PID], row.args[PID_EPOCH]) for row in on_run_row] == [(_PID, 1)]


def test_the_process_layout_draws_a_pause_once_under_its_process(tmp_path: Path) -> None:
    rows = TraceProcessorReader().read(_write(tmp_path / "run.pftrace", Layout.PROCESS))

    assert sorted((row.process, row.args[COLLECTIONS]) for row in _pauses(rows, 0)) == sorted(
        [(_FIRST, _COLLECTIONS), (_SECOND, create_mock_stats_item().collections)]
    )


def test_a_reused_pid_keeps_its_two_processes_apart(tmp_path: Path) -> None:
    rows = TraceProcessorReader().read(_write(tmp_path / "run.pftrace", Layout.PROCESS))

    assert {row.process for row in _pauses(rows, 0)} == {_FIRST, _SECOND}
    assert all(CMDLINE not in row.args for row in _pauses(rows, 0))


def test_without_the_extra_opening_a_trace_fails_naming_it(tmp_path: Path, without_perfetto: None) -> None:
    path = _write(tmp_path / "run.pftrace")

    with pytest.raises(MissingExtraError, match=r"gcmon\[analysis\]"):
        TraceProcessorReader().read(path)
