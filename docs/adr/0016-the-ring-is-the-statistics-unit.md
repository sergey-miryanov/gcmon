# ADR-0016: Report statistics per ring, and drop the per-process row from the `--stats` table

- **Status:** Accepted
- **Date:** 2026-08-15, amended:
  - 2026-08-26: `heap_size` gained its interpreter on the trace, see
    [ADR-0024](0024-an-event-names-the-track-it-is-drawn-on.md)
  - 2026-08-31: the epoch moved onto a `Process` the monitor creates, see
    [ADR-0025](0025-create-every-process-in-one-place.md)
  - 2026-09-11: `heap_size` lost its interpreter from the track name, see
    [ADR-0027](0027-group-every-row-an-interpreter-owns.md)
  - 2026-09-01: the track keys it cites became per process, see
    [ADR-0011](0011-process-lifetime-and-ordering.md)

## Context

Every interpreter in a target keeps its own collector, its own rings and its
own cumulative counters. The trace side has said so since
[ADR-0015](0015-gc-loss-spans-on-their-own-track.md): records go on a thread
track per `(process, iid)`, loss spans on a `GC Loss` track per
`(process, iid)`, and [ADR-0003](0003-gc-metrics-group-track.md)'s counter
group is per `(process, iid)` too. Open a trace of a process running three
interpreters and you see three rows.

The statistics side kept an older key. Sampled durations accumulated per pid
and loss per `(pid, gen)`, so two interpreters' gaps landed in one slot with
nothing left to tell them apart. Lifetime totals were keyed per
`(pid, iid, gen)`, and their only reader summed the interpreter away before
printing.

A `GC Pause(0)` row reported one blended distribution for interpreters that
may run different workloads, so its `P50` through `P99` described none of
them. `Cov` divided a pid-wide sampled count by a pid-wide lost count, so a
busy interpreter masked a starved one beside it and the mid-run advisory that
fires below 90% stayed silent on captures worth discarding. The footer's
lifetime note read "Since interpreter start" over a sum across interpreters
that started at different moments, and across processes where a pid was
reused.

Nothing downstream can unfold coverage that was folded before it was stored,
so the fix belongs in the key.

## Decision

**The ring is the unit statistics are reported for.**
`gcmon.stats.streaming_stats` holds one entry per `(process, iid)`, the pair a
`Track` names ([ADR-0024](0024-an-event-names-the-track-it-is-drawn-on.md),
[ADR-0025](0025-create-every-process-in-one-place.md)), and each entry keeps
its sampled metrics, loss and lifetime totals in a dict per generation. A ring
is one of those generations, and the three settle together because one entry
holds them. gcmon folds when it reads a figure, so a ring's own number and a
roll-up over rings both stay available.

**The `--stats` table prints two levels: the run, and the ring.** `Total`
stays, the one answer to what a run cost. The per-process block goes, its rows
having blended interpreters the trace keeps apart.

**The first column is headed `PID:IID`, and every ring row carries both
parts**, `12345:0` on an ordinary single-interpreter run as much as on a tree.
Dropping the `:0` would leave a header naming two fields over cells holding
one.

**`Total` keeps its percentiles.** They are quantiles of a mixture and the
footer says so.

**The bound of 256 counts the interpreters still running**, replacing a bound
of 64 processes. At the same footprint per entry, it buys those 64 processes
four interpreters each.

**A ring settles when its process exits, and never before.** gcmon learns a
pid has gone when the tick's listing of the target's children drops it, or
when a wait policy gives it up, and settles that pid's rings at either. The
sample buffers go back, the slots go back, and the four percentiles left
behind cover each of those rings end to end. A target that spawns and exits
keeps a row per process it ran without exhausting the bound.

**`gcmon.monitoring.monitor` decides who is alive, and
`gcmon.stats.streaming_stats` takes that decision.** Whatever arrives on a pid
gcmon called dead is a new process, the same one or not. The statistics never
infer liveness a second time from the target's counters, so the two sides
cannot disagree about which process a figure describes.

