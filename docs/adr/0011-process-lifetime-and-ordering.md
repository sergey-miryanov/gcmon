# ADR-0011: Show process lifetimes on one shared track, ordered by first observation

- **Status:** Accepted
- **Date:** 2026-06-27
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

**Crossing spans.** Putting every pid on one track has a constraint the
original design missed: slices on a single Perfetto track are a *stack*. A
`TYPE_SLICE_END` force-closes everything stacked above the slice it closes, so
a pair that merely crosses (A starts first, B starts inside A, B ends after A)
cannot be expressed. Given pid 1111 `[100ms, 400ms]` and pid 2222
`[200ms, 600ms]`, the trace processor returns pid 2222 with a 200ms duration
and reports `misplaced_end_event: 1`. The failure is quiet: the slice table
still holds one row per pid, and only the durations are wrong. gcmon monitors
a process *tree*, so any two siblings whose lifetimes overlap without nesting
cross. The repository's own integration fixture crossed, and every assertion
in the suite passed anyway.

## Decision

**A single shared top-level track named `Processes`** holds one
`TYPE_SLICE_BEGIN`/`TYPE_SLICE_END` pair per process, spanning
`[first observed, last observed]` for that process;
[ADR-0029](0029-report-liveness-and-fold-it-into-the-span.md) says what counts
as an observation. A span never covers a stretch in which the process it names
did not exist.

**A slice is named for its process**, `Process <pid>` for the first to hold
the pid and `Process <pid>#N` after it, the suffix the `--stats` table prints
([ADR-0016](0016-the-ring-is-the-statistics-unit.md)). The END repeats the
suffix, since matching is by name and two spans on one pid would otherwise
share a BEGIN.

- Parented to the trace root, so `parent_uuid` is **absent on the wire**, not
  `0`, which is the reserved root descriptor
  ([ADR-0002](0002-perfetto-track-uuid-and-hierarchy.md)).
- No `process` / `thread` / `counter` sub-message, no `child_ordering` (it is
  a leaf: it has slice events, not child tracks), no `sibling_order_rank` (it
  is neither an explicit-ordered child nor a process or thread track, so the
  field would be ignored).
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

**The whole track is emitted at encoder close**, once per trace; convert
passes record spans and emit nothing. Two reasons the BEGIN cannot go out
earlier: keeping the track laminar needs every pid's span in hand at once, and
a clip discovered at close cannot correct a BEGIN already written. Nor could
the END, since `PerfettoExporter` flushes in chunks of `flush_threshold` and
Perfetto pairs a BEGIN with the **first** matching END, orphaning the rest.

**Spans are clipped to a laminar set.** Sorted by ascending start, ties broken
by longer span first and then by process, a stack sweep pulls each crossed
span's end back to one nanosecond before the span that crosses it. Nesting is
untouched, so a parent outliving its children costs nothing. Spans that merely
touch (`A.end == B.start`) count as crossing when B extends past A, because
the wire format does not pin down the relative order of an END and a BEGIN
sharing a timestamp; a B that both starts and ends at `A.end` is nested, and
is left alone. Sorting longer-first on equal starts is what makes the clip
safe: two spans with the same start always nest, so a clip only happens when
`A.start < B.start`, and `B.start - 1` never lands before `A.start`.

**Each span is emitted as an adjacent BEGIN/END pair, not interleaved into
stack order.** The trace processor sorts by timestamp and breaks ties by
position in the sequence, so order decides anything only where events share a
timestamp. Two ENDs at one timestamp need no rule, because gcmon names every
END and the trace processor matches it to the BEGIN with that name rather than
to the top of the stack, so an END cannot close the wrong slice. It does
force-close anything sitting *above* the slice it matched, and that is what
makes the other two collisions matter. Each is owned by one function and
neither is optional: building the pair BEGIN-first belongs to the emission
site, because a zero-length span emitted END-first reads as `dur = -1`;
putting the outer BEGIN of two spans sharing a start ahead of the inner
belongs to the sweep, because `[(100, 2, 6), (101, 2, 3)]` emitted inner-first
gives pid 100 a duration of 1 instead of 4 plus a `misplaced_end_event`. The
fuzz suite below checks both against the trace processor. The sweep's sort is
load-bearing twice over: longer-first on a tie keeps the clip safe *and*
orders these BEGINs. The sweep therefore sorts its own input instead of
documenting the order as a precondition.

