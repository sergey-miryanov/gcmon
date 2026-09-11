# ADR-0015: Draw reconstructed GC loss on a per-interpreter track, one span per poll interval

- **Status:** Accepted
- **Date:** 2026-08-05, amended:
  - 2026-08-26: the sentinel tid became a `LossTrack`, see
    [ADR-0024](0024-an-event-names-the-track-it-is-drawn-on.md)
  - 2026-09-01: the track key became per process, see
    [ADR-0011](0011-process-lifetime-and-ordering.md)
  - 2026-09-11: the loss row moved onto the interpreter group, see
    [ADR-0027](0027-group-every-row-an-interpreter-owns.md)

## Context

CPython 3.15 exports GC records through a fixed ring buffer, a few slots per
generation and one per generation under `Py_GIL_DISABLED`. A target collecting
faster than gcmon polls overwrites records before any poll reads them, and the
read gives no sign of it.

Two cumulative fields make the loss measurable. `collections` counts the runs
that finished and `duration` totals their pause seconds, so the gap between
one poll's last counter and the next poll's first carries an exact count and
an exact pause sum.

Where inside that gap the missing runs happened, nothing the target exports
says. Every question below comes back to that split: the counts are exact,
their placement is guesswork.

## Decision

**Loss spans go on a `GC Loss` track of their own, one per `(process, iid)`.**
The track is a custom slice track parented to that interpreter's group, ranked
below the interpreter's pause row inside it
([ADR-0027](0027-group-every-row-an-interpreter-owns.md)). It carries no
OS-association sub-message, which stops Perfetto from drawing a track as a
thread that does not exist. A `LossTrack` names it
([ADR-0024](0024-an-event-names-the-track-it-is-drawn-on.md)), where a
sentinel tid extending [ADR-0013](0013-rss-sampling.md)'s did before.

**A span is one poll interval**, from the read before the gap to the read that
found it, which is the tightest bound available. A record the previous poll
did not return was either lost already, which that poll reported itself, or
not yet written, so every run a span names finished between those two reads.

**Both edges come from the monitor's clock**, `time.monotonic_ns()` taken at
the start of the read. Anchoring both on the same point makes consecutive
intervals tile the timeline, where a left edge from one poll's start against a
right edge from another's finish would overlap by the width of a read.
ADR-0013's RSS samples already land on this timeline beside the target's own
timestamps.

**One span, named for the generations that lost records**, `GC Loss(0,2)`. A
record goes missing between two reads, so the generations go blind over the
same interval and differ only in their counts. Perfetto hashes the name, so
each combination keeps a stable colour and a reader sees which generations
went blind before clicking anything.

**A span's width is uncertainty, not GC time.** One short lost run can draw a
bar as wide as the poll interval, which beside the `GC Pause` slices reads as
a very long pause. That is why loss gets a row of its own.

The bar also covers the runs gcmon did see. Runs inside an interpreter
serialize, so no lost run happened during an observed one, and trimming the
bar around them would still guess at where the missing ones ran. The args
report how much of the interval survived instead.

**The row is a sequence.** Consecutive intervals meet at a poll instant, so
spans touch without crossing and the track needs no clipping sweep of the kind
[ADR-0011](0011-process-lifetime-and-ordering.md) built for process lifetimes.
Touching puts one span's END on the same timestamp as the next one's BEGIN,
which makes their emission order load-bearing.

**The args are the interval's totals, then one group per generation.**
`observed_count`, `lost_count`, `seen`, `lost_pause` and `lost_pause_ns` sit
at the top level. Each generation that collected or lost something gets a
`gen0` / `gen1` / `gen2` group under them. A generation that came through
whole still gets its group, which makes `seen` checkable, since the groups add
up to the totals. A generation that neither collected nor lost anything is
left out.

`lost_pause_ns` sums what the lost records cost rather than measuring the bar,
which is the whole reason it is an arg and not the slice's width. It appears
twice, as a number for SQL and as `lost_pause` in text for reading, and `seen`
carries its counts for the same reason. A group reaches Perfetto as a
`DebugAnnotation` holding `dict_entries` and no value of its own, which the
trace processor flattens back under the group's name.

