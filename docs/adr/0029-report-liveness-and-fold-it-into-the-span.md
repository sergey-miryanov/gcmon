# ADR-0029: Report liveness once a tick, and fold it into a process's span

- **Status:** Accepted
- **Date:** 2026-08-02
- **Amended by:** [ADR-0013](0013-rss-sampling.md),
  [ADR-0017](0017-monitor-owns-the-pid-lifecycle.md),
  [ADR-0019](0019-schedule-tick-starts-on-a-fixed-grid.md),
  [ADR-0025](0025-create-every-process-in-one-place.md)
- **Modules:** exporters, monitoring

## Context

[ADR-0011](0011-process-lifetime-and-ordering.md) draws one span per process
on the shared `Processes` track, and
[ADR-0028](0028-draw-every-process-a-row-of-its-own.md) a `Lifetime` slice on
the process's own row. Both need an interval.

A span built from trace events alone is as wide as what the process collected.
A process gcmon polls and reads no collections from has no event to take a
span from, and one that goes quiet reads as gone. The monitor knows more than
the events say: every tick it reads GC state out of each process that answers.

## Decision

**The span is `[min, max]` over every observation, with no event-kind
exception.** An observation is any non-meta trace event, counters included, or
a **liveness observation** from `EventsMonitor`: a process and an instant,
meaning gcmon read GC state out of that process then. One tick of monitoring
is one call on the monitor
([ADR-0017](0017-monitor-owns-the-pid-lifecycle.md)), which reports the whole
`PollStatus.OK` set through `add_process_liveness(processes, ts_ns)` once,
after its poll phase, so the cost is one call per tick rather than one per
pid. The accumulator folds every observation in as a plain min/max.

**A liveness report is stamped when the reads that proved it returned**, not
when the tick opened. A tick polls its pids in sequence, and a process polled
second is observed later than one polled first. The report carries the instant
the tick's last successful read returned. Every process alive in one tick then
shares an end, which the sweep nests rather than clips (ADR-0011).
`MonitorLoop` still takes one stamping clock read per tick and hands it in
([ADR-0019](0019-schedule-tick-starts-on-a-fixed-grid.md)); that instant opens
the loss window ([ADR-0015](0015-gc-loss-spans-on-their-own-track.md)) and
stamps a whole RSS pass ([ADR-0013](0013-rss-sampling.md)), and only the
liveness report carries the later one.

**Liveness folds in alongside events rather than replacing them.**
`get_gc_stats` returns collections that *already happened*, so a freshly
discovered child's first GC event can predate gcmon ever polling it, and the
span reaches back to that event. Membership in `children` is **not** an
observation: `get_child_pids` is the OS's claim about the process tree, and
taking it as evidence reintroduces the `create_time()` approach rejected
below.

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
coverage"; representing the gap as two spans is out of scope. A gap in the
reports is therefore not a departure
([ADR-0025](0025-create-every-process-in-one-place.md)).

**Liveness is always on**, with no flag. The cost that justified `--rss`
(ADR-0013) does not transfer: the live set is already built by the poll phase,
and this is one batched call and two dict comparisons per pid per tick. A flag
would ship two definitions of a `Processes` slice.

**Liveness attaches at `PerfettoExporter`, beside the encoder's three methods
rather than through them.** They mean "translate a batch of `TraceEvent` into
bytes" ([ADR-0008](0008-buffered-exporter-and-encoder-protocol.md)), and a
liveness observation is neither. `PerfettoExporter` builds its own
`ProtobufEventEncoder` and overrides the liveness call; JSONL and stdout reach
the `EventsExporter` no-op. The override takes the I/O lock, which is not
optional: it guards every other encoder touch, closing included, and
`ControlServer` writes from its own thread. Without it a concurrent
read-modify-write can drop a min/max update, and a new pid arriving while the
exporter closes can raise
`RuntimeError: dictionary changed size during iteration` out of the span
iteration.

## Consequences

- **A process that answered a single poll and never collected has a span**, so
  it gets a `Processes` slice and a rank (ADR-0011) and a row of its own
  (ADR-0028).
- **`combine` diverges from live capture.** Offline conversion has no monitor
  polling anything, so its spans stay event-derived and narrower. Carrying
  liveness through JSONL so `combine` could reproduce it is out of scope.

## Alternatives considered

- **A span of `[first OK, last OK]`, liveness replacing the events.**
  Rejected: a child's first GC event can predate its first poll, so every such
  child would draw a GC slice outside its own lifetime slice.
- **OS-level process times via `psutil.Process(pid).create_time()`.**
  Rejected: the span should describe what gcmon observed, not when the OS
  started the process; the difference would be misread as monitoring coverage.
- **Emitting liveness as a `TraceEvent`.** Rejected: at the default 0.1 s
  rate, a 60-second run with ten children carries ~6,000 extra events, visible
  on the process tracks, to record two numbers per pid.
- **A `RssSampler`-style collaborator** accumulating `first`/`last` per pid
  and flushing at close. Rejected: it mirrors state the exporter already holds
  and adds a close-ordering hazard. Against a min/max, the redundant per-tick
  calls cost only dict comparisons.
