# ADR-0027: Group every row an interpreter owns under one track

- **Status:** Accepted
- **Date:** 2026-09-10

## Context

gcmon draws an interpreter as an operating-system thread, and parents
everything else that interpreter owns to the process track. The thread row
carries a `ThreadDescriptor` holding the process's row pid and the iid as its
`tid`, which is the only way a track descriptor can claim a thread.

The trace processor derives `thread.is_main_thread` from `tid == pid`. gcmon
numbers row pids from 1, in the order it discovers processes, and CPython
numbers interpreters from 0, so the flag lands on the interpreter whose iid
equals its process's row pid. Three processes running four interpreters each,
one workload: it marks iid 1, iid 2 and iid 3. gcmon writes none of the
target's own threads, so there is no row the flag belongs on.

A thread row carries no name and no process of its own in the `track` table,
so a pause reaches the interpreter that ran it only by joining back through
`thread_track` and `thread`.

gcmon emits a `GC Metrics` group per `(process, iid)` and parents each to the
process track, and a trace comes back with one `GC Metrics` row per process
holding every interpreter's counters. `G0 collected` appears in it once per
interpreter, identically named, and nothing says which interpreter each
belongs to. `heap_size` is the one counter a query can attribute, because
[ADR-0024](0024-an-event-names-the-track-it-is-drawn-on.md) has the converter
write the interpreter into its track name.

Two rules govern what the process track does to its children.
[ADR-0003](0003-gc-metrics-group-track.md) found the first in
`track_descriptor.proto`: a parent carrying a `process` or `thread`
sub-message is OS-scoped, so its `child_ordering` is ignored and its
children's `sibling_order_rank` is discarded. The proto documents no second
one, and a trace shows it: under one process, custom tracks sharing a name and
a parent become one row. The process track is OS-scoped, so everything
parented straight to it is unordered, and whatever shares a name there is
merged.

## Decision

**Every row an interpreter owns is parented to a group named
`Interpreter {iid}`**, one per `(process, iid)`, carrying
`child_ordering = EXPLICIT`. It holds no events.

**One `Interpreters` group per process holds those groups**, parented to the
process track and carrying `child_ordering = EXPLICIT`. It is the
non-OS-scoped parent that makes the trace processor honor an interpreter
group's `sibling_order_rank`, which is the iid. It holds no events, and a
process gcmon read a record from gets one whether it ran one interpreter or
many. A process gcmon only ever polled gets none, because nothing inside it
was ever drawn on.

**The encoder cannot write a `ThreadDescriptor`.** The builder loses the
fields that produce the sub-message, and the root track descriptor loses
`thread_ordering`, which orders thread tracks gcmon no longer has.
`process_ordering` stays, since processes are still ordered
([ADR-0011](0011-process-lifetime-and-ordering.md)).

**The group carries the iid, so no row inside it repeats the number.** The
pause row is `GC Pauses`, the loss row `GC Loss`, the consolidated counter
`heap_size`, and the counter group `GC Metrics`, ranked in that order inside
the group.

**`rss` and a process's marks stay on the process track.** A `ProcessTrack`
owns them, so parenting them to the process row is their identity rather than
a policy ([ADR-0024](0024-an-event-names-the-track-it-is-drawn-on.md)).

**The parent chain says which interpreter owns a row.** A counter is a child
of `Interpreter {iid}` or a grandchild of it through `GC Metrics`, and nothing
sits deeper, so two hops reach the group from any counter. A pause keeps the
`debug.iid` annotation it already carries.

The tree a trace holds, for one process running two interpreters:

```
Process 12345
Interpreters
  Interpreter 0
    GC Pauses
    GC Loss
    heap_size
    GC Metrics
      G0 collected, G0 candidates, G0 duration, ...
  Interpreter 1
    ...
```

`GC Loss` is there for an interpreter that lost records and absent for one
that did not. gcmon writes a loss event only for a poll interval that lost
something, and a row exists because an event names it
([ADR-0024](0024-an-event-names-the-track-it-is-drawn-on.md)).

## Consequences

- A query can name the interpreter a per-generation counter belongs to, which
  none could do before.
- The `thread` table keeps one nameless row per process, which the trace
  processor builds from the `ProcessDescriptor` with `tid` equal to the row
  pid, and `thread.is_main_thread` marks it. gcmon cannot drop it while it
  writes processes, and nothing in gcmon produces it to grep for.
- A query reaches a pause through `process_track` rather than `thread_track`.
- A group is a row only once something in its subtree carries an event. The
  trace processor builds no `track` row for a descriptor whose whole subtree
  is eventless, the process track included, so neither group appears in a
  trace until a row inside it is drawn on. Nothing has to suppress them for a
  process that collected nothing: they are already absent.
