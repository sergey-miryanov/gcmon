# ADR-0013: Sample RSS in a standalone `RssSampler`, on a `tid = -1` sentinel track

- **Status:** Accepted
- **Date:** 2026-07-13, amended:
  - 2026-08-02: caller note added
  - 2026-08-17: `tick` took the instant in nanoseconds, which removed the
    per-sample clock read and narrowed "one clock read per tick" to one
    *stamping* read
  - 2026-08-31: `tick` took processes rather than pids, see
    [ADR-0025](0025-create-every-process-in-one-place.md)

## Context

gcmon tracked GC-level object counts, where `heap_size` counts live objects
rather than bytes, but nothing about the process's memory footprint.
Correlating GC activity with real memory pressure (is GC driving RSS growth,
or is RSS growth driving GC?) needs the OS-reported resident set size
alongside the GC events.

Three constraints shaped the design.

**Cost.** `psutil.Process(pid).memory_info().rss` is cheap on Linux (a `/proc`
read) but carries syscall overhead on Windows and macOS. The GC poll runs at
10 Hz by default, and multiplying that by each child pid is a meaningful tax
for a metric that moves slowly.

**RSS has no thread.** The other counters are emitted per `(pid, tid)`, where
`tid` is the interpreter id. RSS is a process-level number with no thread
affinity, so no honest `tid` exists to give it.

**`MonitorLoop` should not learn about `psutil`.** The loop polls and paces.
Threading sampling logic, timers and exception handling through it would
spread a soft-optional dependency across the core.

## Decision

**Sampling lives in its own class**, `RssSampler` in
`src/gcmon/monitoring/rss_sampler.py`. It holds the exporter, the interval,
and the last-sample time. Its only public method is `tick(now_ns, live)`, and
the timer check is internal. `MonitorLoop` gains one optional constructor
argument and one line in the loop body. It knows nothing about `psutil`,
timers, or how a sample turns into an event.

`tick` takes the caller's instant in **nanoseconds**, which both paces the
round and stamps every sample in it. The loop takes one **stamping**
`time.monotonic_ns()` per tick and passes it unconverted, here and to the
monitor ([ADR-0011](0011-process-lifetime-and-ordering.md),
[ADR-0017](0017-monitor-owns-the-pid-lifecycle.md)), so nanoseconds reach the
encoder without a detour through seconds
([ADR-0009](0009-nanoseconds-canonical-time-unit.md)). The loop reads the
clock a second time to pace itself
([ADR-0019](0019-schedule-tick-starts-on-a-fixed-grid.md)); that read stamps
nothing and reaches neither the monitor nor the sampler, so one instant still
covers everything a tick emits. `--rss-interval` stays seconds, because an
operator types it; the sampler converts it once at construction.

**The sampler reads no clock.** It used to stamp each sample with its own
`time.monotonic_ns()`, spreading a round across however long `psutil` took.
That spread carried no information: the round walks a `set`, so hash order
picked which pid got the earliest timestamp, and on the Perfetto side which
sibling's lifetime span got clipped. Spans sharing a start nest, so one
instant per round removes the effect.

**The sampler callback is injectable**, the same pattern as the cmdline
provider in `ProtobufEventEncoder`. Tests pass a mock and never touch
`psutil`. The constructor checks availability **once**: if the import fails,
it disables the sampler, logs at info level, and `tick()` becomes a no-op. No
per-sample import guard.

**Only pids that returned `PollStatus.OK` from the most recent GC poll are
sampled.** The live set is cleared each iteration, so a pid must pass a fresh
poll to be sampled. No stale pids, and a process that dies between the poll
and the RSS read yields nothing.