**Everything a run keeps names the process rather than the pid.** A `Process`
carries an epoch counting from 1 that advances on each of those deaths, so
whatever claims the pid next starts clean: its own entry, its own loss, its
own lifetime counters. The epoch belongs to the pid rather than the ring
(`pid_epoch` in the code, since a ring's own index is CPython's write cursor
into it), so an interpreter a successor creates late counts as the
successor's. The table prints the first block plain and marks the ones after
it, `12345:0#2` for the second process to hold the pid. The monitor assigns
the epoch and nothing here does
([ADR-0025](0025-create-every-process-in-one-place.md)).

**A settled ring turns away a record it has already counted.** A pid pruned
from the tree loses its read cursor
([ADR-0017](0017-monitor-owns-the-pid-lifecycle.md)), so its successor
re-reads the ring and the records come back filed under the predecessor
([ADR-0025](0025-create-every-process-in-one-place.md)). Folding one in a
second time would leave the run totals and the ring's percentiles out by a
duplicate.

**A ring gets its row on its first record and keeps it until its process
exits.** Where no slot is free the ring gets none and its records reach
`Total` alone, which takes 256 interpreters running at once. gcmon says so
twice, in a warning the first time and in a footer note counting the rings
left out. A slot freed later opens no row for that ring, since a row starting
mid-life reads as a whole one.

**The bound holds back sample buffers, not the ring.** A declined ring keeps
its loss and its lifetime counters, four numbers a generation against a
thousand values a generation a metric, so `Total`, the coverage figures and
the lifetime note stay whole on a target too wide for the table.

**The coverage advisory tests rings.** It names the least covered one,
interpreter alongside pid and generation, and fires once per run, latched in
`gcmon.monitoring.monitor`: the remedy it suggests is `--rate`, which no ring
owns. Firing once is why it names the worst ring over the first, since a
marginal figure would otherwise stand for the whole capture.

**The lifetime note folds, and says what it folded.** One line per generation
whatever the size of the tree, stating the interpreter and process counts it
summed over:

```
2. Since each interpreter started, monitored window included, summed over 3 interpreters
   in 2 processes: Gen0 4820 in 6231.400 ms.
```

**Process-wide quantities stay keyed per process.** `heap_size` has no
generation, so no ring owns one, and its high-water mark is taken per process,
with two processes that shared a pid keeping a mark each. The trace draws it
per interpreter instead, a `heap_size` row on each interpreter's group
([ADR-0027](0027-group-every-row-an-interpreter-owns.md)), so the two sides
fold it differently. The end-of-run summary and the coverage footnote stay
run-wide, the scope `Total` reports.

## Consequences

- **Ordinary output changes.** A single-interpreter run's rows go from `12345`
  to `12345:0`, so the documented examples and the row-level assertions in the
  suite move with it. This is user-visible and belongs in the CHANGELOG.
- **The advisory fires on runs that are silent today.** A starved interpreter
  beside a busy one trips the 90% floor on its own figure. That is the defect
  being fixed, and it makes the warning noisier on trees that were this
  incomplete before.
- **`Total` holds the table's only blended percentile.** Every other row
  describes one distribution.
- **Footprint scales with interpreters running at once.** A process creating
  many interpreters consumes ring slots that a process-keyed bound gave it for
  free. A tree wide enough to exhaust the bound leaves its later rings out of
  the table.
- **Every printed row covers one process's ring over one unbroken stretch**,
  so a row's `Count`, its `Sum` and its percentiles always describe the same
  records. A settled ring cannot take more values, and one that tries raises.
- **A run can print two blocks under one `PID:IID`**, where the operating
  system handed the pid out twice. The `#2` on the second says why, and a
  reader who never sees one loses nothing.
- **The rows can be short of the run**, which the footer note states as a
  count. `Total` is a separate accumulator fed once per record, so the run's
  cost stays whole where its detail does not.
- **Reading a capture back from JSONL gives the same numbers as reading it
  live**, because the replay path keys loss per ring too. A key without an iid
  could not.
- **A reused pid no longer corrupts the lifetime fold.** A successor's much
  smaller cumulative counters write a slot of their own, so the fold adds the
  two histories instead of losing the longer one to the shorter one that
  follows. The footnote's process count stops understating a target that
  recycles pids.