The sweep and the emission order together, on the crossing shape from the
Context plus a nested third process (in ns, so the clip lands on 199):

```
                100     200     300     400     500     600  ns
observed
  pid 1111      [=======================]
  pid 2222              [===============================]
  pid 3333              [=======]

sorted by (start, -end, pid)      1111, then 2222 before 3333 -- tie on
                                  start 200, longer span first

after the sweep                   2222 crosses 1111, so 1111's end is pulled
  pid 1111      [======]          back to 199; 3333 nests inside 2222 and is
  pid 2222              [===============================]    left alone
  pid 3333              [=======]

emitted in that order, one adjacent pair per span
  BEGIN 1111 @100   END 1111 @199
  BEGIN 2222 @200   END 2222 @600
  BEGIN 3333 @200   END 3333 @300
        ^                    ^
        |                    +- 199, not 200: the sweep clipped 1111 apart
        |                       from the span that crosses it
        +- 2222 and 3333 share start 200; the longer one is emitted first,
           so 3333 opens inside it rather than outside. Reversed, 3333's
           END would force-close 2222 along with it
```

**Every slice carries `pid`, `pid_epoch`, `real_start_ts` and `real_end_ts`
debug annotations** on its BEGIN, holding which process this is and the span
as observed. They go on *every* slice, not only clipped ones and not only
reused pids, so a consumer reads the operating system's pid, the epoch and the
observed span without an annotation-present check and without parsing the
name. `pid` has nowhere else to go: `TrackDescriptor` has no free-form args
field, `description` is the command line
([ADR-0010](0010-process-identity-cmdline-and-start-marker.md)), and
`ProcessDescriptor` carries only `pid`, `cmdline`, `process_name` and
`start_timestamp_ns`, none of them free once `pid` holds the row's
([ADR-0028](0028-draw-every-process-a-row-of-its-own.md)). Where `ts`/`dur`
and the annotations disagree, the annotations are the truth.

**The shared slice says `clipped`.** A process's own row draws the observed
pair (ADR-0028), so the two rows disagree for a clipped process, and the flag
shows a reader which processes diverge instead of leaving them to subtract one
row's duration from the other's. It goes on every slice, true or false. The
`Lifetime` slice cannot carry it: a retired process's row is drawn at the next
flush and the sweep has decided nothing yet.

**No span is dropped.** A pid observed at a single instant, and a pid clipped
down to nothing, both still get a BEGIN/END pair; the trace processor accepts
it and reports `dur = 0`. A missing slice would leave no record that the
process was monitored at all.

## Consequences

- You can read each process's lifetime beside the others', and the same events
  in a different input order produce the same ranks.
- **A clipped slice under-reports how long the process was observed**, by an
  amount that depends on how close together the starts are, not on how much
  the spans overlap: the clip is to `later.start - 1`. Siblings fanning out
  from a fork loop start microseconds apart, so each keeps microseconds of a
  lifetime that ran for seconds. Liveness (ADR-0029) cuts the other way for
  part of a fan-out: children whose earliest evidence is the tick that first
  polled them share that timestamp and nest rather than clip, while those
  whose first GC event predates the poll keep their jitter.
- **`--rss` adds no jitter of its own.** The sampler stamps a whole pass with
  the tick instant it was given ([ADR-0013](0013-rss-sampling.md)), so those
  spans share a start and nest rather than clipping each other.
