# 0069: Draw an interpreter's row as a custom track, not as a thread

- **Status:** **Pinned**
  (`TestTrackDescriptors::test_thread_tid_is_the_interpreter_id`,
  `TestTrackDescriptors::test_the_trace_processor_keeps_a_thread_whose_tid_is_the_pid`)
- **Kind:** bug (reporting)
- **Effort:** M
- **Origin:** the review of 0027's landing, 2026-09-09, and the two traces
  built from it
- **Respects:**
  [ADR-0002](../docs/adr/0002-perfetto-track-uuid-and-hierarchy.md) (every
  track is explicitly parented: the row keeps its uuid and its parent),
  [ADR-0003](../docs/adr/0003-gc-metrics-group-track.md) (ordering is dropped
  under an OS-scoped parent, which is why `GC Metrics` exists),
  [ADR-0011](../docs/adr/0011-process-lifetime-and-ordering.md) (section 4
  amends its thread-descriptor clause),
  [ADR-0024](../docs/adr/0024-an-event-names-the-track-it-is-drawn-on.md) (an
  event names its `Track`; the model is unchanged, only what the encoder
  derives from an `InterpreterTrack`)

## 1. Problem

Open a trace in the Perfetto UI and one interpreter per process is marked as
that process's main thread. It is not interpreter 0, and running the same
workload again marks a different one: the mark follows the order gcmon
discovered the processes in. In SQL it is `thread.is_main_thread`, and it
carries no meaning about the interpreter it lands on.

Underneath it is the same mistake in a second form. gcmon describes each
interpreter as an operating-system thread, which it is not, and the process's
real threads are nowhere in the trace. An interpreter row is a `thread`, so it
has no name and no process of its own in the `track` table, and a pause
reaches the interpreter that ran it only by joining back through the thread.

## 2. Evidence

`perfetto_format._emit_thread_descriptor` is the only place gcmon writes a
`thread` sub-message:

```python
    row_pid = state.get_row_pid(track.process)
    desc = build_track_descriptor(..., pid=row_pid, tid=iid, ...)
```

The trace processor sets `thread.is_main_thread` from `tid == pid`. Row pids
count from 1 (`PerfettoTrackState.get_row_pid`) and interpreter ids from 0, so
the flag lands on the interpreter whose iid equals its process's row pid, and
the row pid is handed out in discovery order.

Three processes, four interpreters each, one workload: the flag lands on iid 1
in the first process, iid 2 in the second, iid 3 in the third. In the same
trace every interpreter row reads `name = NULL`, no `upid`,
`type = thread_execution` in the `track` table.

The same run with the descriptor swapped for a plain custom track parented to
the process track, everything else untouched: every interpreter row is named
and carries its process (`type = process_merged_track_event`), every pause
joins to its process through `process_track` with no thread in the path, and
the only rows left in `thread` are the nameless ones the trace processor makes
per process, which hold no slices.

`_emit_loss_descriptor` already makes this choice for a `LossTrack`, for the
reason this spec generalizes: *"A plain custom track rather than a thread: a
`LossTrack` names an interpreter but no OS thread, and a `thread` sub-message
would describe one that does not exist."*

## 3. Scope

**Affected:** the Perfetto output, every trace. gcmon's rows leave the
`thread` and `thread_track` tables and appear in `track` and `process_track`,
so every query joining `thread_track` changes, in `docs/perfetto-sql.md` and
in the suite's `_process_filter`.

**Not affected:** the JSONL and stdout paths, which name an interpreter by its
`iid` and write no tid. Slice content and args, `debug.iid` included, which is
what a query attributes a pause with once the tid is gone. The `GC Metrics`
group and the counter tracks, already parented to the process track.
`GC Loss`, already a custom track. The `Processes` track, the `Lifetime`
slices, and the row pid, which the `ProcessDescriptor` still carries.

