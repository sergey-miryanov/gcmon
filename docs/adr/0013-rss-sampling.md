# ADR-0013: Sample RSS in a standalone `RssSampler`, on the process track rather than a thread's

- **Status:** Accepted
- **Date:** 2026-07-13
- **Amended by:** [ADR-0024](0024-an-event-names-the-track-it-is-drawn-on.md),
  [ADR-0025](0025-create-every-process-in-one-place.md)
- **Modules:** cli, exporters, monitoring

## Context

gcmon tracked GC-level object counts, where `heap_size` counts live objects
rather than bytes, but nothing about the process's memory footprint.
Correlating GC activity with real memory pressure (is GC driving RSS growth,
or is RSS growth driving GC?) needs the OS-reported resident set size
alongside the GC events.

These constraints shaped the design.

**Cost.** `psutil.Process(pid).memory_info().rss` is cheap on Linux (a `/proc`
read) but carries syscall overhead on Windows and macOS. A tick runs every 0.1
s by default, and multiplying that by each child pid is a meaningful tax for a
metric that moves slowly.

**RSS has no thread.** The other counters went out per `(pid, tid)`, where
`tid` was the interpreter id. RSS is a process-level number with no thread
affinity, so no honest `tid` existed to give it.

**`MonitorLoop` should not learn about `psutil`.** The loop polls and paces.
Threading sampling logic, timers and exception handling through it would
spread a soft-optional dependency across the core.

## Decision

**Sampling lives in its own class**, `RssSampler` in `monitoring`. It holds
the exporter, the interval, and the last-sample time. Its only public method
is `tick(now_ns, live)`, and the timer check is internal. `MonitorLoop` gains
one optional constructor argument and one line in the loop body. It knows
nothing about `psutil`, timers, or how a sample turns into an event.

`tick` takes the caller's instant in **nanoseconds**, which both paces the
sampling and stamps every sample in a pass. The loop takes one **stamping**
`time.monotonic_ns()` per tick and passes it unconverted, here and to the
monitor ([ADR-0029](0029-report-liveness-and-fold-it-into-the-span.md),
[ADR-0017](0017-monitor-owns-the-pid-lifecycle.md)), so nanoseconds reach the
encoder without a detour through seconds
([ADR-0009](0009-nanoseconds-canonical-time-unit.md)). The loop reads the
clock a second time to pace itself
([ADR-0019](0019-schedule-tick-starts-on-a-fixed-grid.md)); that read stamps
nothing and reaches neither the monitor nor the sampler, so one instant still
covers everything a tick emits. `--rss-interval` stays seconds, because an
operator types it; the sampler converts it once at construction.

**The sampler reads no clock.** A sample stamped with its own
`time.monotonic_ns()` spreads a pass across however long `psutil` takes. That
spread carries no information: the pass walks a `set`, so hash order picks
which pid gets the earliest timestamp, and on the Perfetto side which
sibling's lifetime span is clipped. Spans sharing a start nest, so one instant
per pass removes the effect.

**The sampler callback is injectable**, the same pattern as the command-line
provider `ProcessRegistry` takes
([ADR-0025](0025-create-every-process-in-one-place.md)). Tests pass a mock and
never touch `psutil`. The constructor checks availability **once**: if the
import fails, it disables the sampler, logs at info level, and `tick()`
becomes a no-op. No per-sample import guard.

**Only pids that returned `PollStatus.OK` from the most recent GC poll are
sampled.** The live set is cleared each tick, so a pid must pass a fresh poll
to be sampled. No stale pids, and a process that dies between the poll and the
RSS read yields nothing.

**RSS belongs to the process and conjures no thread.** A sample names a
`ProcessTrack(process)`
([ADR-0024](0024-an-event-names-the-track-it-is-drawn-on.md)), so the row is
the process rather than a number reserved to stand for one, and there is no
thread descriptor to suppress. The track parents to the process row, outside
the `GC Metrics` group, with the display name `rss`, all by construction.

**Opt-in, with a decoupled interval.** `--rss` / `GCMON_RSS` (truthy: `1`,
`true`, `yes`, `on`) enables it; `--rss-interval` / `GCMON_RSS_INTERVAL`
defaults to 1.0 s, independent of the 0.1 s `--rate`.

## Consequences

- Sampling once a second by default costs an order of magnitude less than
  sampling every tick, and RSS does not move fast enough for the resolution to
  matter.
- **A sample is backdated to the start of its tick**, the price of one instant
  per pass. The instant is read before the poll phase and `psutil` runs after
  it, so a value lands up to a whole poll phase before it was read, and
  earlier than every GC record from the same tick. The skew is bounded by how
  long the polls take, which on a wide tree exceeds the 0.1 s rate. Accepted:
  RSS moves slowly enough that tens of milliseconds change nothing a reader
  concludes, while the per-sample read it replaced distorted the `Processes`
  track by hash order ([ADR-0011](0011-process-lifetime-and-ordering.md)).
- You can unit-test `RssSampler` without `psutil` and without a monitor loop.
- Missing `psutil`, a dead process, or a permission error each produce no
  sample and no error. `--rss` on a machine without `psutil` is ignored, with
  one info log.
- **Perfetto-only.** An RSS sample is a no-op on `EventsExporter` and
  `PerfettoExporter` overrides it, so JSONL and stdout carry no RSS. Chrome
  traces contained the counter event, a side effect nobody validated of the
  buffering base the two trace exporters shared; that format and that base are
  both gone ([ADR-0021](0021-write-one-trace-format.md),
  [ADR-0008](0008-buffered-exporter-and-encoder-protocol.md)).
  `RSS_CAPABLE_FORMATS` in the CLI layer names the one format that carries it.
- **`rss` loses its `sibling_order_rank`.** A `ProcessTrack` owns the counter,
  so it parents to the OS-scoped process track and the trace processor drops
  the rank, the trade-off [ADR-0003](0003-gc-metrics-group-track.md)
  established. Its position in the UI is a heuristic.
- The metric is `"rss"` and so is the display name the exporter writes beside
  it ([ADR-0024](0024-an-event-names-the-track-it-is-drawn-on.md)), so the row
  reads `rss` and no key surfaces in the UI.

## Alternatives considered

- **`tid = 0` for the process-level counter.** Rejected: `0` is a legitimate
  interpreter id, so it can collide with a real thread, and meta building
  would manufacture a `ThreadMeta(pid, 0, "Thread 0")` that describes nothing.
  A negative sentinel could not collide, and a `ProcessTrack` says the same
  thing without a number at all (ADR-0024).
- **Sampling inside `MonitorLoop` on every tick.** Rejected on cost and
  coupling: ten times the syscalls for a slow-moving metric, and `psutil`
  knowledge pushed into the core loop.
- **Making RSS always-on.** Rejected: it requires `psutil` and adds syscalls
  to each run, for a metric most people do not need.
- **A guard-and-import at each sample.** Rejected: the availability answer
  cannot change during a run, so checking once at construction is cheaper and
  clearer.
