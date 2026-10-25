# ADR-0030: Name a worker's benchmark by its overlap in time with the harness's results

- **Status:** Accepted
- **Date:** 2026-10-26
- **Modules:** analysis

## Context

`gcmon extract` names the benchmark each record's process ran, in the words
pyperformance's results file uses. A pyperformance trace carries no marks:
pyperformance takes no external pyperf hook, so the hook ADR-0023 describes
does not run in its workers.

A worker's command line does not name its benchmark either. `bm_base64` runs
eleven benchmarks from one script, told apart by `--worker-task` alone, and
`async_tree_io` and `async_tree_io_tg` differ by an argument whose spelling is
not the benchmark's. gcmon-results guessed names from the script and its
arguments through hand-kept alias tables, and on one suite trace matched 70
workers to 54 of the results file's 119 benchmarks, missing async_tree_io_tg.

The results file says when each run ran. pyperf records `uptime`, read once
the run is over, and `duration`, so a run occupied
`[uptime - duration, uptime]`, on the clock the trace's timestamps use.

## Decision

- A run's benchmark goes to the process whose span on the `Processes` row
  shares the largest fraction of the time the two cover together.
- Calibration runs join like measured ones.
- A process no run reaches takes the name the other processes of its command
  agree on, the command reduced to a key that drops install paths, OS handles
  and `--loops` and keeps `--worker-task`. A process with no command line, or
  whose command's processes disagree, gets no name.
- A run's metadata is the results file's, overlaid by its benchmark's,
  overlaid by its own: pyperf moves what every benchmark shares to the top
  level, so a one-benchmark file names its benchmark only there.

## Consequences

- No table changes when pyperformance adds a benchmark or renames one, and two
  benchmarks sharing a script need nothing to tell them apart.
- The naming needs the results file of the run that wrote the trace. A trace
  without one gets no names.
- The join rests on the two clocks agreeing. On Linux pyperf takes the boot
  time from `/proc/stat`'s `btime`, in whole seconds, so `uptime` can sit up
  to a second off the trace's clock. A fixed offset is the remedy if that
  costs workers, not a looser score.
- The pyperf process that starts the workers has a span covering every run and
  takes none, so it needs no filter.

## Alternatives considered

- **Shared time alone.** Rejected: the process that starts the workers shares
  the whole of every run, and takes each run whose worker it beats.
- **Containment of the run in the span.** Rejected: a span opens at the poll
  that discovers the process, so a run often starts a few milliseconds before
  its worker's span, and containment loses it.
- **Alias tables from the script and its arguments.** Rejected: they need an
  entry for every benchmark sharing a script, change with pyperformance, and
  missed async_tree_io_tg on the trace above.
- **The script and `--worker-task` as the name.** Rejected: it separates the
  benchmarks, but `bm_async_tree#0` is not the name the results file and its
  readers use.
