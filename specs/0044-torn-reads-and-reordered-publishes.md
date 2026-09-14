# 0044: Close the two unsynchronized-read hazards, once CPython synchronizes the ring

- **Status:** Blocked (`upstream: the publish/read synchronization proposed in
  [0024](0024-cpython-report-remote-readable-gc-stats.md) sections 3.3 and 3.5
  has to land in CPython first`)
- **Kind:** bug (correctness)
- **Effort:** S once unblocked; see section 7 for the M-sized alternative that
  does not wait
- **Origin:** the loss-spans working set, itself from ADR-0015's
  `## What gcmon trusts the target for`, which names both hazards and
  mitigates neither
- **Respects:**
  - [ADR-0015](../docs/adr/0015-gc-loss-spans-on-their-own-track.md): what
    gcmon reconstructs from an incomplete sample.
  - [0024](0024-cpython-report-remote-readable-gc-stats.md): the upstream
    report both hazards come from.

## 1. Problem

Two wrong numbers can reach an operator with nothing marking either.

A `GC Pause` slice can be one inter-collection interval too long. The operator
reads a 40 ms pause where the target took 4 ms, and the record around it looks
ordinary: it has a fresh `collections`, its `ts_start` is less than its
`ts_stop`, and it sits in counter order beside its neighbours.

A loss window can be short of what it should carry. The `--stats` table's
`Count` and `Sum` stay right, since both are subtractions of end counters, but
the pause of the runs inside a hole is attributed to no window at all, so the
exact pause sum under-reports and `Cov` claims a completeness the capture does
not have.

Neither shows up as an anomaly. Every filter gcmon has passes both, and
re-running the capture gives a different answer with no way to tell which run
was the wrong one.

## 2. Evidence

Both hazards come from reading a live ring with no barrier between the writer
and the reader. [0024](0024-cpython-report-remote-readable-gc-stats.md)
sections 3.3 and 3.5 write them up against CPython 3.15.0b3
(`tags/v3.15.0b3-dirty:cf16a33fad1`) with the sources cited there; this
section covers only what they do to gcmon.

**Store reordering (0024 section 3.3).** `add_stats` in `Python/gc.c`
publishes `ts_stop` last so a remote reader never selects a half-written
record, and the stores implementing that contract are plain. Delivered out of
order, a slot holds the previous run's `ts_start` against this run's `ts_stop`
under a fresh counter. gcmon's two filters both pass it:
`monitor._is_complete` tests `ts_start < ts_stop`, which holds, and
`RingAccumulator.unseen` keys on `collections`, which is fresh. The record
then reaches `RingAccumulator.ingest`, which adds its span to
`sampled_pause_ns`, and reaches the exporter as a slice.

**Straddled read (0024 section 3.5).** `_remote_debugging.get_gc_stats` copies
the whole `struct gc_stats` out of the target in one cross-process read with
nothing holding the target still. A collection finishing partway through
advances the write cursor across a region already copied, so one poll can
return slots from two generations of the ring: sorted on `collections`,
consecutive at both ends with a hole in the middle. `RingAccumulator.ingest`
says what it does with that in its own docstring, *"A ring holds consecutive
records, so only the first of them can sit across a gap and only the last
settles the cursor. Contiguity it trusts without checking, see ADR-0015."*,
and `_gen_loss` is only ever called on `events[0]`, so a hole after it opens
no window.

ADR-0015 records the same two in its `## What gcmon trusts the target for`,
under **"Two hazards break the first two properties, and gcmon mitigates
neither yet."**

## 3. Scope