**Amended 2026-08-26 by
[ADR-0024](0024-an-event-names-the-track-it-is-drawn-on.md).** There was a
sentinel `tid = -1` here, and a counter track keyed `(pid, -1, "rss", "rss")`.
The decision underneath is confirmed rather than overturned: RSS belongs to
the process and must not conjure a thread. An RSS sample names a
`ProcessTrack(process)` now, so that is what the row is rather than a number
reserved to stand for it, and there is no thread descriptor to suppress. The
track is still parented directly to the process track, outside the
`GC Metrics` group, with the display name `rss` -- by construction now rather
than by membership of a metric set.

**Opt-in, with a decoupled interval.** `--rss` / `GCMON_RSS` (truthy: `1`,
`true`, `yes`, `on`) enables it; `--rss-interval` / `GCMON_RSS_INTERVAL`
defaults to 1.0 s, independent of the 0.1 s GC poll rate.

## Consequences

- The default 1 Hz sampling costs an order of magnitude less than sampling at
  the GC poll rate, and RSS does not move fast enough for the resolution to
  matter.
- **A sample is backdated to the start of its tick**, the price of one instant
  per round. The instant is read before the poll phase and `psutil` runs after
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
- **Perfetto-only.** An RSS sample is a no-op on the `EventsExporter` base and
  `BufferedTraceExporter` overrides it, so JSONL and stdout carry no RSS.
  Chrome traces contained the counter event, a side effect of the shared base
  that nobody validated. The format is gone
  ([ADR-0021](0021-write-one-trace-format.md)). `RSS_CAPABLE_FORMATS` in the
  CLI layer names the one format that carries it.
- Adding `"rss"` to the top-level set brings the accepted trade-off from
  ADR-0004 with it: its `sibling_order_rank` is dropped because its parent is
  OS-scoped.
- The counter payload key is `"rss"` (`{"rss": rss_bytes}`). The event carries
  a single argument, so the display name normalizes to the metric name and the
  key never surfaces in the UI.

## Alternatives considered

- **`tid = 0` for the process-level counter.** Rejected: `0` is a legitimate
  interpreter id, so it can collide with a real thread, and meta building
  would manufacture a `ThreadMeta(pid, 0, "Thread 0")` that describes nothing.
  A negative sentinel cannot collide, and one comparison guards it.
- **Sampling inside `MonitorLoop` at the GC poll rate.** Rejected on cost and
  coupling: ten times the syscalls for a slow-moving metric, and `psutil`
  knowledge pushed into the core loop.
- **Making RSS always-on.** Rejected: it requires `psutil` and adds syscalls
  to each run, for a metric most people do not need.
- **A guard-and-import at each sample.** Rejected: the availability answer
  cannot change during a run, so checking once at construction is cheaper and
  clearer.

## Implementation

- `src/gcmon/monitoring/rss_sampler.py` holds `RssSampler`, its
  `tick(now_ns, live)` entry point, the interval check, and the default
  sampler catching `NoSuchProcess` / `AccessDenied`.
- `src/gcmon/exporters/_buffered_exporter.py` holds the `-1` sentinel, the
  `iid >= 0` guard that suppresses thread meta for it, and the exporter's RSS
  sample.
- `src/gcmon/exporters/perfetto_format.py` carries `"rss"` in the top-level
  metric set.
- `src/gcmon/monitoring/monitor_loop.py` takes one stamping read per tick and
  hands the instant to the monitor and then to the sampler; the monitor
  collects the live pids and reports liveness
  ([ADR-0017](0017-monitor-owns-the-pid-lifecycle.md)).
  `src/gcmon/cli/monitor/loop_runner.py` constructs the sampler.
- `src/gcmon/cli/monitor/_env.py` reads `GCMON_RSS` and `GCMON_RSS_INTERVAL`.
- Tests: `tests/test_rss_sampler.py` (interval timing, live-pid filtering,
  injected sampler, psutil-unavailable fallback);
  `tests/exporters/test_buffered_exporter.py` (`iid = -1` emits no
  `ThreadMeta`); `tests/benchmarks/test_rss_sampler_bench.py` (read latency).
