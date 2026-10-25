"""A trace's records as a table, one row per record (spec 0076)."""

import csv
import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence, Set
from pathlib import Path
from typing import Any, Final

from ..model.names import GENERATION, IID, PAUSE, PID, PID_EPOCH, TS
from ..model.phases import PAUSE_ARGS
from ..model.process import Process
from ..support.vocabulary import ENCODING
from .pyperformance import run_metadata
from .trace_rows import SliceRow, TraceRows

__all__ = [
    "BENCHMARK",
    "CALIBRATION",
    "CALIBRATION_FLAGS",
    "COLUMNS",
    "META_KEYS",
    "META_SUFFIX",
    "record_meta",
    "record_rows",
    "write_csv",
    "write_meta",
]

BENCHMARK: Final = "benchmark"
CALIBRATION: Final = "calibration"

COLUMNS: Final[tuple[str, ...]] = (BENCHMARK, CALIBRATION, PID, PID_EPOCH, TS, *PAUSE_ARGS)

# pyperf's worker flags for a run that only finds the loops or warmups to use.
CALIBRATION_FLAGS: Final = frozenset(
    {"--calibrate-loops", "--recalibrate-loops", "--calibrate-warmups", "--recalibrate-warmups"}
)

type Cell = int | float | str | None


def _pause_process(slice_: SliceRow) -> Process | None:
    """The process a pause belongs to, from its track or, on a run row, its
    own `pid` and `pid_epoch`."""
    if slice_.process is not None:
        return slice_.process
    pid, pid_epoch = slice_.args.get(PID), slice_.args.get(PID_EPOCH)
    if isinstance(pid, int) and isinstance(pid_epoch, int):
        return Process(pid, pid_epoch)
    return None


def _calibrates(cmdline: str | None) -> int:
    return int(cmdline is not None and not CALIBRATION_FLAGS.isdisjoint(cmdline.split(" ")))


def record_rows(
    rows: TraceRows, benchmarks: Mapping[Process, str], only: Set[str] | None = None
) -> list[tuple[Cell, ...]]:
    """One row per record, in `COLUMNS` order, by process and then time.
    *benchmarks* names what each process ran; a process it lacks gets none.
    With *only*, a process whose benchmark is not in it gets no rows.

    A layout that draws a pause twice gives one row: copies share the
    process, `iid`, generation and start.
    """
    cmdlines = {row.process: row.cmdline for row in rows.processes}
    pauses: dict[tuple[Process, int, int, int], SliceRow] = {}
    for slice_ in rows.slices:
        gen, iid = slice_.args.get(GENERATION), slice_.args.get(IID)
        if not isinstance(gen, int) or not isinstance(iid, int) or slice_.name != PAUSE.names[gen][0]:
            continue
        process = _pause_process(slice_)
        if process is not None and (only is None or benchmarks.get(process) in only):
            pauses.setdefault((process, slice_.ts, iid, gen), slice_)
    table: list[tuple[Cell, ...]] = []
    for key in sorted(pauses):
        process, ts = key[0], key[1]
        args = pauses[key].args
        table.append(
            (
                benchmarks.get(process),
                _calibrates(cmdlines.get(process)),
                process.pid,
                process.pid_epoch,
                ts,
                *(args.get(name) for name in PAUSE_ARGS),
            )
        )
    return table


def write_csv(
    path: Path, table: Iterable[Sequence[Cell]], *, overwrite: bool, columns: Sequence[str] = COLUMNS
) -> None:
    """Write *table*'s *columns*, a subset of `COLUMNS` in its order, under a
    header naming them. Without *overwrite*, an existing *path* raises
    `FileExistsError` and is left as it was."""
    indices = [COLUMNS.index(name) for name in columns]
    with path.open("w" if overwrite else "x", encoding=ENCODING, newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        writer.writerows([row[index] for index in indices] for row in table)


# The sidecar beside a table: `run.csv` gets `run.meta.json`.
META_SUFFIX: Final = ".meta.json"

_TRACE: Final = "trace"
_TRACE_SHA256: Final = "trace_sha256"
_DATE: Final = "date"

# What the results file's own metadata says about the run.
_RESULTS_KEYS: Final = ("python_version", "performance_version", "perf_version")

META_KEYS: Final[tuple[str, ...]] = (_TRACE, _TRACE_SHA256, *_RESULTS_KEYS, _DATE)


def record_meta(trace: Path, results: Mapping[str, Any]) -> dict[str, Any]:
    """Where a table came from, under `META_KEYS`: the trace's name and hash,
    the versions the results file names, and the earliest run's date."""
    with trace.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    shared: Mapping[str, Any] = results.get("metadata") or {}
    dates = [metadata[_DATE] for metadata in run_metadata(results) if metadata.get(_DATE)]
    return {
        _TRACE: trace.name,
        _TRACE_SHA256: digest,
        **{key: shared.get(key) for key in _RESULTS_KEYS},
        _DATE: min(dates, default=None),
    }


def write_meta(path: Path, meta: Mapping[str, Any], *, overwrite: bool) -> None:
    """Write *meta* as JSON. Without *overwrite*, an existing *path* raises
    `FileExistsError` and is left as it was."""
    with path.open("w" if overwrite else "x", encoding=ENCODING) as handle:
        json.dump(meta, handle, indent=2)
        handle.write("\n")
