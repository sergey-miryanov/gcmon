# ADR-0019: Schedule tick starts on a fixed grid, and skip the positions a slow tick misses

- **Status:** Accepted
- **Date:** 2026-08-20

## Context

`--rate` names the interval an operator wants between polls. The loop honoured
it by waiting that long *after* each tick, so the interval was the rate plus
whatever the tick had cost. The target sets that cost: the number of live
pids, the rings read for each, the RSS round. A wide process tree therefore
polled slower than the number asked for, and the error accumulated across a
run rather than cancelling. Two captures of the same workload were not
comparable either, because a tree that fanned out mid-capture changed its own
sampling interval partway through.

Two constraints shaped the fix.

**The wait must stay interruptible.** Shutdown reaches the loop through an
event a signal handler sets, and the loop waits on that event rather than
sleeping. Swapping it for a more precise sleep would buy per-tick accuracy at
the cost of shutdown latency.

**The tick's instant cannot pace the loop.** The loop reads the clock before
the tick, and the monitor and the RSS sampler both run on that one instant
([ADR-0011](0011-process-lifetime-and-ordering.md),
[ADR-0013](0013-rss-sampling.md)). That instant is fixed before the tick's
cost is known, and pacing off it is the arithmetic that produced the defect.

## Decision

**Tick starts land on a fixed grid.** The loop reads `t0` once before the
first tick, and positions are `t0 + k * rate` for whole `k`. A tick waits
until the next position. Each position comes off `t0` alone, and no tick
carries an error into the next.

**A second clock read paces the loop.** The loop takes it when the tick's work
is done and measures the idle to the next position from it. It stamps nothing.

**A tick that outlasts its position skips to the next one on the grid.** The
loop drops the missed positions and never makes them up. The phase survives:
the effective interval degrades in whole multiples of the rate, and two
captures stay comparable even when one of them fell behind.

**A rate is a plain decimal number of seconds, `MIN_RATE_NS` or more.** gcmon
refuses scientific notation because it hides how small a value is: `1e-12`
reads as a rate. The minimum is the wait floor: below it the guard is longer
than the interval asked for, so no tick can start on time.

**The wait is floored at `MIN_IDLE_NS`, one millisecond.** Without a floor, a
tick finishing just before its next position sends the loop straight back in.
It pins gcmon at a full duty cycle against a struggling target.

**The floor bounds a wait, never a position.** A wait it stretches past the
next position makes that tick start late; the grid stays where `t0` put it,
and the tick after lands back on it.

**A run answers a report.** The loop returns two counts, ticks run and ticks
scheduled. The report carries the loop's state out to whoever prints it, and
the loop decides nothing with it.

**A run overruns when `OVERRUN_SHARE` of its ticks go missing.** One lost
position tells an operator nothing: the loop loses positions for reasons the
rate does not control, and only a systematic loss means the rate is
unreachable.

**The summary decides what to tell the operator, once the run is over.**
`stats_output` reads the report and picks the remedy from it. The low-coverage
advisory fires mid-run, before any report exists, so it states the loss and
prescribes nothing. A later decision that needs what the run did joins the
summary rather than the loop.

## Consequences

- The interval an operator asks for is the interval they get, at any tick
  cost, so `--rate` is a property of the capture rather than of the target's
  size.
- Per-tick jitter remains: the wait primitive still rounds up to the scheduler
  tick. An absolute grid stops that error accumulating without removing it.
  The next wait absorbs a late wake-up, and the ticks after it stay on the
  grid.
- Loss-window widths become predictable. A window's edges are per-pid read
  instants, which the schedule leaves alone. Its width is the gap between
  consecutive reads of the same pid, and the schedule now sets that. Comparing
  loss windows across a run, or across captures, means something it did not
  before.
- RSS sampling inherits an evenly spaced schedule: the sampler paces off the
  stamping instant, and the grid changed nothing in its interval logic.
- `--rate` has a lower bound where it had none. gcmon accepted anything under
  a millisecond before and could never hold it, so the run that used to start
  now does not.

## Alternatives considered

- **Re-basing the schedule to `now + rate` after each tick.** Rejected: one
  overrun shifts the phase for the rest of the run, and a run that overruns on
  most ticks is back to treating the rate as a gap. It fixes the accumulating
  error without fixing the coupling to tick cost.
- **Running the missed ticks back-to-back to preserve the count.** Rejected:
  it schedules the heaviest polling at the moment the target is slowest, and
  the extra polls read a ring that has not refilled.
- **A floor proportional to the rate.** Rejected: it lowers the protection as
  the operator lowers the rate, and a short rate is when a tick most often
  outlasts its position. An absolute constant is correct across the range the
  rate spans, at the cost of bounding that range at the bottom.
- **Capping the floor at the rate**, so the guard never exceeds the interval
  asked for. Rejected: it does not remove the conflict where the two are
  equal, and it weakens the spin-guard where ticks are shortest and the loop
  is most able to spin. Bounding the rate and saying so is more honest than a
  guard that stops guarding without telling anyone.
- **Changing the wait primitive** to beat the scheduler quantum, with a
  chunked sleep or by raising the platform timer resolution. Rejected: it
  costs shutdown latency, and the grid already stops the error accumulating.
  That accumulation made captures incomparable.
- **Extracting the arithmetic into a schedule object.** Rejected: the
  arithmetic is a pure function of the run's first instant, one later instant
  and the rate, and `schedule.py` keeps it that way. An object holding a
  position as state earns its keep only if something other than the loop needs
  to ask, and nothing does.
- **Lending the monitor an overrun predicate**, the way the loop already lends
  it a stop predicate, so the advisory could pick its own wording. Rejected:
  the answer is meaningless in the first few ticks, when the advisory is most
  likely to fire, so it would need a warm-up threshold with nothing to justify
  it.

## Implementation

- Two leaf modules carry what has to reach the option parser without dragging
  the loop behind it. `_env` and the parser both import `stats.views`, so
  importing `monitor_loop` there would pull the monitor in behind every
  environment read. `src/gcmon/model/schedule.py` holds `MIN_IDLE_NS`,
  `MIN_RATE_NS` and the grid arithmetic; `src/gcmon/model/run_report.py` holds
  the run report and `OVERRUN_SHARE`, which crosses from the loop to the
  summary. The per-poll report sits in `src/gcmon/monitoring/monitor.py`,
  beside what produces it, and needs neither.
- `src/gcmon/monitoring/monitor_loop.py` holds the two clock reads.
- `src/gcmon/stats/stats_output.py` states the tick counts and selects the
  remedy; `src/gcmon/monitoring/monitor.py` carries the advisory that no
  longer prescribes one.
- `src/gcmon/cli/monitor/_env.py` parses one rate spelling for both `--rate` and
  `GCMON_RATE`; `src/gcmon/cli/monitor/monitoring_options.py` reports what it
  rejects and applies the same minimum to a rate arriving from anywhere else.
- Tests: `tests/test_schedule.py` for the grid, the skip and the floor,
  asserted on the arithmetic directly; `tests/monitoring/test_monitor_loop.py`
  for the rest, driven by a scripted clock and a stop event that records what
  it was asked to wait for, never by elapsed wall time, which would assert the
  operating system rather than gcmon; `tests/stats/test_stats_output.py` for
  the summary line and the two remedies; `tests/test_monitor_coverage.py` for
  the advisory keeping to what it knows.
