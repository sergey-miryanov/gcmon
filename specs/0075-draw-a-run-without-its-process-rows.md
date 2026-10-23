# 0075: Draw a run without its process rows

- **Status:** Not started
- **Kind:** feature (enhancement)
- **Effort:** S
- **Origin:** design session 2026-10-03, split out of
  [spec 0072](0072-draw-a-run-on-rows-by-kind.md)
- **Respects:**
  - [ADR-0007](../docs/adr/0007-shared-trace-converter-pipeline.md): the
    converter knows nothing of layouts, so a merged trace is drawn from the
    events a full one is.
  - [ADR-0024](../docs/adr/0024-an-event-names-the-track-it-is-drawn-on.md):
    an event names its track and the encoder derives every other row. Amended
    when this lands: the encoder emits a process descriptor ahead of a track's
    first packet only where it draws the process rows.
  - [ADR-0011](../docs/adr/0011-process-lifetime-and-ordering.md): the
    `Processes` row is drawn the same in every layout. Amended when this
    lands: the root descriptor and the process descriptors belong to the
    process rows. Every layout ranks processes.
  - [ADR-0010](../docs/adr/0010-process-identity-cmdline-and-start-marker.md):
    the process descriptor, its command line and its start stamp. Amended when
    this lands: they belong to the process rows, and a merged trace carries
    the command line on the span.
  - [ADR-0028](../docs/adr/0028-draw-every-process-a-row-of-its-own.md): a row
    per process, its `Lifetime` slice and the counts on it. Amended when this
    lands: they belong to the process rows.
  - [ADR-0027](../docs/adr/0027-group-every-row-an-interpreter-owns.md): the
    interpreter groups. Amended when this lands: they belong to the process
    rows.
  - [ADR-0013](../docs/adr/0013-rss-sampling.md): RSS is Perfetto-only and
    sampled the same way. Amended when this lands: a merged trace draws a
    process's RSS in the `RSS` group alone.
  - [ADR-0015](../docs/adr/0015-gc-loss-spans-on-their-own-track.md): one
    window per poll interval. Amended when this lands: the per-interpreter
    `GC Loss` row belongs to the process rows.
  - [ADR-0021](../docs/adr/0021-write-one-trace-format.md): `--format` is
    unchanged, and the layout applies to `perfetto` alone. `combine` keeps
    writing the process layout.
  - [ADR-0014](../docs/adr/0014-perfetto-integration-test-strategy.md): the
    layout is asserted through the trace processor.

## 1. Problem statement

The full layout draws a run on rows by kind and every process's rows beside
them ([spec 0072](0072-draw-a-run-on-rows-by-kind.md)). A pyperformance run
spawns thousands of processes, so a reader who reads the run rows alone still
gets a trace holding thousands of process rows they never open, and gcmon
draws each pause, loss window and mark twice, on the monitor's thread.

## 2. Solution

`--layout merged`, or `GCMON_LAYOUT=merged`, on `gcmon monitor` and
`gcmon run` writes the run on these root rows and nothing else:

```
All GC Pauses
  Gen0
  Gen1
  Gen2
GC Loss
Marks
Processes
RSS
  Process 38460
  Process 14012
  ...
```

The counter rows and the `Lifetime` slice's counts are gone: a counter's value
is an arg on its pause, and a `Lifetime` count is a count or a sum over the
process's pauses and loss windows. A reader who wants the `heap_size` chart or
a row per process records with `full` or `process`.

## 3. User stories

1. As someone reading a trace of a wide tree, I want the run rows alone, so
   that the trace carries no row per process I never open.
2. As an operator monitoring a wide tree, I want each event drawn once, on the
   run rows, so that the tick pays for one copy and not two.
3. As an operator, I want the merged trace to keep every number the full one
   draws, so that choosing it costs a view and not data.
4. As a maintainer, I want a full trace's two sets of rows checked against
   each other, so that a number one of them drops fails a test.

## 4. Implementation decisions

**`merged` runs the shared part and the run rows**, two of the three parts
spec 0072 splits the encoder into, and not the process rows.

**Not drawn:** process descriptors, interpreter groups, counters, the
`Lifetime` slice and its counts, and the root descriptor. Every counter value
is an arg on the pause it came from, and every `Lifetime` count is a count or
a sum over the process's pauses and loss windows. `_record_capture_totals`
belongs to the process rows and does not run. The shared part still folds
spans and ranks processes, since a span needs its bounds and an `RSS` counter
the rank its process row would have.

**The option.** `--layout` and `GCMON_LAYOUT` take `merged` beside `full` and
`process`.

**Docs.** `docs/cli.md` documents the value, and `docs/formats.md` the rows a
merged trace holds and the args that carry what the process rows draw.

## 5. Seams and testing decisions

- **Seam:** the Perfetto integration suite, as in spec 0072 (ADR-0014).
- **New seam needed:** none.
- **What makes a good test here:** what the trace processor reads, never the
  bytes.
- **Prior art:** spec 0072's cases, and `test_process_rows.py` for reading a
  merge through `track_ids`.
- **Cases:**
  1. A merged trace's top-level rows are `All GC Pauses`, `GC Loss`, `Marks`,
     `Processes` and `RSS`, in that order, and it holds no process row with
     `pid > 0`.
  2. **Both sets of rows hold the same data**, inside one full trace. What
     each draws directly matches in both directions: the same number of
     pauses, sub-phases, loss windows, marks and RSS samples, each with the
     same width, name and args, `pid` and `pid_epoch` aside. What the process
     rows derive is recomputed from the run rows: every per-generation counter
     and `heap_size` sample from the args of the pause that starts or ends at
     its timestamp, every `Lifetime` count from the process's pauses and loss
     windows.
  3. From the same events, the merged trace holds exactly the full trace's run
     rows and `Processes`.

## 6. Out of scope

- **Converting a merged trace to the process layout.**
  [Spec 0073](0073-convert-a-merged-trace-to-the-process-layout.md) covers it;
  case 2 pins the property it rests on.
- **`gcmon report` reading a merged trace.**
  [Spec 0061](0061-build-the-statistics-table-from-a-tracefile.md) builds its
  reader around the process rows, and teaching it the run rows is 0061's work.
- **A `heap_size` chart in the merged layout.** Its values are on the pauses,
  and a chart means a row per interpreter, the shape this layout exists to
  avoid.
- **`gcmon combine`.** Spec 0072 keeps it on the process layout, for the
  reason it gives.
- **The records.** The ADRs in the header are amended once this lands; each
  bullet names the clause it loses.

## 7. Further notes

- Constrained after spec 0072, which builds the run rows and the three parts.
- Spec 0072's measurement bears on this one. If `full` stays the default and
  its run rows are easy to reach, what is left for this layout is a smaller
  trace.
