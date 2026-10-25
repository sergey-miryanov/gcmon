"""pyperformance's results file, and which benchmark each process in a trace
ran by its overlap in time with the runs it holds (ADR-0030)."""

import bisect
import gzip
import json
import re
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Final, NamedTuple

from ..model.process import Process
from ..support.vocabulary import ENCODING
from .trace_rows import ProcessRow

__all__ = [
    "RUN_DURATION",
    "Run",
    "benchmark_names",
    "compare_key",
    "read_results",
    "read_runs",
    "run_metadata",
    "runs_of",
]

_NS_PER_S: Final = 1_000_000_000

# pyperf's run metadata key: how long the run took, in seconds.
RUN_DURATION: Final = "duration"

_GZIP_SUFFIX: Final = ".gz"

# The handles the OS hands out fresh on every run. Named one at a time: a rule
# that blanked every number would erase `--loops` too.
_HANDLES: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (re.compile(r"(?i)(--pipe[ =])\d+"), r"\1<N>"),
    (re.compile(r"(?i)(parent_pid=)\d+"), r"\1<N>"),
    (re.compile(r"(?i)(pipe_handle=)\d+"), r"\1<N>"),
)

# Calibration picks it per worker, so two workers of one benchmark can differ.
_LOOPS: Final = re.compile(r"(?i) --loops[ =]\d+")

_INTERPRETERS: Final = frozenset({"python", "python.exe", "python3", "python3.exe"})


class Run(NamedTuple):
    """One run from a results file, in trace nanoseconds."""

    name: str
    start_ns: int
    end_ns: int


def read_results(path: Path) -> dict[str, Any]:
    """The results file at *path*, read through `gzip` where it ends `.gz`."""
    if path.suffix == _GZIP_SUFFIX:
        with gzip.open(path, "rt", encoding=ENCODING) as handle:
            document: dict[str, Any] = json.load(handle)
    else:
        document = json.loads(path.read_text(encoding=ENCODING))
    return document


def run_metadata(document: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
    """Each run's metadata in a results file: the file's, overlaid by its
    benchmark's, overlaid by its own (ADR-0030)."""
    shared: Mapping[str, Any] = document.get("metadata") or {}
    for benchmark in document.get("benchmarks", ()):
        inherited = {**shared, **(benchmark.get("metadata") or {})}
        for run in benchmark.get("runs", ()):
            yield {**inherited, **(run.get("metadata") or {})}


def runs_of(document: Mapping[str, Any]) -> list[Run]:
    """Every run in a results file that names its benchmark and says when it
    ran. `uptime` is read once the run is over (ADR-0030)."""
    runs: list[Run] = []
    for metadata in run_metadata(document):
        name, uptime, duration = metadata.get("name"), metadata.get("uptime"), metadata.get(RUN_DURATION)
        if name is None or uptime is None or duration is None:
            continue
        end_ns = round(uptime * _NS_PER_S)
        runs.append(Run(name, end_ns - round(duration * _NS_PER_S), end_ns))
    return runs


def read_runs(path: Path) -> list[Run]:
    """`runs_of` the results file at *path*."""
    return runs_of(read_results(path))


def _short(token: str) -> str:
    """A path down to its last two segments, an interpreter down to `python`."""
    if "/" not in token and "\\" not in token:
        return token
    parts = re.split(r"[\\/]", token)
    if parts[-1].lower() in _INTERPRETERS:
        return "python"
    return "/".join(parts[-2:])


def compare_key(cmdline: str) -> str:
    """What two processes of one benchmark's command share.

    Install paths, OS handles and `--loops` drop out. `--worker-task` stays:
    it is all that tells apart two benchmarks one script runs.
    """
    joined = " ".join(_short(token) for token in cmdline.split(" "))
    for pattern, repl in _HANDLES:
        joined = pattern.sub(repl, joined)
    return _LOOPS.sub("", joined).strip()


def _overlap_names(processes: Sequence[ProcessRow], runs: Sequence[Run]) -> dict[Process, str]:
    """Each run's benchmark, given to the process whose span shares the largest
    fraction of the time either of them covers (ADR-0030)."""
    ordered = sorted(processes, key=lambda row: row.ts)
    starts = [row.ts for row in ordered]
    # How far the spans up to each one reach between them: past it, nothing
    # earlier touches the run.
    reach: list[int] = []
    for row in ordered:
        reach.append(max(reach[-1] if reach else 0, row.ts + (row.dur or 0)))
    named: dict[Process, str] = {}
    for run in runs:
        found = None
        best = 0.0
        for index in range(bisect.bisect_right(starts, run.end_ns) - 1, -1, -1):
            if reach[index] <= run.start_ns:
                break
            row = ordered[index]
            close = row.ts + (row.dur or 0)
            shared = min(run.end_ns, close) - max(run.start_ns, row.ts)
            if shared <= 0:
                continue
            score = shared / (max(run.end_ns, close) - min(run.start_ns, row.ts))
            if score > best:
                found, best = row.process, score
        if found is not None:
            named.setdefault(found, run.name)
    return named


def benchmark_names(processes: Sequence[ProcessRow], runs: Sequence[Run]) -> dict[Process, str]:
    """The benchmark each process ran, where the results say.

    A process no run overlaps takes the name the other processes of its
    command, by `compare_key`, agree on. One with no command line, or whose
    command's processes disagree, gets none.
    """
    names = _overlap_names(processes, runs)
    keys = {row.process: compare_key(row.cmdline) for row in processes if row.cmdline is not None}
    agreed: dict[str, set[str]] = defaultdict(set)
    for process, name in names.items():
        if process in keys:
            agreed[keys[process]].add(name)
    for process, key in keys.items():
        if process not in names and len(agreed[key]) == 1:
            names[process] = next(iter(agreed[key]))
    return names
