# 0033: Show how much was lost, not only where

- **Status:** Not started (unblocked; the loss-window redesign landed as
  ADR-0015)
- **Kind:** feature (enhancement)
- **Effort:** S
- **Origin:** grilling session, 2026-08-08, recommendation 3
- **Respects:**
  - [ADR-0003](../docs/adr/0003-gc-metrics-group-track.md): `GC Metrics`
    group.
  - [ADR-0024](../docs/adr/0024-an-event-names-the-track-it-is-drawn-on.md):
    which counters are drawn outside the group.
  - [ADR-0005](../docs/adr/0005-counter-y-axis-share-key.md):
    `y_axis_share_key`.
  - [ADR-0007](../docs/adr/0007-shared-trace-converter-pipeline.md): one
    conversion pipeline.
  - [ADR-0015](../docs/adr/0015-gc-loss-spans-on-their-own-track.md): the
    `GC Loss` track.

## 1. Problem statement

Since the per-generation redesign, the `GC Loss` row says where gcmon was
blind, for how long, and which generations were blind together. It says
nothing about how much was lost until you click a span. Every span carries the
same name and so the same colour, and a bar that lost 1 record looks like one
that lost 40.

`Cov` and `F` in the `--stats` table answer that for the run as a whole.
Neither shows the shape over time. An operator watching a workload change
phase cannot see loss get worse, find the worst stretch of a capture, or tell
a run that lost a little everywhere from one that lost everything in a
two-second burst. Those last two want different fixes and read the same today.

## 2. Solution

A counter lane under the process's `GC Metrics` group, one per generation,
plotting collections lost so far. It rises in steps as gcmon finds gaps and is
flat where nothing was lost, so its slope is the loss rate and its final value
is the run total. Reading a capture becomes: glance at the counter for how bad
and where, then click the span for exactly which records.

## 3. User stories

1. As someone reading a trace in the Perfetto UI, I want a line whose slope
   shows loss getting worse, so that I can find the worst stretch of a long
   capture without clicking bars.
2. As an operator attaching to a production process, I want to see whether
   loss is spread evenly or concentrated in a burst, so that I know whether
   lowering `--rate` will help or whether the workload has a phase that
   outruns any rate.
3. As a developer profiling under `gcmon run`, I want the counter to sit
   beside the existing per-generation counters, so that I can read loss
   against `collected` and `candidates` on one shared time axis.
4. As someone comparing two captures, I want the counter's final value to
   equal the run's total lost count, so that the trace and the `--stats` table
   agree without arithmetic.
5. As an operator on a healthy target that lost nothing, I want no new rows at
   all, so that a clean capture does not grow three empty lanes.
6. As a gcmon maintainer, I want the counter derived from the same loss
   records the spans come from, so that ADR-0007's single converter still
   holds and `combine` reproduces the counter from JSONL.

## 4. Implementation decisions

**Cumulative, not instantaneous.** A running total of lost collections per
`(pid, iid, gen)`, emitted once per loss record at the window's `ts_start`.
The alternative, stepping up to `n_lost` at the window start and back to 0 at
its end, answers "how bad right here" directly but costs two packets per
window against one, draws a square wave at roughly ten windows per second per
generation, and has no readable final value. A monotonic staircase gives rate
as slope and total as its last point, which covers both questions with one
line.

**Rejected: a coverage ratio over time.** The most directly useful quantity
and the worst behaved. A ratio over the handful of collections in one poll is
noisy, and it is undefined for a window with no observations on that key,
exactly the case where loss is worst.

**Derived in the converter, not the monitor.** `convert_loss_to_trace_format`
already receives every `LossMsg` and is the single place both live and
`combine` paths pass through. The running total is per `(pid, iid, gen)`
converter state. This keeps the JSONL record in the shape ADR-0015 settled (a
consumer that ignores the counter sees exactly the same file) and means a
capture recorded before this lands still produces the counter when combined.

**Lanes are lazy.** Emit a generation's counter only once that generation has
lost something, the same convention ADR-0015 applies to the loss track
descriptor and OpenTelemetry applies to `otel.dropped_*_count`. A clean
capture grows no rows.

**Placement:** inside the per-process `GC Metrics` group (ADR-0003), beside
the existing `collected` / `candidates` / `duration` counters, not outside it:
what is drawn out there is `heap_size` and whatever a `ProcessTrack` owns
(ADR-0024), both process-wide rather than per-generation. Give all three
generations the same `y_axis_share_key` (ADR-0005) so they share a scale and
can be compared by eye. The lane also needs a row in `_COUNTER_ORDER` and a
name in `TestTheCountersInsideAMetricsGroupAreRanked`: a metric nobody listed
draws below every listed one, which is a position and not a decision.

**Note on the research this came from.** The grilling session recommended a
loss counter as insurance against Perfetto summarising away narrow loss
slices: *"a loss region much narrower than event spans may vanish when zoomed
out."* That reason does not hold here. gcmon's loss windows are the widest
thing on the track, and the `GC Pause` slices beside them are the sub-pixel
ones. Keep the recommendation and drop the reason, and do not carry the reason
into an ADR if this graduates.

## 5. Seams and testing decisions

- **Seam:** the trace processor's `counter` and `counter_track` tables, via
  the existing Perfetto integration tests. Counter values are queryable there,
  so the assertion is on what the trace means rather than on our own emission.
- **New seam needed:** none.
- **What makes a good test here:** assert the counter against the loss records
  that produced it, not against a literal: the final value equals
  `sum(lost_count)` for that `(pid, iid, gen)`, and the series is
  non-decreasing. A literal test would pass equally with an off-by-one running
  total.
- **Prior art:** the existing per-generation counter assertions in the
  Perfetto exporter tests, and
  `tests/cli/analyze/test_convert_cmd_perfetto.py` for the `combine` path.
- **Cases:**
  1. Three windows on one key produce a non-decreasing counter series whose
     last value is their summed `lost_count`.
  2. A generation that lost nothing emits no counter track and no descriptor.
  3. The counter reproduces identically from a JSONL capture through
     `gcmon combine`.
  4. Regression guard: the `GC Loss` slices, their args and the existing
     `GC Metrics` counters stay byte-identical. This adds lanes; it changes
     nothing.

## 6. Out of scope

- **A lost-pause counter.** The same staircase in nanoseconds. Cheap to add
  later and easy to misread as measured GC time, which is the confusion
  ADR-0015 built the separate track to avoid. Decide it after the count
  counter has been looked at.
- **Putting the count in the slice name.** Settled against in ADR-0015 because
  Perfetto colours slices by a hash of the name. If this counter turns out to
  answer "how much" well enough, that question closes for good.
- **A loss counter in the `--stats` table.** `Cov`, `F` and the lifetime
  totals already carry the run-level answer; this spec is about shape over
  time.
- **Correcting any aggregate using the counter.** It is a display of what
  `StreamingStats` already records, with no path back into the statistics.

## 7. Further notes

Settle when picked up: whether the counter is per `(pid, iid, gen)` or per
`(pid, gen)` summed across interpreters. Per-key matches the loss windows and
the `GC Loss` row, and is the default assumed above; per-`(pid, gen)` matches
the `--stats` table, which keys loss on `(pid, gen)`. A multi-interpreter
capture is what settles it: if the two interpreters' curves are legible
superimposed, sum them.
