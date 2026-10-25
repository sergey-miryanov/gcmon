# 0076: Extract a trace's records into a CSV file

- **Status:** Not started
- **Kind:** feature (enhancement)
- **Effort:** M
- **Origin:** GitHub #228, for sergey-miryanov/pep848-sim#117. The debug
  script `scripts/trace_stats.py` (gitignored, no tests) does this as
  `--export` and is the prototype here.
- **Respects:**
  - [ADR-0026](../docs/adr/0026-two-subsystems-over-a-shared-base.md): the
    reader and the join live in `analysis`, the command in `cli.analyze`, and
    neither reaches `monitoring`. `analysis` may read `model`, which is where
    the column names come from.
  - [ADR-0001](../docs/adr/0001-hand-rolled-perfetto-protobuf-encoder.md):
    `perfetto` stays out of the monitoring runtime. It arrives in the
    `analysis` extra.
  - [ADR-0022](../docs/adr/0022-compress-each-batch-of-packets.md): a zstd
    trace needs Perfetto v58 or newer, and the suite pins its own trace
    processor. Amended when this lands, as spec 0061 describes: the analysis
    subsystem holds the pin and the suite takes it from there.
  - [ADR-0010](../docs/adr/0010-process-identity-cmdline-and-start-marker.md):
    the command line is a debug annotation on the process span, absent where
    gcmon could not read it. A process without one is named by the join alone.
  - [ADR-0025](../docs/adr/0025-create-every-process-in-one-place.md): a
    record belongs to a process, a pid and its epoch, so a reused pid is two
    workers in the table.
  - [ADR-0009](../docs/adr/0009-nanoseconds-canonical-time-unit.md): `ts` is
    nanoseconds, as the trace holds it.
  - [ADR-0014](../docs/adr/0014-perfetto-integration-test-strategy.md): traces
    are written in the test and read through the trace processor, behind no
    marker.

## 1. Problem statement

A trace holds every record gcmon read from a run, and nothing in gcmon hands
those records to another tool. pep848-sim holds each of its presets to the
pyperformance benchmark it models, per collector, generation and kind of
collection, and needs the records of a reference-branch trace as a table its
loader reads with the standard `csv` module.

Getting that table out today takes three things gcmon does not ship:

- **A trace processor that reads the file.** The shell the `perfetto` package
  launches is v57.2. It loads a zstd-batched trace as an empty one and reports
  no error.
- **SQL against gcmon's track layout.** The layout changes between releases.
  The debug script that does this extraction writes no rows from a current
  trace: its pause query joins through thread tracks, which no longer hold
  pauses.
- **The benchmark each worker ran.** pyperformance takes no external pyperf
  hook, so a trace of it carries no marks, and the benchmark's name exists
  only in pyperformance's results file. gcmon-results' alias tables matched 70
  workers to 54 of 119 benchmarks on one suite trace, and missed
  async_tree_io_tg.

## 2. Solution

```bash
gcmon extract run.pftrace
gcmon extract run.pftrace -o records.csv --results suite.json \
    --benchmark async_tree_io_tg --benchmark xml_etree_parse \
    --meta gcmon_version=0.9.1
```

The first writes `run.csv` and `run.meta.json` from `run.pftrace` and the
`run.json` pyperformance wrote beside it. The second names each file.

The CSV has a header row and one row per record in the trace. A row names the
benchmark its worker ran, says whether that worker calibrates, identifies the
worker and the interpreter, and carries every field the record's pause carries
under the record's own names. The counters stay the lifetime totals the record
held.

The `.meta.json` beside it says where the table came from: the trace's file
name and hash, the build and the pyperformance and pyperf versions the results
file names, the run's date, and whatever `--meta` added.

## 3. User stories

1. As a pep848-sim maintainer, I want a pyperformance trace's records as a CSV
   with a header, so that the loader needs no package outside the standard
   library.
2. As a pep848-sim maintainer, I want each row to name its benchmark as the
   results file does, so that a preset is matched to its benchmark by name.
3. As a pep848-sim maintainer, I want a benchmark sharing its script with
   others kept apart from them, so that `xml_etree_parse` holds its own
   workers' rows and not those of the other three XML benchmarks.
4. As a pep848-sim maintainer, I want to know which workers calibrate, so that
   I can leave them out.