- A counter track is named `heap_size` again, so a query matching
  `name = 'heap_size'` finds it. ADR-0024 broke that match when it qualified
  the name with the interpreter.
- **Accepted trade-off:** a reader reaches `heap_size` by expanding
  `Interpreters` and then `Interpreter {iid}`, two groups that did not exist
  before.
- **Accepted trade-off:** a process running one interpreter pays two nesting
  levels for guarantees it cannot use: `Interpreters` has one group to order,
  and `Interpreter {iid}` has no sibling to keep it apart from. The trace's
  depth does not then depend on how many interpreters a run happened to have.
- `Interpreters` renders beside `Process <pid>` rather than inside it, by the
  same rule and with the same consequence ADR-0003 accepted for `GC Metrics`.
  It is one row per process rather than several, and it keeps its `upid`.
- Clauses elsewhere are void and move with this record: ADR-0003's parenting
  of `GC Metrics` to the process track, ADR-0011's thread-descriptor clause,
  ADR-0024's `heap_size` qualifier, the top-level clause ADR-0004 keeps in its
  supersession note, ADR-0015's parenting of the loss row, and the track kinds
  ADR-0002 and ADR-0016 enumerate. ADR-0003's finding stands, and this record
  is built on it: a custom group buys back the ordering an OS-scoped parent
  discards.

## Alternatives considered

- **A custom track per interpreter, parented straight to the process track.**
  Fixes the false thread and the missing name. The interpreter rows stay
  unordered, because their parent is still OS-scoped, and the `GC Metrics`
  copies still merge.
- **Keep `heap_size` on the process track.**
  [ADR-0004](0004-toplevel-shared-counters.md) drew it there so a reader saw
  it without expanding the `GC Metrics` group, and paid a loose row beside the
  process for it. That was worth it for one heap size per process. Four
  interpreters make it four loose rows in an order the UI picks, and the group
  puts each one under the interpreter that owns it.
- **Keep `Thread {iid}` and the `Thread {iid} heap_size` qualifier.** Halves
  the churn, and leaves the word "thread" in a trace for a thing that is not
  one, inside a row that already says which interpreter it is.
- **Emit `Interpreters` only for a process running more than one
  interpreter.** Rejected as unimplementable rather than undesirable, for the
  reason ADR-0024 rejected the same shape for the `heap_size` name: gcmon is a
  streaming writer and does not know at descriptor time whether a sibling will
  appear.
- **Carry the iid as a track argument, so attribution is one join.** The
  descriptor's `description` field reaches the `args` table and would hold it.
  Rejected: `description` is a tooltip a human reads, and a row would then
  carry two sources of truth about who owns it that can disagree.
- **Keep `tid = iid` and live with the flag.** What gcmon does today, landed
  2026-09-09 to make the iid readable out of `thread.tid`. It moved the
  `tid == pid` reading off interpreter 0 onto whichever interpreter equals the
  row pid rather than retiring it.
- **Renumber row pids so no iid can equal one.** Moves the collision without
  addressing the thread that should not exist, and ADR-0011 owns the pid
  scheme for reasons that have nothing to do with interpreters.

## Implementation

- `src/gcmon/exporters/perfetto_format.py` derives an interpreter's group, the
  process's `Interpreters` group and the rows inside both, and holds the ranks
  and the track names.
- `src/gcmon/exporters/perfetto_track_state.py` keys a uuid per interpreter
  group and one per `Interpreters` group, beside the tables it already keys on
  a `Track`.
- `src/gcmon/exporters/perfetto_builders.py` no longer encodes a
  `ThreadDescriptor`, and `src/gcmon/exporters/perfetto_proto.py` drops the
  field numbers behind it: `ThreadDescriptorField` and `ThreadOrdering`, and
  the `thread` and `thread_ordering` entries in `TrackDescriptorField`. A
  field number nothing writes is a way back in.
- `src/gcmon/exporters/trace_converter.py` writes `heap_size` as the display
  name.
- `src/gcmon/model/trace_event.py` keeps its shape: no event names a group,
  and `InterpreterTrack` and `LossTrack` name the same two rows as before.
  Only `InterpreterTrack`'s docstring moves, off the thread it described.
- Tests: `tests/exporters/test_perfetto_exporter_integration.py` asserts the
  hierarchy through the trace processor, that a `GC Metrics` row exists per
  interpreter rather than per process, and that no row in `thread` carries a
  name or a slice.
- `docs/perfetto-sql.md` teaches the two-hop parent join.
