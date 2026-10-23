# 0072: Draw a run on rows by kind

- **Status:** Not started
- **Kind:** feature (enhancement)
- **Effort:** M
- **Origin:** design session 2026-10-03
- **Respects:**
  - [ADR-0007](../docs/adr/0007-shared-trace-converter-pipeline.md): the
    converter stays the one path from records to events and knows nothing of
    layouts. The layout is the encoder's.
  - [ADR-0024](../docs/adr/0024-an-event-names-the-track-it-is-drawn-on.md):
    an event names its track and the encoder derives every other row; the run
    rows are a second derivation from the same tracks. Amended when this
    lands: a record's slices name a `PauseTrack` carrying the generation, and
    each set of rows writes one descriptor per track.
  - [ADR-0027](../docs/adr/0027-group-every-row-an-interpreter-owns.md): the
    interpreter groups. Amended when this lands: `GC Pauses` merges a track
    per generation by name.
  - [ADR-0028](../docs/adr/0028-draw-every-process-a-row-of-its-own.md): a row
    per process, its `Lifetime` slice and the counts on it, unchanged.
    `interpreters` counts the distinct iids over a process's pause tracks.
  - [ADR-0011](../docs/adr/0011-process-lifetime-and-ordering.md): the
    `Processes` row is drawn once whatever the layout, and an `RSS` counter
    takes the rank its process row has.
  - [ADR-0013](../docs/adr/0013-rss-sampling.md): RSS is Perfetto-only and
    sampled the same way. Amended when this lands: the full layout also draws
    a counter per process in the `RSS` group.
  - [ADR-0015](../docs/adr/0015-gc-loss-spans-on-their-own-track.md): one
    window per poll interval, the args and their emission order hold. Amended
    when this lands: the full layout also draws every loss window on one root
    `GC Loss` row, where overlapping windows fold into lanes instead of
    nesting, so the rejected "one row for every interpreter" no longer holds.
  - [ADR-0001](../docs/adr/0001-hand-rolled-perfetto-protobuf-encoder.md): the
    encoder already writes everything the run rows need: counter descriptors,
    `child_ordering` and `sibling_order_rank`.
  - [ADR-0021](../docs/adr/0021-write-one-trace-format.md): `--format` is
    unchanged, and the layout applies to `perfetto` alone. `combine` keeps
    writing the process layout.
  - [ADR-0014](../docs/adr/0014-perfetto-integration-test-strategy.md): the
    rows are asserted through the trace processor.

## 1. Problem statement

On a run over a process tree, the trace gives every process a row of its own,
and a process's pauses sit two groups down, under `Python Interpreters` and
`Interpreter {iid}`. A pyperformance run spawns thousands of processes.
Finding the one whose pauses you want means scrolling a list that long and
expanding two groups, and seeing when the tree collected, or where gcmon went
blind, means doing that for every process.

## 2. Solution

`gcmon monitor` and `gcmon run` draw the run on these rows, beside the process
rows a trace draws today:

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

Every pause in the run sits on its generation's row with its sub-phases inside
it, and every loss window on `GC Loss`. A row is as many lanes tall as the
most interpreters pausing, or blind, at once. A process's pauses are the ones
inside its span's window on `Processes`, and every pause, loss window and mark
on these rows carries `pid` and `pid_epoch` to settle a window two processes
share. `Marks` holds the workload's marks, and with `--rss`, `RSS` holds a
counter per process.

This is the full layout, and the default. `--layout process`, or
`GCMON_LAYOUT=process`, writes today's trace, without these rows. A layout set
with `--format jsonl` or `stdout` logs a warning and the run goes ahead.

## 3. User stories

1. As someone reading a trace of a wide tree, I want every pause on a few
   rows, so that I can see when the run collected without opening a group per
   process.
2. As someone reading that trace, I want to find one process's pauses from its
   span, so that I do not scroll a row per process.
3. As someone reading it, I want each pause to say which process ran it, so
   that a window two processes share still tells them apart.
4. As someone reading it, I want gen-2 pauses on a row of their own, so that
   they stand out from the frequent gen-0 ones.
5. As someone reading it, I want every loss window on one row, so that I see
   where gcmon went blind across the run.
6. As someone reading a trace, I want the run rows and the process rows in one
   trace by default, so that I do not have to pick a view before the run
   starts.
7. As a developer profiling one script under `gcmon run`, I want the default
   to keep the counter charts and the row per process, so that the run rows
   add to what I read and take nothing away.
8. As an operator whose ticks are already tight, I want `--layout process`, so
   that the encoder draws each event once.
9. As an operator who sets a layout with `--format jsonl`, I want a warning,
   so that I know the layout did not apply.
10. As a CI job, I want `GCMON_LAYOUT` to stop the run on any value but `full`
    or `process`, so that a typo does not write the other layout.