5. As a pep848-sim maintainer, I want the counters as the record held them, so
   that taking the differences myself keeps a lost record's counts in the
   totals.
6. As a pep848-sim maintainer, I want to keep only the benchmarks I model, so
   that the files stay small enough to commit.
7. As a pep848-sim maintainer, I want the build, the versions and the trace's
   hash beside the table, so that a target can be traced to the run it came
   from and a fresh trace replaces it knowingly.
8. As an operator with a trace of a stock 3.15, I want the same columns with
   the fields that build does not report left empty, so that one loader reads
   both builds.
9. As an operator whose trace sits beside its results file under the same
   name, I want to name the trace alone, so that the common case takes one
   argument.
10. As an operator who installed gcmon to monitor, I want `extract` to say
    which extra it needs, so that its absence costs me one install and no
    other command breaks.
11. As an operator who mistyped a benchmark name, I want the command to stop
    and name it, so that an empty table is never the answer to a typo.
12. As a pep848-sim maintainer rerunning `extract`, I want an existing table
    left alone unless I ask, so that a committed target is never overwritten
    by accident and replacing it takes one flag.
13. As an operator whose results file is missing, I want the command to stop,
    so that I never get a table with no benchmark names.
14. As a maintainer adding a field to the record, I want it to reach the CSV
    the way it reaches the pause, so that no second list falls behind.
15. As a pep848-sim maintainer, I want to drop the columns I do not read, or
    keep only those I do, so that committed files stay small.

## 4. Implementation decisions

**This lands spec 0061's lower seam and the `analysis` extra, with slices and
processes.** A path goes in, and rows come out: each slice with its track's
name, its category, name, start and duration, its arguments keyed, and the
process its track parents to. Each process comes with the pid, pid epoch and
command line its span carries. The trace processor behind it is the pinned
v58.2 shell, and opening a trace without the extra fails naming it, both as
0061 now specifies. Counter samples and the upper seam stay 0061's.

**The table reads rows, not records.** A pause's arguments already carry the
record's fields under the record's names. 0061's upper seam returns
`dict[int, list[TItem]]`, keyed by pid: it drops the pid epoch and the command
line this table needs, and folding into `GCStatsInfo` only to spell the fields
back out is a round trip through a shape the CSV does not use.

**One row per record.** The `full` layout draws each pause twice, on its
interpreter's `GC Pauses` row and on a run row, and the `process` layout once.
A row is keyed by pid, pid epoch, `iid`, `generation` and start, and a second
copy of a key is dropped. The run-row copy carries `pid` and `pid_epoch` as
arguments, and the other takes them from its process's span. Spec 0075's
merged layout draws only the run-row copy, and the key holds there too.

**The columns are fixed, and the pause's come from the groups that draw it.**
First `benchmark`, `calibration`, `pid`, `pid_epoch` and `ts`, then every
argument a pause can carry, in the order the converter writes them:
`PausePhase`'s, then each `PAUSE_FIELDS` group's. The header reads them off
the `Info` protocols, which already name the fields each group's `args` reads,
and `PausePhase` gains one for its own; `generation` comes from `gen`. `args`
keeps its literal dict: building it from a list of names was measured slower
on the converter's path. A field the record lacks is an empty cell, so a stock
build's rows have the same columns with `old_work` and its neighbours empty.

Rejected: a column list in the extract module. #228 was written before #229
added five fields to the pause, and a list taken from it would have missed all
five. Rejected too: the union of the arguments one trace holds. It gives a
stock trace fewer columns than a reference one, and a loader keyed on
`old_work` fails on the first.

**The counters are written as read.** `collections`, `candidates`,
`collected`, `uncollectable` and `duration` are lifetime totals per process,
interpreter and generation. The debug script took differences, which folds a
lost record's counts into whichever step follows the gap. Here the reader
takes them.

**A worker's benchmark comes from overlap in time, ported from the debug
script's `name_processes` and `label_processes`.** A results-file run occupies
`uptime - duration` to `uptime`, on the clock the trace's timestamps use. Each
run goes to the process whose span shares the largest fraction of the time
either of them covers. Shared time alone would hand every run to the pyperf
manager, whose span covers all of them. Containment would lose a run that
began a few milliseconds before gcmon discovered its worker. A process the
join misses takes the name the other processes of its command agree on. The
command is reduced by the script's `compare_key`, which drops pipe handles,
`parent_pid` and `--loops` and keeps `--worker-task`. A process with no name
has an empty `benchmark` cell.