- **The epoch depends on gcmon seeing the exit.** A pid recycled between two
  ticks, with the listing never showing it gone, reads as one process
  throughout. ADR-0015 accepted the related hazard on the monitor side, and
  the cursor there has the same blind spot.
- **A death called wrongly costs.** Where gcmon gives up on a pid whose
  process is still running, the records after that point are a second process:
  a second block, and a second cumulative lifetime reading that the fold adds
  to the first. That is the price of one side owning liveness. Reading a
  restart out of the counters would settle this case and reopen the one it
  replaced, since a target is free to report that counter either way.
- **A benchmark's metadata keeps its released key names.** They are flat,
  run-wide scalars, the scope a benchmark wants and the scope `Total` reports.
  Per-ring keys would embed pids that differ every run.

## Alternatives considered

- **Keep the process rows and add ring rows beneath them**, printed where a
  process ran more than one interpreter. Rejected: ordinary output stays
  untouched, but the blended process row stays too, and the table's depth then
  depends on the target's shape.
- **A separate `IID` column beside `PID`.** Machine-readable and uniform, and
  it widens every table by a column reading `0` on most runs. The compound
  cell carries both fields in the width already there.
- **Leave the arithmetic alone and fix only the wording.** Rejected: coverage
  folded at record time cannot be unfolded, so no footnote recovers a
  per-interpreter figure that was never stored.
- **Key only loss and lifetime totals per ring, leaving the sampled buffers
  per process.** `Count`, `Sum`, `Cov` and `F` become per-ring while
  percentiles stay blended. Rejected: the blended percentile is the number
  most likely to be quoted out of the table, and the split needs explaining
  wherever a column is documented.
- **Blank the distribution columns on any row folding more than one ring.**
  Consistent, and it costs `Total` its `Avg` and its `P99`, the two figures a
  run is summarized by. The footnote guards the same misreading for less.
- **Drop `Total` as well, printing rings only.** Rejected: a run has one cost,
  so a roll-up over everything has one answer. A roll-up over the interpreters
  of one process does not.
- **Bound interpreters per process rather than rings overall**, admitting
  every interpreter of an admitted process. Rejected: the footprint then has
  no bound in the dimension that grows.
- **Evict the least recently used ring, and let it resume its entry when it
  comes back.** What the branch did first. A resumed row printed `Count` and
  `Sum` over the ring's whole life beside percentiles covering only the
  stretch since it returned, and no column said so. And a materialized entry
  outlives its process, so a reused pid resumed a dead one and added a second
  process's records to it. Settling on exit costs the LRU policy and buys an
  entry that only one process can write to.
- **Give the successor of a reused pid no block at all**, counting its records
  in `Total` and naming it in the footer. Tried between the two, and it keeps
  every block honest for a line of code. Rejected: a live process gets no row
  so that a dead one can keep its heading, and on a target that recycles pids
  the table thins as the run goes on. The epoch costs one integer per pid and
  gives both processes what they earned.

## Implementation

- `src/gcmon/stats/streaming_stats.py` keys sampled metrics, loss and lifetime
  totals on the ring, bounds the active set, and answers both a ring's totals
  and a fold over them. One entry holds all three, so a ring's numbers settle
  together, and a key is the process and the interpreter rather than three
  numbers.
- `src/gcmon/stats/stats_output.py` builds the table's two levels and the
  footer notes, and owns the `PID:IID` spelling.
- `src/gcmon/monitoring/monitor.py` passes the iid it has in hand when
  recording loss, holds the advisory's once-per-run latch, and settles a pid's
  rings where it drops that pid's monitor state.
- `src/gcmon/pyperf/hook.py` keys loss per ring when replaying a capture from
  JSONL, so the offline path reconstructs what the live path recorded.
- `tests/stats/test_stats_output.py` pins the table's two levels and the
  footer wording; `tests/stats/test_stats.py` pins the per-ring arithmetic,
  the settling and the bound; `tests/test_loss_replay.py` pins that a replayed
  capture agrees with the live one.
