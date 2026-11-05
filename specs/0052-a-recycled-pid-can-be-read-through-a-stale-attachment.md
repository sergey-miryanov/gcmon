# 0052: Refuse to read a pid whose process changed underneath gcmon

- **Status:** Not started
- **Kind:** bug (correctness)
- **Effort:** S
- **Origin:** spec 0048 section 6, retired, see [RETIRED.md](RETIRED.md);
  filed once [ADR-0020](../docs/adr/0020-attach-to-a-process-once.md) existed
  to cite
- **Respects:**
  - [ADR-0020](../docs/adr/0020-attach-to-a-process-once.md): an attachment is
    dropped on every failed read, and revalidating on each read is rejected.
  - [ADR-0017](../docs/adr/0017-monitor-owns-the-pid-lifecycle.md): the pid
    epoch is what keeps a successor out of its predecessor's figures.

## 1. Problem

An operator attaches gcmon to a long-lived process tree on Linux. One worker
exits between two ticks and the kernel hands its pid straight to a new
process. gcmon never sees a failed read: the worker was there on the tick
before and something is there on the tick after, so it goes on reading that
pid through the attachment it already held. The trace it produces contains GC
records for that worker that no collection ever produced: plausible pause
durations, plausible counters, drawn on the worker's own track. The operator
has no way to tell them from the real ones, and no warning is printed.

## 2. Evidence

`gcmon.monitoring.events_reader.RemoteEventsReader` keeps one
`_remote_debugging.GCMonitor` per pid and reuses it for every read until a
read fails. `GCMonitor` resolves the target's runtime address and debug
offsets when it is constructed and revalidates neither afterwards; the reads
it performs are raw remote-memory copies at addresses derived from that
one-time resolution.

Two facts make the result data rather than an error:

1. **Nothing on the read path validates.** Every field gcmon consumes (`gen`,
   `iid`, `collections`, `ts_start`, `ts_stop`, `heap_size`, `duration`) is an
   integer or a double copied out of the target. The only filter is
   `gcmon.monitoring.monitor`'s completeness check, which rejects a slot whose
   `ts_start` is not below its `ts_stop`. Arbitrary memory satisfies that
   roughly half the time.
2. **The successor is a Python process of the same build**, in the case that
   matters. A recycled pid under a fan-out is very often the *same worker
   program being restarted*, so the address gcmon resolved is very likely
   still mapped and still holds a `gc_stats` structure, just a different one,
   with counters that have nothing to do with the ring gcmon was tracking.

`gcmon.monitoring.monitor.EventsMonitor` cannot see this either. Its prune
keys on the child listing, and the pid never left it. The cursor comparison is
what turns fresh counters into a loss window, and here the counters are not
fresh, merely unrelated.

**This is a change of consequence, not a new defect.** ADR-0017 was written
about the same window: before this, the worst case was a successor's records
attributed to its predecessor's cursor, which made a number wrong.
`GCMonitor`'s cached offsets change the worst case to records fabricated out
of an unrelated process's memory.

## 3. Scope

**Affected:**

- Linux, every subcommand that monitors live (`gcmon run`, `gcmon monitor`),
  every `--format`. Worse the wider the fan-out and the shorter the child
  lifetimes, because both raise the chance of a recycle inside one tick.

**Not affected:**

- **Windows.** Attaching opens a process handle, and a held handle keeps the
  process object alive, so the kernel will not reissue that pid while gcmon is
  attached (ADR-0020). The scenario is impossible there, not merely unlikely.
- **macOS.** The pid is reissued as freely as on Linux, but a read addresses
  the Mach task port the attachment holds, and a dead task's port is not
  rebound to the successor. The read fails, ADR-0020 drops the attachment, and
  the next poll attaches to whatever holds the pid now. The successor's
  counters then meet the predecessor's cursor and make a number wrong instead
  of inventing records: the older case ADR-0017 was written about.
  [Remote reads, per platform](../docs/internals/remote-reads.md) carries the
  evidence for both.
- **`gcmon combine`** and every offline path: no reads, no attachments.
- **A pid recycled while gcmon is *not* attached**, after a failed read or
  after the pid left the child listing. ADR-0020's lifetime already drops the
  attachment in both, so the next read attaches afresh.
- **`get_child_pids`.** Stateless and re-derived every tick.

**Why the suite does not catch it.** Every test of the prune drives a pid
*leaving* the listing or a policy *giving up*, because those are the routes
gcmon can observe. There is no test of a pid that stays in the listing while
the process behind it changes, because no seam gcmon has can currently tell
that happened. That is the gap this spec closes, and the fix and the test need
the same new fact.

## 4. Proposed change

1. **Give a pid an identity beyond its number, taken once at attach.** The
   process start time is the one cheap identifier the OS keeps that a
   successor cannot inherit: field 22 of `/proc/<pid>/stat`. Record it
   alongside the attachment. Linux is the only platform that needs one.
