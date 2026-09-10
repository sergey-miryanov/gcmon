# 0069: Group every row an interpreter owns under one track

- **Status:** **Pinned**
  (`TestTrackDescriptors::test_gcmon_writes_no_thread_row_of_its_own`,
  `TestTrackDescriptors::test_no_slice_is_drawn_on_a_thread_track`). The two
  tests it was pinned by asserted the shape this spec removes:
  `test_thread_tid_is_the_interpreter_id` went with the claim, and
  `test_the_trace_processor_keeps_a_thread_whose_tid_is_the_pid` became the
  first of the two above.
- **Kind:** bug (reporting)
- **Effort:** L
- **Origin:** the review of spec 0027's landing, 2026-09-09, and the grilling
  session on 2026-09-10 that measured four trace shapes against the trace
  processor
- **Respects:**
  - [ADR-0027](../docs/adr/0027-group-every-row-an-interpreter-owns.md): the
    record this spec builds. Every step below is one of its rules.
  - [ADR-0002](../docs/adr/0002-perfetto-track-uuid-and-hierarchy.md): every
    track is explicitly parented, the two new groups included.
  - [ADR-0003](../docs/adr/0003-gc-metrics-group-track.md): a custom group
    buys back the ordering an OS-scoped parent discards. ADR-0027 applies it
    twice more, and step 5 moves the `GC Metrics` group's parent.
  - [ADR-0004](../docs/adr/0004-toplevel-shared-counters.md): superseded by
    ADR-0024, and its supersession note still keeps `heap_size` drawn a level
    up, outside `GC Metrics`. Step 7 takes that clause out.
  - [ADR-0011](../docs/adr/0011-process-lifetime-and-ordering.md): the row pid
    and the process ordering are untouched. Step 7 rewrites its
    thread-descriptor clause, which ADR-0027 voided.
  - [ADR-0024](../docs/adr/0024-an-event-names-the-track-it-is-drawn-on.md):
    an event names its `Track`, and the encoder derives every other row. The
    model does not move; the two groups are rows the encoder derives, like the
    `GC Metrics` group and the `Processes` track already are. Step 7 rewrites
    its `heap_size` clauses.

## 1. Problem

Open a trace in the Perfetto UI and one interpreter per process is marked as
that process's main thread. It is not interpreter 0, and running the same
workload again marks a different one. In SQL it is `thread.is_main_thread`,
and it says nothing about the interpreter it lands on.

Ask a harder question of the same trace and it has no answer at all. Expand
`GC Metrics` on a process running four interpreters and `G0 collected` is
there four times, identically named, with nothing to say which interpreter
each belongs to. Every per-generation counter in the trace is like this. The
only counter a reader can attribute is `heap_size`, and only because its track
name spells the interpreter out.

Underneath both is one mistake. gcmon describes each interpreter as an
operating-system thread, which it is not, and hangs everything else the
interpreter owns off the process track beside it, where the trace processor
merges what shares a name and discards what carries an order.

## 2. Evidence

`perfetto_format._emit_thread_descriptor` is the only place gcmon writes a
`thread` sub-message:

```python
    row_pid = state.get_row_pid(track.process)
    desc = build_track_descriptor(..., pid=row_pid, tid=iid, ...)
```

The trace processor sets `thread.is_main_thread` from `tid == pid`. gcmon
numbers row pids from 1 in the order it discovers processes
(`PerfettoTrackState.get_row_pid`), and CPython numbers interpreters from 0,
so the flag lands on the interpreter whose iid equals its process's row pid.
Three processes, four interpreters each, one workload: it marks iid 1, iid 2
and iid 3.

The merge is the load-bearing evidence, and it needs no patch to observe.
`_emit_counter_group_descriptor` allocates a `GC Metrics` uuid per
`(process, iid)` and parents each to the process track. Query the `track`
table of any gcmon trace and there is one `GC Metrics` row per process,
holding every interpreter's counters:

```
id=5   GC Metrics      parent=NULL   process=Process 4001
id=6     G0 collected  parent=5
id=11    G0 collected  parent=5      <- a different interpreter, same name
```

`CONTEXT.md` already describes the counter group as "one per interpreter",
which is what the code intends and what no trace has ever held.

Every shape below was read back through the trace processor rather than
reasoned from the proto. Parenting each interpreter's rows to a group of its
own, and those groups to one `Interpreters` group per process, is what leaves
every row with a non-NULL `parent_id`, the diagnostic ADR-0003 used to tell an
honored ordering from a discarded one:

