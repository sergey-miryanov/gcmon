"""A trace gcmon wrote, read back as the rows it holds: spec 0061's lower seam.

The rows keep the trace's own shape, so a pause the layout draws twice is two
rows. What they mean is the caller's to decide.
"""

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Final, NamedTuple, Protocol

from ..exporters.perfetto_process_lifetime import process_track_name
from ..model.names import CMDLINE, PID, PID_EPOCH
from ..model.process import Process
from .perfetto_prebuilt import MissingExtraError, trace_processor_bin

__all__ = ["ProcessRow", "SliceRow", "TraceProcessorReader", "TraceReader", "TraceRows"]

type ArgValue = int | float | str

# Every annotation gcmon writes is a debug annotation, filed under this.
_ARG_PREFIX: Final = "debug."

# Starting the shell and loading a suite's trace can take minutes.
_LOAD_TIMEOUT_S: Final = 300

_SLICES_QUERY: Final = """
SELECT
    s.id AS id,
    s.ts AS ts,
    s.dur AS dur,
    s.name AS name,
    s.category AS category,
    t.name AS track,
    p.name AS process,
    a.key AS key,
    a.value_type AS value_type,
    a.int_value AS int_value,
    a.real_value AS real_value,
    a.string_value AS string_value
FROM slice s
JOIN track t ON s.track_id = t.id
LEFT JOIN process_track pt ON pt.id = s.track_id
LEFT JOIN process p ON p.upid = pt.upid
LEFT JOIN args a ON a.arg_set_id = s.arg_set_id
ORDER BY s.id
"""


class ProcessRow(NamedTuple):
    """A process, as its span on the `Processes` row carries it."""

    process: Process
    cmdline: str | None
    ts: int
    dur: int | None


class SliceRow(NamedTuple):
    """One slice where it is drawn. *process* is the process its track parents
    to, and None on a row no process holds. *dur* is None for a slice the
    trace never closed."""

    track: str
    category: str | None
    name: str
    ts: int
    dur: int | None
    args: Mapping[str, ArgValue]
    process: Process | None


class TraceRows(NamedTuple):
    processes: list[ProcessRow]
    slices: list[SliceRow]


class TraceReader(Protocol):
    def read(self, path: Path) -> TraceRows: ...


class TraceProcessorReader:
    """`TraceReader` over the pinned trace processor."""

    def read(self, path: Path) -> TraceRows:
        try:
            from perfetto.trace_processor import TraceProcessor, TraceProcessorConfig
        except ImportError as error:
            raise MissingExtraError from error
        config = TraceProcessorConfig(bin_path=trace_processor_bin(), load_timeout=_LOAD_TIMEOUT_S)
        tp = TraceProcessor(trace=str(path), config=config)
        try:
            drawn = _read_slices(tp.query(_SLICES_QUERY))
        finally:
            tp.close()
        return _resolve(drawn)


class _Drawn(NamedTuple):
    """A slice before its track's process name is resolved to a `Process`."""

    track: str
    category: str | None
    name: str
    ts: int
    dur: int | None
    args: dict[str, ArgValue]
    process_name: str | None


def _read_slices(result: Iterable[Any]) -> list[_Drawn]:
    drawn: list[_Drawn] = []
    last_id = None
    for row in result:
        if row.id != last_id:
            last_id = row.id
            dur = None if row.dur is None or row.dur < 0 else row.dur
            drawn.append(_Drawn(row.track, row.category, row.name, row.ts, dur, {}, row.process))
        if row.key is not None:
            drawn[-1].args[row.key.removeprefix(_ARG_PREFIX)] = _value(row)
    return drawn


def _value(row: Any) -> ArgValue:
    value: ArgValue
    if row.value_type == "real":
        value = row.real_value
    elif row.value_type == "string":
        value = row.string_value
    else:
        value = row.int_value
    return value


def _span_process(slice_: _Drawn) -> Process | None:
    """The process *slice_* is the span of, or None if it is no span.

    A span sits on a row no process holds, carries the pid and its epoch, and
    is named after the process they make.
    """
    if slice_.process_name is not None:
        return None
    pid, pid_epoch = slice_.args.get(PID), slice_.args.get(PID_EPOCH)
    if not isinstance(pid, int) or not isinstance(pid_epoch, int):
        return None
    process = Process(pid, pid_epoch)
    return process if slice_.name == process_track_name(process) else None


def _resolve(drawn: list[_Drawn]) -> TraceRows:
    processes: list[ProcessRow] = []
    by_name: dict[str, Process] = {}
    for slice_ in drawn:
        process = _span_process(slice_)
        if process is None:
            continue
        cmdline = slice_.args.get(CMDLINE)
        processes.append(ProcessRow(process, cmdline if isinstance(cmdline, str) else None, slice_.ts, slice_.dur))
        by_name[slice_.name] = process
    slices = [
        SliceRow(
            slice_.track,
            slice_.category,
            slice_.name,
            slice_.ts,
            slice_.dur,
            slice_.args,
            None if slice_.process_name is None else by_name.get(slice_.process_name),
        )
        for slice_ in drawn
    ]
    return TraceRows(processes, slices)
