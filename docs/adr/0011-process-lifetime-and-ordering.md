# ADR-0011: Show process lifetimes on one shared row, ordered by first observation

- **Status:** Accepted
- **Date:** 2026-09-20
- **Amended by:** [ADR-0013](0013-rss-sampling.md),
  [ADR-0015](0015-gc-loss-spans-on-their-own-track.md),
  [ADR-0027](0027-group-every-row-an-interpreter-owns.md),
  [ADR-0028](0028-draw-every-process-a-row-of-its-own.md),
  [ADR-0029](0029-report-liveness-and-fold-it-into-the-span.md)
- **Modules:** exporters

## Context

[ADR-0010](0010-process-identity-cmdline-and-start-marker.md) gives each pid a
`Process <pid>` track and keeps it visible. Two gaps remained.

**Monitoring duration.** A process track in isolation says nothing about span,
and nothing groups the processes for cross-process comparison.

**Track order.** Process tracks came out in dict-insertion order, so the same
input in a different arrival order produced a differently-ordered trace.
Perfetto's mechanism here is `sibling_order_rank` on each process track
descriptor, but it is consulted for process tracks only when the special root
descriptor at `uuid = 0` carries
`process_ordering = PROCESS_ORDERING_EXPLICIT`. This is the same
OS-scoped-parent rule [ADR-0003](0003-gc-metrics-group-track.md) ran into,
seen from the other side: for process and thread tracks, ordering is
configured on the root rather than on the parent.

**Crossing spans.** Slices on a single Perfetto track are a *stack*. A
`TYPE_SLICE_END` force-closes everything stacked above the slice it closes, so
a pair that merely crosses (A starts first, B starts inside A, B ends after A)
cannot be expressed on one track. Given pid 1111 `[100ms, 400ms]` and pid 2222
`[200ms, 600ms]`, the trace processor returns pid 2222 with a 200ms duration
and reports `misplaced_end_event: 1`. The failure is quiet: the slice table
still holds one row per pid, and only the durations are wrong. gcmon monitors
a process *tree*, so any two siblings whose lifetimes overlap without nesting
cross. The repository's own integration fixture crossed, and every assertion
in the suite passed anyway. Perfetto's answer to an overlapping pair is a
track for each, merged into one row by their shared name.

## Decision

**One shared row named `Processes`, built from a track per process.** Each
process gets a top-level track carrying one
`TYPE_SLICE_BEGIN`/`TYPE_SLICE_END` pair, spanning
`[first observed, last observed]` for that process;
[ADR-0029](0029-report-liveness-and-fold-it-into-the-span.md) says what counts
as an observation. A span never covers a stretch in which the process it names
did not exist. Every one of those tracks carries the same name, which is what
the trace processor merges them by, packing the spans into lanes of one row
for the UI to draw.

**A slice is named for its process**, `Process <pid>` for the first to hold
the pid and `Process <pid>#N` after it, the suffix the `--stats` table prints
([ADR-0016](0016-the-ring-is-the-statistics-unit.md)). A process is one holder
of a pid, so a reused pid gets a track per holder. The END carries no name:
its track holds this one span and nothing else it could close.

- Parented to the trace root, so `parent_uuid` is **absent on the wire**, not
  `0`, which is the reserved root descriptor
  ([ADR-0002](0002-perfetto-track-uuid-and-hierarchy.md)).
- No `process` / `thread` / `counter` sub-message, no `child_ordering` (it is
  a leaf: it has slice events, not child tracks), no `sibling_order_rank` (it
  is neither an explicit-ordered child nor a process or thread track, so the
  field would be ignored), and neither `sibling_merge_key` nor
  `sibling_merge_behavior`: the shared name is what the default merge keys on.
- Perfetto-only. JSONL is unchanged.

**A root `TrackDescriptor` at `uuid = 0`** is emitted once per trace with
`process_ordering = EXPLICIT` (field 19) and nothing else: no name, no parent,
no sub-message. There is no thread ordering to ask for, since nothing gcmon
draws is a thread track
([ADR-0027](0027-group-every-row-an-interpreter-owns.md)). The UI reads the
hint only on the canary channel of `ui.perfetto.dev` (Flags -> Release channel
-> Canary), and a trace processor older than 0.57 ignores it and orders tracks
its own way. gcmon writes it whatever the reader, so a trace stays
forward-compatible.

**Process tracks are ranked by first observation**, ties broken by ascending
process, sequential from 0. Every process with a recorded span gets a rank,
including one known only from liveness (ADR-0029).