**A span names the records it is missing**, as `lost_collections` in each
group, both ends included and written as one field. A ring that loses a single
record puts a pair of numbers on the same counter, and a range of nothing
reads as a mistake to anyone who does not already know the ends are inclusive.
Only `lost_from` is stored; the far end follows from it and `lost_count`,
since a stored pair could drift from the count `--stats` sums. The range makes
the reconstruction checkable: between the first and last record gcmon read on
a ring, every run is either drawn as a `GC Pause` slice or inside exactly one
span's range for that generation, none twice and none unaccounted for.

**The reconstruction answers to the target's own totals.** Per ring, the pause
time gcmon sampled plus the pause time it attributed to the gaps equals the
delta of `duration` across the whole observed span. The suite asserts that
invariant, which catches a fencepost error, a wrong gap, and a `duration` that
does not share a clock with the timestamps.

**The statistics record each gap as it is found**, before anything is drawn,
so coverage, the scale factor and every aggregate stay independent of what the
trace shows.

**Loss records leave the monitor a poll at a time.** Nothing is retained, so
no flush is needed when a session stops or a pid goes away and no buffer grows
over a long run. Emitting there keeps loss inside the shared converter, so
every exporter takes it from one place and
[ADR-0007](0007-shared-trace-converter-pipeline.md) holds. `combine` can then
rebuild the spans from a JSONL capture.

## What gcmon trusts the target for

**The publish-last contract survives to the reader.** `add_stats` in
`Python/gc.c` orders a record's stores so that a remote reader never selects a
half-written one. A read can still land on a slot twice over, or on one still
being filled, with nothing in the data marking either; gcmon's two filters
exist for that.

**One poll's records for one ring are contiguous.** A ring holds consecutive
records, so a gap can only sit at the seam between two polls, and gcmon trusts
that without checking. A hole inside one poll's records would still leave the
counts right, since a count subtracts two end counters, but no gap would carry
the hole's pause and the invariant would break in silence.

**`duration` and the timestamps share a clock.** One is a `double` and the
others are `PyTime_t`, and the arithmetic subtracts one from the other. The
invariant tests it, and a failure there means the whole reconstruction is
unsound.

**Two hazards break the first two properties, and gcmon mitigates neither
yet.** A torn read returns one poll's records with a hole in them. Store
reordering returns a record holding the previous run's `ts_start` against this
one's `ts_stop` under a fresh counter, which both filters pass and which
reports a pause too long by the interval between the two runs.
[Spec 0024](../../specs/0024-cpython-report-remote-readable-gc-stats.md)
catalogues both for an upstream report, and
[spec 0044](../../specs/0044-torn-reads-and-reordered-publishes.md) settles
the reader's side: gcmon waits for the target to synchronize rather than
guessing, since one of the two leaves no signature a reader can trust and the
other is invisible from Python entirely.

Lifetime totals rest on none of this. `collections` and `duration` run
cumulative from interpreter start, and `gc_get_prev_stats` reads the immediate
predecessor rather than the slot about to be overwritten, so the chain
survives the ring wrapping.

## Consequences

- **The track reads as a near-solid bar at default settings**, since gcmon is
  blind for most of every tick. A lower `--rate` or a calmer workload thins it
  out, and the numbers live in the args either way.
- **One extra row per `(process, iid)`**, on top of the tracks each process
  already carries. Three generations share it, so the row's shape does not
  depend on how many went blind.
- **Sums and counts become exact; percentiles do not.** `Count` and `Sum` come
  back from the target's own counters. gcmon holds only the durations it
  sampled, and that sample skews long, so `P50` through `P99` read high. The
  scale factor corrects a sum, being a ratio of two totals; a quantile would
  need the sampled and unsampled runs to share a distribution, which is what
  the skew denies. [`docs/statistics.md`](../statistics.md) documents this
  rather than correcting it.
- **A loss row whose spans overlap corrupts in silence**, the first END
  closing the wrong span while the trace processor reports
  `misplaced_end_event = 0`. One span per poll cannot produce that shape, so
  the default suite pins the row's flatness rather than leaving it to `fuzz`.
- **The intervals either side of the observed span draw nothing.** No poll
  measured a `collections` delta across them, and gcmon cannot tell "ran
  before we attached" from "lost". Both fall outside the span rather than
  counting against coverage.
- **A pid reused inside one tick is measured against its predecessor's
  counter**, so gcmon reports a gap belonging to neither process. Accepted
  without code: reuse that fast needs the pid allocator to wrap, and a
  successor gcmon cannot read returns `INVALID_PROCESS`, which clears the
  rings.
