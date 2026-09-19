# ADR-0017: Give the monitor every piece of per-pid state, and leave the loop the clock

- **Status:** Accepted
- **Date:** 2026-08-17
- **Amended by:** [ADR-0021](0021-write-one-trace-format.md),
  [ADR-0025](0025-create-every-process-in-one-place.md)
- **Modules:** monitoring

## Context

`EventsMonitor` held the ring cursors and the poll instant that
[ADR-0015](0015-gc-loss-spans-on-their-own-track.md)'s loss arithmetic runs
on. `MonitorLoop` held the `WaitPolicy` deciding when a pid was finished. Each
pruned its own half against the child pids, computed twice per tick from one
listing, through two expressions in two modules, with no test comparing them.

Both prunes read one listing, so the two never diverged and no trace was
affected. The split went because of what a divergence produces: a pid the OS
reuses inherits the dead process's `collections` cursor, the next poll
subtracts a fresh counter from a stale one, and gcmon draws a `GC Loss` span
for hundreds of collections that never ran. That span looks like data, so an
operator has no reason to distrust it.

## Decision

**One tick of monitoring is one call.** `EventsMonitor.tick` performs a whole
tick and reports which pids answered and whether any policy still wants the
run open. Child discovery, the prune, the control-plane enable check, the
poll, the policy verdict and the liveness report are all inside it, so the
monitor exposes no exporter: the loop would reach for one only to make that
liveness call.

**Per-pid state has one owner and one prune.** Cursors, poll instant,
streaming stats, wait policy and the process a pid is currently holding share
a lifetime, so one pass over one child set drops them together. The monitor is
therefore the only thing that creates a process or retires one
([ADR-0025](0025-create-every-process-in-one-place.md)), and the prune settles
a departing process's rings before retiring it, since the rings file under the
process that earned them.

**The loop keeps the clock and the stop signal.** It reads the tick instant,
lends the monitor a read of the `threading.Event` a signal handler sets, and
stops when the report says to. Everything per-pid is the monitor's.

**A pid the policy gives up on keeps its policy and loses its cursors.** A
replacement policy never saw the pid alive, so it answers "still starting" to
every later invalid poll and holds the run open for a whole startup timeout.

**A failed child listing prunes nothing.** `None` means the OS did not answer,
not that the tree is empty. Reading it as empty drops every live child's
cursor and re-exports its whole ring.

## Consequences

- A test drives one method and asserts on its report, instead of reproducing
  the loop's orchestration against a mock.
- Asserting the pruned state is empty would prove the prune ran, not that it
  was right. The evidence is a pid that leaves and returns holding an
  unrelated counter: it emits records and no loss window, while the same ring
  without the departure opens one. Both halves of policy-stays-cursors-go need
  an assertion each, since a test watching one half passes with the other
  inverted.
- A pinned whole-run trace holds what an operator sees across the loop and the
  monitor together.
- The liveness report comes from the monitor rather than `MonitorLoop`, and
  [ADR-0029](0029-report-liveness-and-fold-it-into-the-span.md)'s constraints
  on it hold unchanged.
- A pid that leaves the tree and returns re-exports whatever its ring still
  holds, since the prune took its cursor. Duplicate slices are the price of
  not fabricating a loss window. They are drawn on the process that produced
  them ([ADR-0025](0025-create-every-process-in-one-place.md)), and the
  settled ring counts them once
  ([ADR-0016](0016-the-ring-is-the-statistics-unit.md)).
- The monitor holds more state than it did and is still driven from one loop.
  Nothing here makes it safe to share.

## Alternatives considered

- **Leave the split, add a test that the two prunes agree.** Pins the coupling
  instead of removing it, and compares the two expressions only on inputs a
  test author thought of.
- **Move timing into the monitor too, leaving `MonitorLoop` a shell.**
  Rejected: the `Runner` seam makes a duration-limited run testable without
  waiting, and the stop event is what the signal handler sets. The count of
  modules was never the problem; the boundary ran through the per-pid state
  instead of around it.