2. **Compare on the tick boundary, not on the read.** ADR-0020 rejects
   revalidating inside `read`, and that decision holds: a probe per read
   reintroduces a per-poll syscall against the target, which is the cost
   attaching once exists to remove. The comparison belongs in the pruning pass
   the monitor already makes once per tick, the same pass that reconciles the
   child listing.
3. **A pid whose start time changed is treated as a departure and an arrival,
   in that order.** Drop the attachment, drop the cursors, settle the
   statistics and advance the pid epoch, then let the next poll attach afresh.
   That is precisely what happens today when a pid leaves the listing and
   returns, so the machinery exists; what is new is the trigger.
4. **Say so once, at warning level.** A recycled pid under monitoring is a
   fact about the operator's workload that changes how they should read the
   trace, and unlike the ordinary departure it is invisible in the process
   listing.

**Rejected: comparing start times inside `read`.** The obvious fix, and it
loses: a syscall per pid per poll, the same shape as the cost 0048 removed,
and a platform-specific probe on the hot path for a condition that can only
change between ticks anyway.

**Rejected: making the pid epoch part of what the reader keys on.** It moves
the problem rather than solving it: the epoch only advances when gcmon
*notices* the death, which is the thing it cannot do.

**Open, and what would settle it:** whether the start-time read belongs behind
the existing `EventsReader` seam or beside it as a separate collaborator. What
settles it is whether anything other than the reader ever needs a process's
identity. If the answer stays no, it belongs to the reader.

## 5. Seams and testing decisions

- **Seam:** the injected `EventsReader`, plus whatever `psutil`-or-equivalent
  seam the start-time read lands behind. Both are already injected, so a test
  can hand the monitor a pid whose identity changes between two ticks without
  needing a real recycle, which is untestable: no test can make the kernel
  reissue a chosen pid.
- **New seam needed:** the process-identity read has to be injectable. It is
  the only new one, and it is required: the whole defect is about a value the
  test must control.
- **What makes a good test here:** assert on what reached the exporter and the
  statistics. The fabricated-record case has a specific signature, records
  under the *old* pid epoch with counters unrelated to the tracked ring, and
  the fix's signature is a settled block for the predecessor and a fresh one
  for the successor. Asserting that the attachment was dropped proves the
  prune ran, not that the trace came out right.
- **Prior art:**
  `tests/monitoring/test_monitor.py::TestARecycledPidStartsFromNothing`, which
  already models a pid leaving and returning with an unrelated counter and
  asserts no loss window is drawn. This is the same shape with the departure
  hidden. Its control case, the same counters *without* a departure, which
  must still draw a loss window, is what gives the new assertion teeth and
  should be mirrored.
- **Cases:**
  1. A pid whose identity changes between two ticks, while staying in the
     child listing, settles its predecessor and starts the successor from
     nothing: no loss window, and the successor's records under a new pid
     epoch.
  2. The control: a pid whose identity is unchanged across a counter jump
     still draws the loss window ADR-0015 says it should. Without this, a fix
     that treated every tick as a recycle would pass.
  3. A pid whose identity cannot be read, because the process exited between
     the listing and the probe, is treated as a departure, not as an error.
  4. Regression guard: the attach count over a run of stable pids is
     unchanged. A fix that re-attaches on every tick would silently undo 0048,
     and only a count catches it.

## 6. Out of scope

- **Windows and macOS.** Neither can be served a read from a recycled pid, so
  the identity read is a no-op on both and should be written as one rather
  than as a platform branch with live arms.
- **A record-level plausibility filter**, bounds on `duration` or monotonicity
  of `ts_start` against the previous record. Tempting, and a trap: it would
  reject some fabricated records and some real ones, and it would leave gcmon
  unable to say which. The problem is that gcmon read the wrong process, and
  the fix is to know that, not to grade the output.
- **Torn reads and reordered publishes**, which the target's sequence counter
  closes ([ADR-0015](../docs/adr/0015-gc-loss-spans-on-their-own-track.md)).
  Those were races inside a target gcmon is correctly attached to. This is the
  opposite: a correct read of the wrong process.
- **The version-mismatch classification** 0048 section 6 also left open. It
  shares a cause with this, both wanting a failure gcmon can tell apart from
  "not started yet", but it wants `debug=False`, which means arguing against
  ADR-0020, and it is operator-facing where this is silent.
- **Anything about what a record means or how loss is computed.** ADR-0015 and
  ADR-0016 own those.

## 7. Further notes

The window is narrow and the consequence is severe, which is an awkward
combination to prioritise. Two things argue for taking it despite the
narrowness: it is silent, so nobody will report it; and the output is
indistinguishable from a real measurement, so an operator who hits it draws a
wrong conclusion about their own workload rather than about gcmon.

Sizing note: the pid-epoch machinery, the settle-and-restart path and the
prune pass all exist and all do the right thing already. What this adds is one
fact per attachment and one comparison per tick, on one platform. The cost is
the test seam, not the logic.