- **A duplicated export can push `Cov` above 1.0.** After gcmon drops a pid's
  rings the next poll re-exports the whole ring, and those duplicates inflate
  a sampled count that now divides into an exact one. Clamping the ratio would
  hide the duplication.

## Alternatives considered

- **One span per generation, each running to that generation's next observed
  record.** Rejected on the widths: they come from where the next record
  happens to sit rather than from when anything was lost, so three bars at
  three widths read as three events at three times. They also need sorting
  into nesting order, can arrive reversed when a read tears, and say nothing
  the grouped args leave out.
- **Narrowing a span around the records observed inside it**, cutting it into
  pieces that share the counts and pause between them. Rejected: nothing in
  the ring says how the records divide, so every division is a guess and the
  only estimated quantity in the drawing. A share taken by width can hand a
  piece more pause than it is wide. A reader can still narrow it by hand, from
  the `GC Pause` slices on the row above and `observed_count` in the args.
- **Clipping a span's far end to the poll's earliest observation anywhere in
  the interpreter.** The same guess in a subtler form. Oldest-first eviction
  orders a ring's lost records against that ring's kept records and says
  nothing about another generation's, so a lost gen-0 run can have happened
  after an observed gen-2 one. Clipping can also leave a span narrower than
  the pause it reports. This rejects the **eviction-order** inference, not
  every narrowing. A bound taken from a record still in progress at the read
  is temporal: that record held the interpreter at that moment, and
  collections in an interpreter are serialized, so anything lost after the
  read ran after that collection ended, whatever generation either belongs to.
  No ring, no cross-key inference. Rejecting the first argument leaves the
  second open.
- **Bounding a span by how many runs it could hold**, taking a ring's shortest
  observed interval between two records as a floor on its period. Rejected:
  one fast burst weakens that floor for the whole session, and it errs in the
  dangerous direction, since a stretch narrower than the floor that did hold a
  record loses it. Loss happens when runs come fast.
- **Inline on the interpreter's own row**, either with an ADR-0011-style
  clipping sweep or snapped to the adjacent observed records. Rejected: an
  interval-width bar beside the pause slices invites the misreading a separate
  track prevents, clipping shortens spans whose width is the claim being made,
  and snapping draws the loss as a pause of known extent.
- **One track per `(pid, iid, gen)`.** Three rows say what the args say, at
  three times the vertical cost, and a process with several interpreters would
  carry nine.
- **One row for every interpreter.** Rejected: interpreters collect
  concurrently and one poll bounds all of them, so two interpreters' spans
  would land on one row over the same interval and Perfetto would read the
  pair as nested.
- **Retaining intervals and emitting them at shutdown.** Correct, and
  rejected: it buffers without bound on a long session and emits every loss
  span in a lump after every GC record. A poll knows both its edges.
- **Emitting the track from a Perfetto-only finalization hook**, as ADR-0011
  does for process lifetimes. Rejected: it would make loss Perfetto-only,
  breaking ADR-0007's single converter and leaving `combine` unable to rebuild
  spans from a JSONL capture.
- **A flag to disable loss detection.** Rejected: a reader who does not know
  what fraction of the records a capture holds cannot interpret any other
  number in it.

## Implementation

- `src/gcmon/model/loss.py` holds the arithmetic, one accumulator per
  `(pid, iid, gen)`.
- `src/gcmon/model/data.py` holds the loss record.
- `src/gcmon/monitoring/monitor.py` keeps each pid's poll instant beside its
  rings, so dropping a pid drops both and a reused pid inherits no interval.
- `src/gcmon/exporters/trace_converter.py` takes loss through the shared
  pipeline as its third record type and restores span order for a capture read
  back from JSONL. `src/gcmon/exporters/perfetto_format.py` and
  `src/gcmon/exporters/perfetto_builders.py` write the track and the
  generation groups.
- `src/gcmon/stats/streaming_stats.py` records every gap.
- `tests/test_loss.py` and `tests/test_loss_replay.py` check the arithmetic
  against synthetic sessions and a real capture replayed behind a simulated
  ring. `tests/analysis/test_combine_loss_round_trip.py` resolves the loss row
  as a stack, live and through `combine`.
  `tests/exporters/test_perfetto_loss_track.py`, marked `fuzz`, settles the
  track layout against the real trace processor per
  [ADR-0014](0014-perfetto-integration-test-strategy.md).