- **The drawn duration is a lower bound, never an upper one**, so deaths are
  misreported as early rather than late. `real_end_ts - real_start_ts`
  recovers what was observed, as does the `Lifetime` slice on the process's
  own row; `docs/perfetto-sql.md` carries the query.
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
- **Deep nesting is now the normal shape.** Processes still alive when the
  loop stops share an end timestamp, and the sweep reads an outer span ending
  at or after the inner one as enclosing it, so co-terminating spans nest one
  level per process instead of clipping. Staggered deaths still clip, so
  traces mix both.
- **The trace processor closes at most 512 nested slices.** The fuzz suite
  measures this against the real trace processor: at 512 every slice reads
  back intact, and each level past it leaves one more with `dur = -1`. The
  loss is **silent**, since `misplaced_end_event` stays 0 and no other
  non-info stat is raised. gcmon writes a well-formed pair for every span
  either way, so the limit sits in the reader. Bounding nesting depth is out
  of scope.
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
- The `Processes` block lands at the end of the file, descriptor first. The
  trace processor resolves track references across the whole trace rather than
  in file order.
- Consumers enumerating slices must filter `track.name == 'Processes'`, since
  these slices are Perfetto-only.
- The emission-order claims are settled by a randomized differential test that
  asserts the rejected orderings break, so the case that passes cannot pass by
  the order being irrelevant.
- [ADR-0015](0015-gc-loss-spans-on-their-own-track.md) needs no sweep: its
  loss windows are one per poll interval and meet without overlapping. Its
  `GC Loss` track is separate so a reader can tell intervals gcmon recorded
  from intervals it lost.

## Alternatives considered

- **One lifetime track per pid**, representing crossing spans with no
  clipping. Rejected: gcmon runs on captures with hundreds to thousands of
  processes, and a track per pid makes the timeline unreadable. A collapsible
  parent group does not help; the row count is the problem.
- **Packing spans into lanes**, colouring the interval graph so the row count
  is maximum *concurrency* rather than process *count*. Better than a track
  per pid (8 workers running 1000 tasks needs 8 rows), but rejected: N
  children alive at once are N mutually crossing intervals and still need N
  lanes, so the timeline is as unreadable as before.
- **Dropping a slice that ends up zero-length.** Rejected: it optimises the
  rendering at the cost of the record, and the pids likeliest to be clipped to
  nothing are the short-lived children a reader is looking for.
- **Snapping near-equal starts together before the sweep**, turning a jittered
  fan-out back into the nesting it almost is; every end survives at a cost of
  at most ε on each start, and a clipped fan-out keeps its whole observed
  duration instead of microseconds of it. Not adopted: ε is a heuristic,
  nesting N deep costs N rows of vertical space inside the track, and the
  trace processor stops closing slices past 512 (see Consequences).
  [Spec 0070](../../specs/0070-keep-a-fanned-out-processs-width-on-the-processes-track.md)
  specifies it.
- **Extending the earlier span's end instead of clipping it**, nesting the
  later span inside. Rejected: it makes a dead process look alive, and the
  nesting implies a parent/child relationship that may not exist.
- **Clipping whichever side loses fewer nanoseconds.** Rejected: it makes the
  direction of the distortion depend on the data rather than on a stated rule.
- **Leaving crossing spans alone and documenting the mismatch.** Rejected: the
  durations are wrong, and `misplaced_end_event` is not something a reader of
  the UI would think to check.
- **Snapping every pid in a tick to one timestamp**, making spans that share a
  start nest rather than cross, so the "snap near-equal starts" alternative
  above lands with no ε to choose. Noted and not taken: the benefit is an
  artifact of `--rate` and would degrade silently as the rate drops.
- **Emitting the slice END at the end of each convert call.** Rejected: a run
  flushes many times, and Perfetto pairs a BEGIN with the first matching END,
  orphaning the rest.
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
  bound to a row, and a counter event on a track described later is dropped
  outright.