## 4. Implementation decisions

**The encoder takes the layout at construction.** `ProtobufEventEncoder` gains
a `layout` parameter, `PerfettoExporter` passes it through, and
`EventsExporterFactory` passes the option. `combine` passes `process`.

**The encoder draws in three parts.** It calls three functions today:
`convert_trace_events_to_perfetto` per batch, `emit_retired_process_row` per
retired process and `finalize_perfetto_packets` at close. Each one's work
splits into three parts sharing one `PerfettoTrackState`:

- **Shared:** fold every event into its process's span, rank processes, and
  draw `Processes`: its descriptors, and its spans at retirement and at close.
- **Process rows:** the root descriptor, the process descriptors, the
  interpreter groups and every row inside them, and the `Lifetime` slice with
  the totals `_record_capture_totals` counts.
- **Run rows:** a new module in `exporters`.

The encoder runs the shared part, then the parts the layout names: both for
`full`, the process rows for `process`.

Rejected: two sets of the three functions, one per set of rows, run side by
side. Both would draw `Processes`, and the second finalizer would find
`has_process_lifetime_emitted` set and draw nothing at close.

Rejected: a layout argument threaded through the `_emit_*` helpers in
`perfetto_format`. Every helper would carry a branch for rows it does not
draw.

**A record's slices go on a pause track.** `convert_item_to_trace_format`
places the pause and its sub-phases on a new `PauseTrack(process, iid, gen)`
instead of `InterpreterTrack(process, iid)`, and its counters stay on the
`InterpreterTrack`. Every layout encodes the same `TraceEvent`s, and the
converter still knows nothing of layouts (ADR-0007). A sub-phase has no
`generation` arg of its own, and its track now carries it. The process rows
write a `GC Pauses` descriptor per `PauseTrack` under the interpreter's group,
and the trace processor merges an interpreter's three into one row by name. An
interpreter's collections never overlap, so the row stays one lane tall;
checked against the pinned trace processor. `get_interpreter_count` counts the
distinct iids over a process's pause tracks.

Rejected: one `GC Pauses` descriptor per `(process, iid)`, which kept the
trace byte-identical. A track would then be one descriptor on the run rows and
a third of one on the process rows, and the trace processor reads one
descriptor and three alike.

Rejected: recovering the generation in the encoder from the slice name, by
inverting the per-generation names in `model.names`. The converter has the
generation when it builds the slice, and the track is where an event says
which row it is drawn on (ADR-0024).

**Each track maps to a row in each set:**

| An event on | Process rows | Run rows |
|---|---|---|
| `PauseTrack`, a slice | `GC Pauses` in the interpreter group, merging a track per generation | a track per `(process, iid, gen)` named `Gen{gen}`, parented to `All GC Pauses`, ranked by gen |
| `InterpreterTrack`, a counter | `heap_size`, or the `GC Metrics` group | not drawn |
| `LossTrack` | `GC Loss` in the interpreter group | a track per `(process, iid)` named `GC Loss` at the root |
| `ProcessTrack`, an instant | the process row | a track per process named `Marks` at the root |
| `ProcessTrack`, a counter | `rss` on the process row | a counter per process named for its span, parented to `RSS`, ranked by `get_process_track_rank` |

A span goes on `Processes`, once, from the shared part.

Both groups carry `child_ordering = EXPLICIT`. The trace processor merges
tracks sharing a name and a parent. It puts a slice into a free lane when the
slice's track has nothing open, and a slice that begins inside an open one
stays on that lane, so a sub-phase stays inside its pause; checked against the
pinned trace processor. A track per `(process, iid, gen)` is enough, since an
interpreter's collections serialize (ADR-0015).

**`pid` and `pid_epoch` go on the run rows' pauses, loss windows and marks**,
written by the encoder from the event's `Process`. A sub-phase gets neither,
since it sits inside its pause. The process rows already say whose a pause is,
and the encoder's cost follows the annotation count, so they do not carry
them.

**Root rows sort by name**, with or without a root descriptor asking for
explicit ordering; checked against the pinned trace processor. `All GC Pauses`
is named to sort the pauses above `GC Loss`, the order the process rows rank
them in inside an interpreter group (ADR-0027).

**Packets go out when the process rows' would.** A track's descriptor precedes
the first packet naming it. A span goes out at the flush that retires its
process or at close (ADR-0011), and a pause, a mark or a sample at its own
flush, so a killed run keeps on the run rows what it keeps on the process
rows.

**`PerfettoTrackState` gains keys of the run rows' own:** a pause track per
`(process, iid, gen)`, a loss track per `(process, iid)`, a marks track and an
RSS counter per process, and the two groups. The process rows key their
descriptors on the same `Track`s, so the run rows share none of their keys,
and neither set takes the other's descriptor for its own.