**Why the suite didn't catch it:** nothing asserted on `is_main_thread` until
0027 landed. The tests that assert it now state the current shape, which is
what makes this spec Pinned: they change in the same commit as the code.

## 4. Proposed change

1. `_emit_thread_descriptor` builds a plain custom track: the name it already
   uses, parented to the process track as now, with no `thread` sub-message
   and so no `tid` and no `pid`. It is the encoder's last `thread` field, so
   after this gcmon writes none.
2. `sibling_order_rank` stays as it is on both the interpreter row and the
   `GC Loss` row beneath it. The parent is the process track, which is
   OS-scoped, so the trace processor drops the rank either way (ADR-0003);
   moving that is section 6's, not this spec's.
3. `docs/perfetto-sql.md` loses the `thread` bullet and the tid note 0027
   added, and its examples join `process_track`. An interpreter is named by
   the row it draws on and by `debug.iid` on each slice.
4. ADR-0011's thread-descriptor clause is rewritten: the row pid reaches the
   trace through the `ProcessDescriptor` alone, and no thread claims it.
   ADR-0024 gains the sentence that an `InterpreterTrack` draws as a custom
   row, beside the `LossTrack` that already does.
5. The CHANGELOG carries one breaking-change line: a trace holds no `thread`
   rows of gcmon's, and a query that joined `thread_track` joins
   `process_track`.

Against the records in the header: ADR-0002 holds, since the row keeps its
sequential uuid and its explicit parent. ADR-0003 holds and explains step 2.
ADR-0011 is amended by step 4, which is the one clause this work overturns.
ADR-0024 holds: `InterpreterTrack` is unchanged and no event moves.

## 5. Seams and testing decisions

- **Seam:** the trace processor, via
  `tests/exporters/test_perfetto_exporter_integration.py`. It is the highest
  seam that can see the change and the only one that says what the trace means
  rather than what bytes were written.
- **New seam needed:** none.
- **What makes a good test here:** assert the path a reader takes. A pause
  joins to its process through `process_track`, the row it sits on is named,
  and `thread` holds no row gcmon wrote. Asserting the descriptor's bytes
  would confirm the sub-message is absent and say nothing about whether the
  row still belongs to its process.
- **Prior art:** `tests/exporters/test_perfetto_loss_track.py`, which reads a
  custom track under a process, and `TestTrackDescriptors`.
- **Cases:**
  1. Every `GC Pause` joins to its own process through `process_track`, on a
     row named for its interpreter. Fails today, where the join needs a
     thread.
  2. Every row in `thread` is nameless and holds no slice, so no interpreter
     carries `is_main_thread` and none can.
  3. Regression guard: the counter tracks, the `GC Metrics` group, `GC Loss`,
     the `Lifetime` slices and the `Processes` spans are unchanged, slice args
     including `debug.iid` are unchanged, and
     `tests/fixtures/monitored_run_perfetto_trace.txt` moves only where a
     thread descriptor stood.

## 6. Out of scope

- Renaming `Thread {iid}`. The row stops being a thread and the name should
  follow, but it is also the counter tracks' prefix (`Thread 0 heap_size`) and
  every documented example, and section 1's complaint is answered without it.
- A grouping track to make interpreter order explicit. It costs a nesting
  level in every process, and what would settle it is whether the trace
  processor honors a rank on a non-OS-scoped child of an OS-scoped parent,
  which the seam in section 5 can measure once the rows are custom.
- The row pid scheme. Counting from 1 is what makes the flag land on an
  interpreter at all, but the fix is to stop describing interpreters as
  threads, not to move the pids out of their way.

## 7. Further notes

0027 landed `tid=iid` on 2026-09-09 to make the interpreter id readable out of
`thread.tid`. The review of it found the `tid == pid` reading had moved rather
than gone: it left interpreter 0 and took up on whichever interpreter the row
pid met. This spec is the other direction, and it retires the question instead
of answering it.