Calibration runs join as well. The script skipped them, and here a calibration
worker needs its name so pep848-sim can see which benchmark it calibrated.
`calibration` is 1 where the command line carries one of pyperf's worker flags
`--calibrate-loops`, `--recalibrate-loops`, `--calibrate-warmups` or
`--recalibrate-warmups`, and 0 otherwise.

Rejected: gcmon-results' alias tables, which guess a name from the script and
its arguments. They need an entry for every benchmark that shares a script,
and missed async_tree_io_tg on the trace in section 1. Rejected too: spec
0062's key alone, `bm_async_tree#0`. It separates the benchmarks but is not
the name #228 asks for.

**A results file's metadata nests.** pyperf moves metadata that every
benchmark shares to the file's top level, so a one-benchmark file keeps `name`
there and its benchmark's own `metadata` is `null`. A run's metadata is the
file's, overlaid by the benchmark's, overlaid by the run's own. The debug
script reads only the benchmark level and names nothing on such a file. A path
ending `.gz` is read through `gzip`, as pyperf reads it.

**Only the trace is required.** `-o` defaults to the trace's path with `.csv`
in place of its suffix, and `--results` to the same path with `.json`, the
debug script's convention of `A.pftrace` beside `A.json`. Either flag names
another path.

**A missing results file stops the command, and so does an existing output.**
Named or inferred, a results file that is not there stops it before anything
is written. A table with no benchmark names answers nothing #228 asks. A CSV
or sidecar already at the output path stops it too, and both are left as they
were, unless `--overwrite` is given. A committed table is replaced on request,
never by a plain rerun. gcmon has no overwrite flag elsewhere to match, and
`--overwrite` names what it permits where `--force` would not.

**`--benchmark` keeps rows, by name.** It repeats. Without it every process
with a pause has rows. A name the results file does not hold stops the command
with an error naming it.

**`--drop` and `--keep` choose the columns, by name.** Each takes a
comma-separated list and repeats, and the two exclude each other. `--drop`
removes the columns named, so a field a later record gains still reaches the
table. `--keep` writes only the columns named, a set that holds across gcmon
versions. Either way the columns keep `COLUMNS` order, so two tables cut from
different traces line up. A name `COLUMNS` does not hold, or a choice that
leaves no column, stops the command before anything is written.

**The sidecar is JSON, at the output's path with `.meta.json` in place of its
suffix.** `trace` and `trace_sha256` come from the file. `python_version`,
`performance_version` and `perf_version` come from the results file's
metadata, where a reference build's `python_version` carries its revision.
`date` is the earliest run's. `--meta KEY=VALUE` repeats and adds a key, and
one that collides with a derived key stops the command.

The collector and the thresholds are not in the sidecar. Since #229 every
record carries `gc_type` and `threshold0` to `threshold2`, so they are
columns. gcmon's version is not derived: the trace does not record which gcmon
wrote it (spec 0020), and the version running `extract` is a different fact.
`--meta gcmon_version=0.9.1` says it. Rejected: comment lines heading the CSV.
The `csv` module has no comment syntax, and `DictReader` would take the first
one as the header.

**The command is `extract`, in `cli.analyze` beside `combine`.** The ported
join, the results reader and `compare_key` move into `analysis` with tests.
The debug script's other modes stay where they are.

**Docs.** `docs/cli.md` gains an `extract` section. `docs/formats.md`
describes the identity columns and the sidecar, and says that the remaining
columns are the pause's arguments under their JSONL names, pointing at the
JSONL table rather than listing them again. That keeps the fields
`UNDOCUMENTED` lists out of the docs. CHANGELOG gets one `Features` line.

## 5. Seams and testing decisions

- **Seam:** the `extract` command, run in-process over a trace and a results
  file written in the same test, with the CSV read back by `csv.DictReader`.
  It observes everything an operator sees. Below it, the join is a function
  over spans and runs, tested with no trace.
- **New seam needed:** the lower reader seam, which nothing reaches today.
  `read_jsonl` is the shape to model it on.