**Affected.** Every command that reads a live target (`gcmon monitor`,
`gcmon run`, and the pyperf hook's live path) on every `--format`. Both
hazards are in the read, so a wrong number is already wrong before any
exporter sees it.

**Not affected.** `gcmon combine` and the pyperf hook's `_replay`, which read
a JSONL capture: replay reproduces faithfully whatever the live run recorded,
so it neither introduces these nor repairs them. Free-threaded builds cannot
form the straddled read at all: `GC_YOUNG_STATS_SIZE` is 1 there, so a poll
returns at most one record per generation and there is no interior slot to
straddle. Lifetime totals rest on neither hazard: `collections` and `duration`
are cumulative from interpreter start, so a reordered or straddled read of the
ring leaves them intact.

**Why the suite does not catch it.** Both are races inside the target, and
every fixture in `tests/captures.py` is synthesized from complete, consecutive
records, so neither shape is generated, so no assertion can fail on one. This
is also why a fix needs the ground-truth captures named in section 5 rather
than a unit test alone.

## 4. Proposed change

**The decision is to wait, and the reason is that gcmon cannot see the seam
for one of the two.**

1. **Build no mitigation while the target is unsynchronized.** For the
   straddled read, gcmon never performs the cross-process copy itself:
   `_remote_debugging.get_gc_stats` does it and hands back a snapshot, so the
   moment the ring moved under the read is not observable from Python at all.
   A retry belongs in `Modules/_remote_debugging/gc_stats.c`, which is what
   0024 section 3.5 asks for. For the reordering, gcmon does receive the
   fields, but the two it would compare are exactly the two the reordering
   makes inconsistent, and a record that survived a reorder is
   byte-indistinguishable from a genuine one.
2. **When CPython synchronizes the publish and the read, gcmon inherits it
   without a code change.** A seqlock applied in `gc_stats.c` retries inside
   the module gcmon already calls, so the snapshot that reaches
   `EventsMonitor._ingest` is coherent by construction. Confirm this against
   the first version carrying the fix rather than assuming it: if the retry is
   exposed to the caller instead of handled internally, this step becomes a
   real change and this spec grows.
3. **Version-gate the caveat.** ADR-0015's
   `## What gcmon trusts the target for` and 0024 section 4's handling table
   both state "not handled" unconditionally. Once a fixed CPython exists the
   statement is version-dependent, and the docs have to say which side of the
   line a capture was taken on; an operator holding a trace needs to know
   whether the caveat applies to it.
4. **Re-verify with ground truth** per section 5 before either caveat is
   relaxed. "The hazard is fixed upstream" is a claim about the target, and
   the property gcmon should assert is its own: the records one poll returns
   for a ring are consecutive.

**What would settle acting sooner instead:** the interior-hole check in
section 7 firing on no genuine record across the ground-truth captures. That
is a measurement, not an argument, and until someone runs it the check stays
unbuilt.

## 5. Seams and testing decisions

- **Seam:** `RingAccumulator.ingest`. It already receives exactly one poll's
  records for one ring, in counter order, and already owns the arithmetic that
  turns a gap into a window, so it is the highest place a hole is both visible
  and repairable. `EventsMonitor._ingest` sits above it and would work too,
  but it holds every ring of a pid at once and would have to re-group.
- **New seam needed:** none.
- **What makes a good test here:** external behavior only: the reconstructed
  exact count, the exact pause sum, and the loss window's
  `lost_from`/`lost_count`/`lost_pause_ns`. Assert on the reconstruction,
  never on whether a detector fired; a detector that fires and repairs nothing
  is the outcome section 7 argues against.
- **Prior art:** `tests/model/test_loss.py` for the per-ring arithmetic and
  `tests/captures.py` for building a poll's record set with a known ground
  truth. `tests/monitoring/test_loss_replay.py` is the model for asserting
  that two paths agree on one capture.
- **Cases:**
  1. One poll's records for a ring carrying an interior hole: today the hole's
     pause reaches no window, and `exact_pause_ns` is short by it.
  2. A record holding its predecessor's `ts_start` against its own `ts_stop`:
     today its span is added to `sampled_pause_ns` whole.
  3. The regression guard: an ordinary poll, with consecutive records and the
     mid-write slot at the head, produces byte-identical numbers to today.

## 6. Out of scope

- **The other three findings in 0024.** The ring sizes (section 3.1) are what
  loss reconstruction exists for; exposing `index` (section 3.2) would replace
  a sort, not a correctness gap; the duplicate twin (section 3.4) is already
  handled by `RingAccumulator.unseen`, which keys a dict on `collections`.
- **Filing the upstream report.** That is 0024's own lifecycle, and this spec
  is downstream of it landing rather than of it being filed.
- **Anything on the exporter or trace side.** Both hazards are settled before
  a record reaches an exporter, and no trace-side change can distinguish a
  wrong record from a right one.
- **Free-threaded builds.** The straddled read cannot form there, and the
  one-slot ring is 0024 section 3.1's problem, not this one's.

## 7. Further notes

**The alternative this spec defers, and why it is not dead.** The two hazards
are not equally opaque, and the difference is worth writing down because the
obvious reading, "neither is detectable from the reader", is only true of one
of them.

The straddled read *does* leave a signature. One poll returns every slot of
the ring, so the records it yields for a ring should be consecutive on
`collections`; a hole in the middle of that set means the cursor crossed the
read. The two shapes that could produce a false positive both sit at the head
rather than the middle: a mid-write slot, which `monitor._is_complete` already
drops, and the section 3.4 twin, which is a duplicate rather than a gap. And
an interior hole is not merely detectable but repairable:
`RingAccumulator._gen_loss` already converts a counter gap into a window from
the cumulative `duration` delta, and applying it at an interior hole rather
than only at `events[0]` would attribute the pause that is lost today.

That is an M-sized change and it is deferred, not rejected, on two grounds:
the upstream fix deletes the shape entirely, so the code would exist to handle
a case that stops occurring; and the false-positive cost is asymmetric: a
check that fires on a genuine record discards it and widens a gap, which is
worse than the hazard, since loss is already reconstructed. Section 4's
measured condition is what would reopen it.

The reordering has no equivalent. Every fingerprint it leaves is one a genuine
record can also leave, so any client-side test for it is a heuristic over
plausible pause lengths, and that is a detector that eventually drops a real
long pause, the one record an operator most wants.

**Where the answer goes.** ADR-0015's `## What gcmon trusts the target for` is
the statement of record and is amended when this lands, per
[CONVENTIONS.md](CONVENTIONS.md) rule 4. 0024 section 4's handling table
carries the same two rows and stays in step with it.