```
id=3   Interpreters    parent=NULL          process=Process 4001
id=4     Interpreter 0 parent=Interpreters  process=Process 4001
id=5       GC Pauses   parent=Interpreter 0
id=6       GC Metrics  parent=Interpreter 0
id=7         G0 collected  parent=GC Metrics
id=10      heap_size   parent=Interpreter 0
id=11    Interpreter 1 parent=Interpreters  process=Process 4001
id=13      GC Metrics  parent=Interpreter 1  <- no longer merged
```

Every counter in that trace attributes to its interpreter and its process by
walking `track.parent_id`, two hops at most.

`scripts/interpreter_as_custom_track.py` and `scripts/interpreter_group.py`
write these traces and print the tables above. `scripts/` is gitignored, so a
fresh clone reproduces them from the docstrings rather than from the files.

## 3. Scope

**Affected:** the Perfetto output, every trace. The pause row, the loss row
and the `heap_size` counter are renamed, `Interpreters` and
`Interpreter {iid}` appear, and the `thread` and `thread_track` tables lose
every row gcmon wrote. Everything that reads a trace moves with it:
`docs/perfetto-sql.md`, `docs/formats.md`, `docs/control-plane.md`, both
`_process_filter` helpers
(`tests/exporters/test_perfetto_exporter_integration.py` and
`tests/test_convert_cmd_perfetto.py`), the thread joins in
`tests/exporters/test_perfetto_slice_expansion.py` and
`tests/exporters/test_perfetto_loss_track.py`, the name literals in seven test
modules, and `tests/fixtures/monitored_run_perfetto_trace.txt`.

**Not affected:** the JSONL and stdout paths, which name an interpreter by its
`iid` and write no tid. Slice content and args, `debug.iid` included. The
`Processes` track, the `Lifetime` slices, the process descriptor and the row
pid it carries, and the process ordering. `rss` and the process's marks, which
a `ProcessTrack` owns. The `TraceEvent` model: no event names a group, the way
no event names the `GC Metrics` group today.

**Why the suite didn't catch it:** nothing asserted on `is_main_thread` until
spec 0027 landed, and nothing has ever asserted that a per-generation counter
can be traced back to an interpreter. The first gap is what makes this spec
Pinned. The second is a test that fails on `main` today, and section 5 case 2
is it.

## 4. Proposed change

Each step is one rule of ADR-0027. The record holds the argument; this is the
order to build it in.

1. The encoder derives an `Interpreters` group per process, parented to the
   process track with `child_ordering = EXPLICIT`, and an `Interpreter {iid}`
   group per `(process, iid)` inside it, ranked by iid and also `EXPLICIT`.
   `PerfettoTrackState` keys a uuid for each, beside the tables it already
   keys on a `Track`.
2. The interpreter's own row becomes a plain custom track named `GC Pauses`,
   parented to its interpreter group. No `thread` sub-message, so no `tid` and
   no `pid`.
3. The loss row is named `GC Loss` and parents to the interpreter group. The
   iid leaves its name, which the group now carries.
4. `heap_size` parents to the interpreter group rather than the process track,
   and `trace_converter` writes `heap_size` as its display name rather than
   `Thread {iid} heap_size`. The top-level metric set stays as the switch that
   selects it, and now means "drawn on the interpreter group rather than
   inside `GC Metrics`".
5. The `GC Metrics` group parents to the interpreter group. Its key is already
   per `(process, iid)`, so nothing about its identity changes; only its
   parent does, and that is what stops the copies merging.
6. `build_track_descriptor` loses `tid` and `thread_name` and the branch they
   select, `perfetto_proto` loses `ThreadDescriptorField`, and the root
   descriptor loses `thread_ordering` and `ThreadOrdering` with it. After this
   the encoder cannot describe an operating-system thread.
7. The records catch up. ADR-0011's thread-descriptor clause says the row pid
   reaches the trace through the `ProcessDescriptor` alone. ADR-0024 loses its
   `heap_size` qualifier clause, and ADR-0004's supersession note loses the
   top-level clause it still keeps. ADR-0003's decision parents `GC Metrics`
   to the interpreter group, and its alternatives paragraph stops citing
   `thread_ordering` as a field gcmon uses. ADR-0027 drops "unbuilt" from its
   status.