**`full` is the default.** Under it, a pause and its sub-phases, a loss
window, a mark and an RSS sample each go out twice, and the encoder's cost
follows the annotation count. `PerfettoExporter` flushes on the monitor's
thread, so the second copy lengthens every tick that flushes. Accepted for
now: a pyperformance run at `--rate 0.01` under `full` and under `process`,
compared on overruns, decides whether `full` stays the default.

Rejected: `process` as the default, with `full` asked for. A reader would have
to know before the run that they want the run rows, and a streaming writer
cannot add them afterwards.

**The option.** `--layout` takes `full` or `process` and reads `GCMON_LAYOUT`,
which stops the run on any value but those two, as `GCMON_FORMAT` does
(ADR-0021). Its default is `None`, read as `full`, so the configuration echo
knows whether the operator chose a layout. One chosen by flag or variable,
whatever its value, with a format other than `perfetto` logs a warning and the
run goes ahead, as `--rss` does. The check compares the format with `perfetto`
and adds no tuple beside `RSS_CAPABLE_FORMATS`.

**Docs.** `docs/cli.md` documents the flag and the variable, `docs/formats.md`
the run rows, `docs/perfetto-sql.md` how to select one process's pauses on
them, and `docs/rss.md` where the counter sits, and under which name, in each
layout.

## 5. Seams and testing decisions

- **Seam:** the Perfetto integration suite in
  `tests/exporters/perfetto_integration/`: a trace built through the real
  `PerfettoExporter` and read back through the trace processor (ADR-0014). Row
  order, parentage and lanes come from `_track_event_tracks_ordered_groups`, a
  row's lanes being its `track_ids`; nesting comes from the `slice` table's
  `parent_id`.
- **New seam needed:** none; the layout is a constructor argument.
- **What makes a good test here:** what the trace processor draws, never the
  bytes. `misplaced_end_event` is filed under `data_loss`, so a check
  filtering on `error` alone misses it.
- **Prior art:** `TestTheOrderRowsAreDrawnIn` in `test_row_contents.py` for
  row order, `test_process_rows.py` for reading a merge through `track_ids`,
  `test_perfetto_process_span_fuzz.py` for the fuzz.
- **Cases**, on a full trace unless a case names the layout:
  1. The run rows come out at the top level in the order `All GC Pauses`,
     `GC Loss`, `Marks`, `Processes`, `RSS`.
  2. `All GC Pauses` holds `Gen0`, `Gen1` and `Gen2` in rank order.
  3. Two interpreters pausing over overlapping intervals: `Gen0` is one row
     whose `track_ids` hold two tracks, each pause keeps its own width, each
     sub-phase's `parent_id` is its own pause, and no `misplaced_end_event`.
  4. Two interpreters' overlapping loss windows: the root `GC Loss` is one row
     whose `track_ids` hold two tracks, and one interpreter's touching windows
     still read as a sequence.
  5. On the run rows, pauses, loss windows and marks carry `pid` and
     `pid_epoch`; sub-phases do not, and nothing on the process rows does.
  6. `RSS` holds a counter per process, in the order the process rows rank
     their processes.
  7. On the process rows, an interpreter that paused in all three generations
     draws one `GC Pauses` row and counts once in the `Lifetime` slice's
     `interpreters`.
  8. From the same events, the process layout's trace holds exactly the full
     trace's process rows and `Processes`, and no run row.
  9. A layout set with `--format jsonl` warns and runs, and `--format jsonl`
     with no layout set does not warn; `GCMON_LAYOUT=bogus` stops the run.
  10. Marked `fuzz`: random overlapping pauses with sub-phases, across
      interpreters and processes, each pause read back at its own width with
      its sub-phases' `parent_id` naming it, and no `misplaced_end_event`.

## 6. Out of scope

- **The merged layout.**
  [Spec 0075](0075-draw-a-run-without-its-process-rows.md) draws the run rows
  without the process rows. The measurement in section 4 decides whether
  anyone needs it.
- **`gcmon combine`.** It keeps writing the process layout. Whether it takes
  `--layout`, and with which default, waits on the measurement that decides
  the live default.
- **Choosing the layout from the tree's size.** gcmon is a streaming writer
  and does not know how wide a tree will grow when it writes the first
  descriptor, the argument ADR-0024 and ADR-0027 already make.
- **A `heap_size` chart on the run rows.** Its values are on the pauses, and a
  chart means a row per interpreter, which the process rows already draw.
- **The records.** The ADRs in the header are amended once this lands, with a
  new record taking the decision; each bullet names the clause it loses.

## 7. Further notes

- Where the Perfetto UI places the run rows against the process rows reaches
  no SQL table (ADR-0011). It is read off the UI when this is built, and the
  run rows' names are adjusted then if they land below.