**A rank comes off a counter that only goes up, and the processes described
together are sorted before they draw from it.** A descriptor is written once,
at the first flush that names its process, so the rank it carries can never be
revised. Ranking the whole accumulator each time gave two processes the same
number: a process reached in a later batch can have been observed earlier,
because a first poll drains the whole ring and its oldest record predates the
poll. Sorting settles the order among the processes described together; the
counter settles it between one group and the next, in the order gcmon reached
them. gcmon cannot do better than that, since it cannot rank a process against
one it has not reached, and a process reached later was started later except
on the first tick, where every process is reached at once and sorted together.

**A span goes out once it is final.** A process gcmon has retired gets its
span at the flush that draws its own row: no span is measured against another,
so one that is complete can be written on its own
([ADR-0028](0028-draw-every-process-a-row-of-its-own.md)). A process still
held when the run stops is drawn at encoder close. For a live process the END
cannot go out earlier, since `PerfettoExporter` flushes in chunks of
`flush_threshold` and Perfetto pairs a BEGIN with the **first** matching END,
orphaning the rest.

**Each span is written as an adjacent BEGIN/END pair, BEGIN first.** The trace
processor sorts by timestamp and breaks ties by position in the sequence, so
the order matters only where two events share a timestamp. The pair of a
process observed at a single instant is that case: written END-first it reads
as `dur = -1`.

The crossing shape from the Context, with a third process inside the second
(in ns):

```
                100     200     300     400     500     600  ns
  pid 1111      [=======================]
  pid 2222              [===============================]
  pid 3333              [=======]
```

All three are drawn at those widths. The row is three lanes tall, because at
250 three processes are alive.

**Every slice carries `pid` and `pid_epoch` debug annotations** on its BEGIN,
holding which process this is. They go on *every* slice, not only on a reused
pid's, so a consumer reads the operating system's pid and the epoch without an
annotation-present check and without parsing the name. `pid` has nowhere else
to go: `TrackDescriptor` has no free-form args field, `description` is the
command line ([ADR-0010](0010-process-identity-cmdline-and-start-marker.md)),
and `ProcessDescriptor` carries only `pid`, `cmdline`, `process_name` and
`start_timestamp_ns`, none of them free once `pid` holds the row's
([ADR-0028](0028-draw-every-process-a-row-of-its-own.md)). `ts` and `dur` are
the observed pair, so no annotation restates either.

**A span is identified by its slice name or its annotations, never by
`track_id`.** The trace processor folds mergeable descriptors as it imports
them: a span lands on a track of its merge group whose stack is empty, so one
`track` row holds spans of different processes. `track.name = 'Processes'`
still selects the whole row, and nothing nests inside a span on it.

**No span is dropped.** A pid observed at a single instant still gets a
BEGIN/END pair; the trace processor accepts it and reports `dur = 0`. A
missing slice would leave no record that the process was monitored at all.

## Consequences

- You can read each process's lifetime beside the others', and the same events
  in a different input order produce the same ranks.
- **`dur` on a `Processes` slice is a duration gcmon observed.** A query reads
  it directly, and a death is reported where it was seen.
- **The row is as tall as the largest number of processes alive at once**, so
  a tree whose processes all overlap gets a lane for each, and the `track`
  table holds that many `Processes` rows. That is the price of the widths, and
  bounding the height is out of scope.
- **A lane carries no meaning.** The fold packs by time alone, so which lane a
  span lands in depends on the spans before it and says nothing about
  parentage. The tree is readable from `pid` and from the per-process rows
  (ADR-0028).
- **A killed run keeps the span of every process gcmon had retired**, beside
  the row ADR-0028 drew for it at the same flush. Only a process still running
  when the run died loses its span.
- **An observation folded in after a retired process's pair went out is not
  drawn.** Both ends are in the file by then, and Perfetto pairs a BEGIN with
  the first matching END, so a second pair would draw the process twice rather
  than widen it. Nothing should arrive: gcmon has let go of the pid, and a
  record read afterwards belongs to whatever holds it now (ADR-0025). An event
  already queued when a process retires is not late, since the exporter
  serializes a flush against a retirement and the queued one reaches the
  accumulator first. One that did arrive late would leave the pair short at
  whichever end it would have moved, the accumulator being a min/max over both
  (ADR-0029), which is the cost ADR-0028 accepts for a control-plane instant
  arriving after the row is drawn.
- **One `Processes` slice per process gcmon polled**, so a consumer joining
  slices to pids joins many to one and reads `pid_epoch` to tell them apart. A
  process that answered a single poll and never collected gets one; only a pid
  seen through meta events alone has none.
- **A span covers only its own process, even where a read crossed the
  boundary.** A pid pruned from the tree loses its read cursor, and a
  successor re-reads what its predecessor produced. Each record is drawn on
  the process that made it
  ([ADR-0025](0025-create-every-process-in-one-place.md)), so those timestamps
  land in the predecessor's span.