8. `CONTEXT.md` gains **interpreter group** and **interpreter list**;
   **Track** respells "an interpreter's row" as the pause row; **Interpreter**
   loses "gcmon publishes the iid as a Perfetto `tid`"; **Process track**
   stops listing thread rows. **Counter group** needs no edit and becomes
   true. `specs/CONVENTIONS.md` rule 4 carries the same stale claim and loses
   it.
9. `docs/perfetto-sql.md` loses the `thread` bullet and the tid note, joins
   `process_track`, and teaches the two-hop parent join that names the
   interpreter a counter belongs to. `docs/formats.md` and
   `docs/control-plane.md` take the renamed rows and the two groups.
10. The CHANGELOG takes one entry per claim under `Breaking changes`, since
    each stands alone: a trace holds no `thread` rows of gcmon's; `Thread N`,
    `GC Loss N` and `Thread N heap_size` are renamed; and a query that joined
    `thread_track` joins `process_track`.

## 5. Seams and testing decisions

- **Seam:** the trace processor, via
  `tests/exporters/test_perfetto_exporter_integration.py`. It is the highest
  seam that can see the change and the only one that says what the trace means
  rather than what bytes were written. **With one exception**: step 1 on its
  own is invisible there. The trace processor builds no `track` row for a
  descriptor whose whole subtree carries no event, so the two groups do not
  exist in a trace until step 5 moves `GC Metrics` inside them. Built as two
  commits, the first is asserted on the wire and the second at this seam.
- **New seam needed:** none.
- **What makes a good test here:** assert the path a reader takes. A pause
  reaches its process through `process_track`; a counter reaches its
  interpreter by walking `parent_id`; every row inside an interpreter group
  has a parent, and `Interpreters` carries its process's `upid`. Asserting the
  descriptor's bytes would confirm a sub-message is absent and say nothing
  about whether the row still belongs to anyone.
- **Prior art:** `tests/exporters/test_perfetto_loss_track.py`, which reads a
  custom track under a process, and `TestTrackDescriptors`.
- **Cases:**
  1. Every row inside an interpreter group chains up to `Interpreters` on a
     non-NULL `parent_id`: `GC Pauses`, `GC Loss`, `heap_size` and
     `GC Metrics` under `Interpreter {iid}`, and those under `Interpreters`.
     `Interpreters` itself has `parent_id` NULL and reaches its process by
     `upid`, which is the flattening ADR-0003 accepted for a custom child of
     an OS-scoped parent. Asserting a parent on that row instead is the way
     this test fails after the change rather than before it. Fails today,
     where the interpreter rows are threads and the rest are flat.
  2. A process running N interpreters has N `GC Metrics` rows, each holding
     one interpreter's counters, and every counter names its interpreter
     through two `parent_id` hops. **Fails on `main` today**, where there is
     one merged row and the question has no answer.
  3. No row in `thread` carries a name, a `thread_track` or a slice. The
     nameless row the trace processor builds per process out of the
     `ProcessDescriptor` is the only thing left there, and `is_main_thread`
     marks it.
  4. Regression guard: `rss` and the `Lifetime` slices stay on the process
     track, the `Processes` spans and the process ordering are unchanged,
     slice args including `debug.iid` are unchanged, and
     `tests/fixtures/monitored_run_perfetto_trace.txt` moves only in the
     descriptors, not in the events.

## 6. Out of scope

- Renaming `InterpreterTrack` or `LossTrack`. They name the same two rows
  after this as before, and ADR-0024's rule that an event names its track is
  what makes the encoder free to re-parent underneath them.
- Ordering the `Interpreters` group against the process track it hangs beside.
  Its rank is discarded, because the process track is OS-scoped, and buying
  that back would need a third group above the process, which is a level too
  far for one row per process.
- The row pid scheme. It is what lets an iid equal a pid at all, but the fix
  is to stop describing interpreters as threads, and ADR-0011 owns the scheme
  for reasons that have nothing to do with interpreters.
- Whether a nested group is the right home for `heap_size` in the UI. ADR-0027
  accepts the two collapses it costs; a reader who wants it back at the top
  level is asking to reopen that trade-off, not to change this spec.

## 7. Further notes

Spec 0027 landed `tid=iid` on 2026-09-09 to make the interpreter id readable
out of `thread.tid`. The review of it found the `tid == pid` reading had moved
rather than gone: it left interpreter 0 and took up on whichever interpreter
equals the row pid. This spec was first written the same day as the other
direction, one step, drawing the interpreter row as a custom track. Measuring
that shape is what turned up the merged `GC Metrics`, which the one step does
not fix, and the spec was rewritten in place around the group that does.