- **What makes a good test here:** records written through the encoder and
  read back through the pinned trace processor, a reader gcmon did not write.
  Command lines quoted from a real pyperformance capture, not invented ones.
- **Prior art:** `tests/exporters/` for writing a trace in the test, the
  `combine` command's tests for the CLI, `tests/perfetto_prebuilt.py` for the
  pin.
- **Cases:**
  1. Every record written reads back as one row, its fields as written, from a
     `full` trace and from a `process` trace alike.
  2. A stock-shaped record gives a row with every column and the incremental
     fields empty.
  3. Each worker's last row per `iid` and `generation` holds the record's
     lifetime totals.
  4. A trace written with `compression.zstd` reads back in full.
  5. The header is the identity columns and then the names the `Info`
     protocols give, in the order `args` writes them, and a record carrying
     every field fills every column.
  6. The join: a run goes to its worker over a manager whose span covers every
     run; a run starting a few milliseconds before its worker's span still
     goes to it; a worker the join misses takes its command's agreed name, and
     one whose command disagrees gets none.
  7. Two benchmarks of one script, told apart by `--worker-task` alone, get
     their own names.
  8. A one-benchmark results file names its workers from top-level metadata.
  9. Each of the four calibration flags sets `calibration`, and a calibration
     worker is named.
  10. `--benchmark` keeps only its rows, and an unknown name stops the command
      naming it.
  11. The sidecar holds the trace's SHA-256 and the results file's versions,
      `--meta` adds a key, and a colliding key stops the command.
  12. Without the extra, `extract` fails naming it, and `monitor`, `run` and
      `combine` still run.
  13. Given the trace alone, `extract` reads the `.json` beside it and writes
      the `.csv` and `.meta.json` beside it.
  14. A missing results file, inferred or named, stops the command and writes
      nothing.
  15. An existing CSV or sidecar stops the command and is left unchanged; with
      `--overwrite`, both are replaced.
  16. Regression guard: the suite's trace-processor tests read through the
      moved pin and pass unchanged.
  17. `--drop` removes its columns and `--keep` writes only its own, both in
      `COLUMNS` order; the two together are refused, and an unknown name or a
      choice leaving no column stops the command naming it.
- **Acceptance run, not a test:** on a reference-branch pyperformance trace
  with calibration workers left out, each benchmark's last rows match what SQL
  reads from its workers' last pauses, `xml_etree_parse`'s rows come from its
  own workers, and every async_tree_io_tg worker has rows.

## 6. Out of scope

- **The statistics table from a trace, and the upper seam.** Spec 0061.
- **Counter samples in the reader.** Nothing here reads one, and 0061 adds
  them with the fold that does.
- **RSS.** The debug script carries it, and #228 does not ask for it.
- **Sub-phase timings.** A row is a record as its pause carries it, and #228
  names no sub-phase field.
- **Differences, yields and plots.** #228 leaves them to the reader.
- **Rows for lost records.** gcmon never read them. A gap in `collections`
  shows where they were.
- **The workload level and the sanitizer registry.** Spec 0062.
- **The debug script's other modes:** its tables, `--compare` and
  `--old-work`.
- **A JSONL capture as input.** `combine` turns one into a trace.
- **A trace gcmon did not write**, for 0061's reason.

## 7. Further notes

**The clock agreement is observed on Windows alone.** pyperf's `uptime` is
`time.time()` minus the boot time. On Linux that boot time is `/proc/stat`'s
`btime`, in whole seconds, while the trace's timestamps come from CPython's
performance counter. The join named every async_tree_io_tg worker on two
Windows traces. A Linux pyperformance trace through case 6's join settles
whether an offset under a second costs it workers. If it does, the fix is an
offset estimated from the trace, not a looser score.

This lands before 0061. 0061 then takes the lower seam, the pin and the extra
as given, keeps the upper seam and `report`, and is rewritten to say so when
this lands. Spec 0063's `--results` can name workers through this join rather
than through version-specific tables, and is rewritten when picked up.

Landing this earns an ADR for the join: a worker's benchmark comes from
overlap in time with the harness's own results, not from its command line.

#228's requirement for the thresholds and the collector beside the table is
met by columns, since #229 landed after it was filed.
