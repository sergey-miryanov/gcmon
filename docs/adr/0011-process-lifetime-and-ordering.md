# ADR-0011: Show process lifetimes on one shared track, ordered by first observation

- **Status:** Accepted
- **Date:** 2026-06-27, amended:
  - 2026-06-28: ordering added
  - 2026-07-31: laminar clipping added
  - 2026-08-01: emission simplified to unnested BEGIN/END pairs
  - 2026-08-02: sort moved into the sweep, and the once-per-trace guard made
    explicit
  - 2026-08-02: monitor-reported liveness landed and the counter carve-out was
    removed
  - 2026-08-05: pointer to
    [ADR-0015](0015-gc-loss-spans-on-their-own-track.md) added
  - 2026-08-17: the reporting site moved from `MonitorLoop` to
    `EventsMonitor`, see [ADR-0017](0017-monitor-owns-the-pid-lifecycle.md)
  - 2026-08-17: the RSS round stopped adding start jitter, see
    [ADR-0013](0013-rss-sampling.md)
  - 2026-08-20: "one clock read" narrowed to one *stamping* read, see
    [ADR-0019](0019-schedule-tick-starts-on-a-fixed-grid.md)
  - 2026-08-31: the span became one per process, and the liveness stamp moved
    to the end of the poll phase, see
    [ADR-0025](0025-create-every-process-in-one-place.md)
  - 2026-09-01: the process track was split per process
  - 2026-09-02: each process's own row gained a `Lifetime` slice drawing the
    observed pair
  - 2026-09-02: a retired process's row began going out at the next flush
  - 2026-09-02: the bar gained the interpreter count and the capture totals
  - 2026-09-02: `clipped` moved onto the span the sweep shortens
  - 2026-09-02: a rank became one draw off a counter
  - 2026-09-02: a row moved off the operating system's pid onto one gcmon
    counts

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
`[first observed, last observed]` for that process (see the liveness section
below for what counts as an observation). A span never covers a stretch in
which the process it names did not exist.

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
`process_ordering = EXPLICIT` and `thread_ordering = EXPLICIT` (fields 19 and
20) and nothing else: no name, no parent, no sub-message. The UI reads the two
hints only on the canary channel of `ui.perfetto.dev` (Flags -> Release
channel -> Canary), and a trace processor older than 0.57 ignores them and
orders tracks its own way. gcmon writes them whatever the reader, so a trace
stays forward-compatible.

**Process tracks are ranked by first observation**, ties broken by ascending
process, sequential from 0. Every process with a recorded span gets a rank,
including one known only from liveness.

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

**A process with a span and no descriptor gets one at close.** Finalization
walks every process the accumulator holds, so a process gcmon polled and read
no collections from draws its own row: a `ProcessDescriptor` stamped and
ranked from its first observation, its command line, and a `Lifetime` slice
with nothing under it.

**The Perfetto process track is split per process, each row stamped and ranked
from its own first observation.** The trace processor keys process identity on
`ProcessDescriptor.pid`, not on the track uuid, so descriptors sharing a pid
do not reliably draw a row each.

**A row is written under a pid gcmon counts from 1, not the operating
system's.** The thread descriptor carries that pid, and the `tid` beside it is
the interpreter id, interpreter 0 included. The trace processor reads a
thread's process off the descriptor's pid, so the `tid` says nothing but which
interpreter. `thread.is_main_thread` is the price: the trace processor sets it
from a `tid` equal to the pid, so the flag goes to whichever interpreter's id
equals its process's row pid, and in every other process to a nameless row of
the trace processor's own. No query of gcmon's reads it. The alternative was a
`tid` that means an interpreter in one row and a pid in the next.

**Every process draws a full set of rows of its own**: process track, thread
track per interpreter, `GC Loss` track, counter group and `Lifetime` slice,
named `Process <pid>` and `Process <pid>#N` to match its `Processes` span.
`start_timestamp_ns` stamps a row where its process started. Measured against
the trace processor the suite pins in `tests.perfetto_prebuilt`.

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
`start_timestamp_ns`, none of them free once `pid` holds the row's. Where
`ts`/`dur` and the annotations disagree, the annotations are the truth.