- **A process track's `sibling_order_rank` reaches no SQL table.** It is a UI
  hint, so the trace-processor tests act as a *schema-validity guard*: they
  confirm the layout is accepted and the `process` and `track` tables survive
  intact, but only the Perfetto UI can assert the order of process tracks. A
  rank under an explicit group does reach one
  ([ADR-0014](0014-perfetto-integration-test-strategy.md)). Perfetto's docs
  call these orderings "strong hints" in any case, so the UI may still
  rearrange tracks in special contexts.
- **A process observed before one already described still sorts after it.**
  The rank is right within each group and follows the order gcmon reached them
  between groups. A process adopted mid-run that predates every other is the
  case this reads wrong, and `sibling_order_rank` is a UI hint, so the cost is
  the order of two adjacent rows.
- A process still held at close lands at the end of the file, descriptor
  first. The trace processor resolves track references across the whole trace
  rather than in file order.
- Consumers enumerating slices must filter `track.name == 'Processes'`, since
  these slices are Perfetto-only.
- **The widths are settled against the real trace processor** (ADR-0014): a
  randomized suite reads every span back and compares it with the one that
  went in. `misplaced_end_event` is what a lost pairing raises, and its
  severity is `data_loss`, so a check filtering on `error` alone never sees
  it.
- **A reader that merges nothing draws a row per process**, each span intact.
  Perfetto moved the merge out of the UI and into the trace processor in
  v52.0, and the UI merged by name before that, so a reader from either side
  of that release draws one row. This raises no minimum version of its own;
  the root descriptor's ordering hint above already sets one.
- [ADR-0015](0015-gc-loss-spans-on-their-own-track.md) needs no row of this
  shape: its loss windows are one per poll interval and meet without
  overlapping, so one track per interpreter holds them. Its `GC Loss` track is
  separate so a reader can tell intervals gcmon recorded from intervals it
  lost.

## Alternatives considered

- **One shared track, with crossing spans clipped to a laminar set.** Sorted
  by ascending start, a stack sweep pulls each crossed span's end back to one
  nanosecond before the span that crosses it, which expresses every span on
  one track. Rejected: the clip lands at `later.start - 1`, so what a span
  loses depends on how close the two starts are rather than on how much the
  spans overlap. A sibling fanning out from a fork loop keeps the gap between
  its start and the next one's, not the lifetime it ran for. `dur` on the row
  is then not a duration gcmon observed, recovering one takes two annotations
  on every slice, and a reader has to know to subtract them.
- **`SIBLING_MERGE_BEHAVIOR_NONE`, giving each process a row.** Rejected:
  gcmon runs on captures with hundreds to thousands of processes, and a row
  per process makes the timeline unreadable. A collapsible parent group does
  not help; the row count is the problem. A lane costs the height of one
  slice, where a row costs a header and an entry in the track list, so peak
  concurrency in lanes is cheaper than process count in rows.
- **A shared `sibling_merge_key` on every `Processes` track.** Rejected: it
  says what the shared name already says, and it costs the hand-rolled encoder
  ([ADR-0001](0001-hand-rolled-perfetto-protobuf-encoder.md)) two fields whose
  only test is that the merge still happens.
- **Laying the lanes out by the process tree**, each child under its parent.
  Rejected: the fold chooses the lane, and neither it nor the stock UI takes a
  depth gcmon computed.
- **Dropping the slice of a process observed at a single instant.** Rejected:
  it optimises the rendering at the cost of the record, and the pids it drops
  are the short-lived children a reader is looking for.
- **Emitting the slice END at the end of each convert call.** Rejected: a live
  process's span is not final, a run flushes many times, and Perfetto pairs a
  BEGIN with the first matching END, orphaning the rest.
- **Re-emitting a process descriptor with a corrected rank in a later batch.**
  Rejected: it breaks idempotent emission for a cosmetic gain in a rare
  ordering, and it does not work. The trace processor keeps the first
  descriptor for a uuid and drops the second, reporting nothing.
- **Deriving the rank from the process alone**, as milliseconds between a
  fixed reference and its first observation, so no group has to be sorted.
  Rejected: every available reference fails. The earliest timestamp folded in
  moves as processes arrive, which is the problem being solved; the first one
  folded in makes the rank an artefact of the order events reach the encoder;
  and a clock read makes a trace of the same run unreproducible.
- **Holding a process's whole subtree back until its rank settles**, releasing
  it at the flush after its tick closes. Rejected: it buys ordering across
  groups at the price of the rows a killed run keeps
  ([ADR-0010](0010-process-identity-cmdline-and-start-marker.md)), and only
  the whole subtree can move. A process descriptor arriving after the rows
  beneath it loses the per-process split (ADR-0028), since the pid is already
  bound to a row, and a counter event on a track described later is dropped.