**The process's own row draws the observed pair; the shared row draws the
clipped one.** Clipping exists because every process shares the `Processes`
track and slices on a Perfetto track are a stack. A process's own row carries
one `Lifetime` slice
([ADR-0010](0010-process-identity-cmdline-and-start-marker.md)) and the
workload's `Instant` marks, which nest without closing anything, so nothing on
that row can cross the slice and nothing needs clipping. The two rows
therefore disagree for a clipped process, and the row able to draw the
observed pair draws it. `Lifetime` needs no `real_*` annotations: its own `ts`
and `dur` are those two numbers.

The shared slice says `clipped`, so a reader sees which processes diverge
instead of subtracting one row's duration from the other's. It goes on every
slice, true or false. The bar cannot carry it: a retired process's row is
drawn at the next flush and the sweep has decided nothing yet.

**How much of the process gcmon read is counted in the convert pass.** The bar
says `sampled_count` against `lost_count`, and the exporter's entry points are
where those arrive: `add_event` takes one record and `add_loss_event` one poll
interval. Neither holds the encoder lock, and the accumulators are read under
it, so counting there means a second acquisition per record on the hot path.
Counting in the convert pass costs nothing, since its caller already holds the
lock, and it also covers `gcmon combine`, which builds a trace from a capture
without an exporter.

The pass sees events rather than records, so it counts the two that stand for
one thing each: the `GC Pause` slice, which every record produces one of, and
a slice on a `LossTrack`, which is one interval. Every other event is a phase
or a counter of a record already counted.

`sampled_count` cannot be summed off the `GC Loss` slices. `EventsMonitor`
reports an interval only when it lost something, so the `observed_count`
riding there covers lossy intervals alone; a process that lost nothing has no
slice to sum and would read as one gcmon never sampled.

**A retired process's row goes out at the next flush; the shared slice waits
for close.** Once gcmon lets go of a pid the process's span is final: a record
read afterwards is filed under whatever holds the pid now
([ADR-0025](0025-create-every-process-in-one-place.md)), and liveness and RSS
both work off the tick's live set. The bar needs nothing but that one span, so
it is drawn as soon as the events queued ahead of it have reached the
accumulator. The `Processes` slice needs every other span in the run: the
sweep is global, and a process discovered later can still open one inside a
retired process's, because a poll returns collections that already happened. A
slice drawn early could not be clipped against a sibling that did not exist
yet, and two crossing slices on one track come back at widths neither was
given with nothing reported.

The Perfetto UI hides a row holding no events, so a bar that never reached the
file takes its whole row with it, thread rows and all. A process already
retired keeps its row; one still running does not, and neither does the
minimap.

The exception is the control plane, which files an instant by timestamp and
can still name a retired process (ADR-0025). One arriving after the row was
drawn lands on it outside the bar. Accepted: the alternative is holding every
row back for a message that may never come.

**No span is dropped.** A pid observed at a single instant, and a pid clipped
down to nothing, both still get a BEGIN/END pair; the trace processor accepts
it and reports `dur = 0`. A missing slice would leave no record that the
process was monitored at all.

**The span is `[min, max]` over every observation, with no event-kind
exception.** An observation is any non-meta trace event, counters included, or
a **liveness observation** from `EventsMonitor`: a process and an instant,
meaning gcmon read GC state out of that process then. One tick of monitoring
is one call on the monitor, which reports the whole `PollStatus.OK` set
through `add_process_liveness(processes, ts_ns)` once, after its poll phase,
so the cost is one call per tick rather than one per pid. The accumulator
folds an observation in as a plain min/max with no keyword: the counter
carve-out this ADR called provisional is **removed**, since the sampler
liveness it kept out of the end is now reported directly.

**A liveness report is stamped when the reads that proved it returned**, not
when the tick opened. A tick polls its pids in sequence, and a process polled
second is observed later than one polled first. The report carries the instant
the tick's last successful read returned. Every process alive in one tick then
shares an end, which the sweep nests rather than clips. `MonitorLoop` still
takes one stamping clock read per tick and hands it in; that instant opens the
loss window ([ADR-0015](0015-gc-loss-spans-on-their-own-track.md)) and stamps
a whole RSS round ([ADR-0013](0013-rss-sampling.md)), and only the liveness
report carries the later one.

**Liveness folds in alongside events rather than replacing them.**
`[first OK, last OK]` was rejected because `get_gc_stats` returns collections
that *already happened*, so a freshly discovered child's first GC event can
predate gcmon ever polling it. Under a replace rule every such child would
draw a GC slice outside its own lifetime slice. Membership in `children` is
**not** an observation: `get_child_pids` is the OS's claim about the process
tree, and taking it as evidence reintroduces the `create_time()` approach
rejected below.

**A successful read is what makes the span mean anything.** `get_gc_stats`
returns only once the runtime has finished initializing, so the first `OK`
dates the process becoming ready rather than the OS creating it. A read that
fails after earlier ones succeeded means the process has died or entered
finalization, so the last `OK` dates the other end. That is the interval the
slice draws, and it is why an unpolled or never-collecting process still has
one.

**The span means *liveness*, not *monitoring coverage*.** A pid the control
server suppresses mid-run is not polled and so not observed, but if re-enabled
it gets **one continuous span across the gap**, because the accumulator stores
only a min and a max. Correct under "liveness", wrong under "monitoring
coverage"; representing the gap as two spans is out of scope.

**Liveness is always on**, with no flag. The cost that justified `--rss`
([ADR-0013](0013-rss-sampling.md)) does not transfer: `live_pids` is already
built by the poll phase, and this is one batched call and two dict comparisons
per pid per tick. A flag would ship two definitions of a `Processes` slice.

**Liveness attaches at `PerfettoExporter`, not on the `EventEncoder`
protocol**, which is three methods meaning "translate a batch of `TraceEvent`
into bytes"; a liveness observation is neither. `PerfettoExporter` builds its
own `ProtobufEventEncoder`, so it keeps a typed handle and overrides the
liveness call; JSONL and stdout reach the `EventsExporter` no-op. The override
takes the I/O lock, which is not optional: it guards every other encoder
touch, closing included, and `ControlServer` writes from its own thread.
Without it a concurrent read-modify-write can drop a min/max update, and a new
pid arriving while the exporter closes can raise
`RuntimeError: dictionary changed size during iteration` out of the span
iteration.

## Consequences

- You can read each process's lifetime beside the others', and the same events
  in a different input order produce the same ranks.
- **`process.pid` is never the operating system's**, so a query joining it
  against a pid from `ps`, from a log or from a trace recorded elsewhere
  matches nothing rather than matching some of the processes that held that
  pid.
- **A clipped slice under-reports how long the process was observed**, by an
  amount that depends on how close together the starts are, not on how much
  the spans overlap: the clip is to `later.start - 1`. Siblings fanning out
  from a fork loop start microseconds apart, so each keeps microseconds of a
  lifetime that ran for seconds. Liveness cuts the other way for part of a
  fan-out: children whose earliest evidence is the tick that first polled them
  share that timestamp and nest rather than clip, while those whose first GC
  event predates the poll keep their jitter.
- **`--rss` adds no jitter of its own.** The sampler stamps a whole round with
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
- **A zero-GC process draws a full row**, since the monitor reads its command
  line when it creates the process rather than on the encoder's write
  ([ADR-0010](0010-process-identity-cmdline-and-start-marker.md)) and
  finalization gives it a descriptor off its span alone. Only the thread rows,
  the loss rows and the counters are missing, because it produced nothing to
  draw on them.
- **Deep nesting is now the normal shape.** Processes still alive when the
  loop stops share an end timestamp, and the sweep breaks out on
  `outer_end >= end`, so co-terminating spans nest one level per process
  instead of clipping. Staggered deaths still clip, so traces mix both.
- **The trace processor closes at most 512 nested slices.** The fuzz suite
  measures this against the real trace processor: at 512 every slice reads
  back intact, and each level past it leaves one more with `dur = -1`. The
  loss is **silent**, since `misplaced_end_event` stays 0 and no other
  non-info stat is raised. gcmon writes a well-formed pair for every span
  either way, so the limit sits in the reader. Bounding nesting depth is out
  of scope.
- **`combine` diverges from live capture.** Offline conversion has no monitor
  polling anything, so its spans stay event-derived and narrower. Carrying
  liveness through JSONL so `combine` could reproduce it is out of scope.
- **`sibling_order_rank` is not exposed as a SQL column.** It is a UI hint, so
  the trace-processor tests act as a *schema-validity guard*: they confirm the
  layout is accepted and the `process` and `track` tables survive intact, but
  only the Perfetto UI can assert display order. Perfetto's docs call these
  orderings "strong hints" in any case, so the UI may still rearrange tracks
  in special contexts.
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
- [ADR-0015](0015-gc-loss-spans-on-their-own-track.md) needs no sweep: its
  loss spans are one per poll interval and meet without overlapping. Its
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
- **Dropping a slice that ends up zero-length**, the original decision here.
  Reversed: it optimised the rendering at the cost of the record, and the pids
  likeliest to be clipped to nothing are the short-lived children a reader is
  looking for.
- **Snapping near-equal starts together before the sweep**, turning a jittered
  fan-out back into the nesting it almost is; every end survives at a cost of
  at most ε on each start, and a clipped fan-out keeps its whole observed
  duration instead of microseconds of it. Not adopted, still open: ε is a
  heuristic, nesting N deep costs N rows of vertical space inside the track,
  and the trace processor stops closing slices past 512 (see Consequences).
- **Extending the earlier span's end instead of clipping it**, nesting the
  later span inside. Rejected: it makes a dead process look alive, and the
  nesting implies a parent/child relationship that may not exist.
- **Clipping whichever side loses fewer nanoseconds.** Rejected: it makes the
  direction of the distortion depend on the data rather than on a stated rule.
- **Leaving crossing spans alone and documenting the mismatch.** Rejected: the
  durations are wrong, and `misplaced_end_event` is not something a reader of
  the UI would think to check.
- **OS-level process times via `psutil.Process(pid).create_time()`.**
  Rejected: the span should describe what gcmon observed, not when the OS
  started the process; the difference would be misread as monitoring coverage.
- **Snapping every pid in a tick to one timestamp**, making spans that share a
  start nest rather than cross, so the "snap near-equal starts" alternative
  above lands with no ε to choose. Noted and not taken: the benefit is an
  artifact of `--rate` and would degrade silently as the rate drops.
- **Deriving the epoch from gaps in the liveness reports**, which reach this
  track already and would have split the spans with nothing new plumbed
  through. Rejected in [ADR-0025](0025-create-every-process-in-one-place.md):
  a pid the control server suppresses produces the same gap as a pid that
  died.
- **Sharing one process track across every process that held a pid**, on the
  grounds that two descriptors on one pid might collapse to a single `upid`.
  The original decision here, and **reversed**: the shared row interleaved two
  processes' thread events, stepped its counters between them with nothing
  marking where, and carried a start stamp that predated the successor.
- **Resolving the epoch inside the encoder**, asking a `ProcessLookup` which
  process held the pid at a record's timestamp. Rejected: the monitor already
  decided that when it created the process
  ([ADR-0025](0025-create-every-process-in-one-place.md)), so a second answer
  computed downstream can only disagree with the first, and a boundary
  timestamp is where it would.
- **Keeping one process track and annotating each counter with the epoch.**
  Rejected: an annotation on a counter is not a row, so the UI still draws one
  line stepping between two processes. It also leaves the start stamp, the
  lifetime slice and the command line wrong.
- **Writing the operating system's pid on every descriptor**, leaving
  `process.pid` a trace-wide identifier a reader joins on and correlates
  against other tools. The original decision here, and **reversed**: the first
  measurement found that two descriptors on one pid split, and generalised
  from it. A third does not, so the scheme costs a row per process past the
  second. The join it buys is replaced by the `pid` annotation on both of a
  process's spans and by the row's name.
- **Leaving the operating system's pid on the first process to hold it**, and
  counting only its successors, keeping `process.pid` joinable for the rows a
  pid was never reused for. Rejected: a column true for most rows and false
  for the rest is a worse rule than one never true, because a query on it
  comes back with a subset that looks like an answer.
- **Offsetting a process by its epoch**, `pid + (pid_epoch - 1) * space`, so
  `process.pid` decodes to the operating system's pid and the epoch with no
  annotation. Rejected: `ProcessDescriptor.pid` is an `int32`, so whatever
  *space* reserves for the pid comes out of the epoch. At Linux's `1 << 22`
  ceiling it allows 512 processes per pid and still misses a Windows pid,
  which reaches `1 << 26`; sized for Windows it allows 32, which an ordinary
  run reaches. The case it misses needs a fold and a warning, machinery for a
  convenience the annotation already provides.
- **Fixing the command line alone**, the one field that was wrong rather than
  merged: the `#2` span and the track above it named different programs.
  Rejected: there is no correct value to write into a field two processes
  share, and the thread row, the counters, the start stamp and the lifetime
  slice stay merged behind it.
- **Emitting liveness as a `TraceEvent`.** Rejected: at 10 Hz × N pids, a
  60-second run with ten children carries ~6,000 extra events, visible on the
  process tracks, to record two numbers per pid.
- **A `RssSampler`-style collaborator** accumulating `first`/`last` per pid
  and flushing at close. Rejected: it mirrors state the exporter already holds
  and adds a close-ordering hazard. Against a min/max, the redundant per-tick
  calls cost only dict comparisons.
- **A fourth method on the `EventEncoder` protocol**, with a no-op in
  `JsonEventEncoder`. Rejected: it widens a precise abstraction to carry
  per-trace state one implementation has, and taxes the other for the life of
  the protocol.
- **Emitting the slice END at the end of each convert call.** The original
  implementation, and wrong; see above.
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
  the whole subtree can move. A process descriptor arriving after its own
  thread descriptor loses the per-process split, since the pid is already
  bound to a row, and a counter event on a track described later is dropped
  outright.

## Implementation

- `src/gcmon/exporters/perfetto_process_lifetime.py` holds this decision: the
  `Processes` track name, the clipping sweep, the emission of both rows and
  the finalization the encoder calls at close. The root and the process
  descriptors live there because finalization writes them, a process known
  only from liveness being described there or nowhere.
- `src/gcmon/exporters/perfetto_proto.py` carries `process_ordering` at field
  19 and `thread_ordering` at field 20. Fields 6 and 7 on the same message are
  `chrome_process` and `chrome_thread`, so a wrong number writes a different
  message and fails silently
  ([ADR-0001](0001-hand-rolled-perfetto-protobuf-encoder.md)).
- `src/gcmon/exporters/perfetto_track_state.py` holds the span accumulator,
  the ranks and the row pids. Every key it holds is filed under the process,
  which is what splits the rows a reused pid draws.
- The liveness path runs from `src/gcmon/monitoring/monitor_loop.py`, which
  stamps the tick ([ADR-0019](0019-schedule-tick-starts-on-a-fixed-grid.md)),
  through `src/gcmon/monitoring/monitor.py`, which reports the live set
  ([ADR-0017](0017-monitor-owns-the-pid-lifecycle.md)), to the no-op on
  `src/gcmon/exporters/exporter.py` that
  `src/gcmon/exporters/perfetto_exporter.py` overrides under the I/O lock,
  forwarding to `src/gcmon/exporters/encoder.py`.
- The sweep and finalization are covered in
  `tests/exporters/test_perfetto_process_lifetime.py`, the accumulator and the
  row pids in `tests/exporters/test_perfetto_track_state.py`, the ranks and
  `start_timestamp_ns` in `tests/exporters/test_perfetto_ordering.py`.
  `tests/exporters/test_perfetto_emission_order_fuzz.py`, marked `fuzz` and
  run by its own CI job, settles the emission-order claims and both sides of
  the 512-deep nesting limit; the orderings rejected above are asserted to
  break, so the positive case cannot pass by the order being irrelevant.
  `tests/exporters/test_perfetto_exporter_integration.py` reads the trace back
  through the real trace processor
  ([ADR-0014](0014-perfetto-integration-test-strategy.md)), and
  `tests/monitoring/test_monitored_run_trace.py` pins a whole run's trace.
  Liveness reaches those from `tests/monitoring/test_monitor.py` and
  `tests/monitoring/test_monitor_loop.py`.
