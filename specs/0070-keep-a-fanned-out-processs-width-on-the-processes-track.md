# 0070: Keep a fanned-out process's width on the `Processes` track

- **Status:** Not started
- **Kind:** feature (enhancement)
- **Effort:** S
- **Origin:** the "snapping near-equal starts" alternative in ADR-0011, which
  the record lists as not adopted
- **Respects:**
  - [ADR-0011](../docs/adr/0011-process-lifetime-and-ordering.md): the laminar
    clip, the longer-first sort that keeps it safe, the adjacent BEGIN/END
    pair, and two clauses this work has to keep true: a span never covers a
    stretch in which its process did not exist, and the drawn duration is a
    lower bound. It amends the "snapping near-equal starts" alternative, which
    becomes part of the Decision.
  - [ADR-0029](../docs/adr/0029-report-liveness-and-fold-it-into-the-span.md):
    what an observation is. Children first seen in one tick already share a
    start and nest; this covers the ones whose first GC event predates that
    poll.
  - [ADR-0028](../docs/adr/0028-draw-every-process-a-row-of-its-own.md): the
    `Lifetime` slice on a process's own row draws the observed pair, and stays
    as it is.
  - [ADR-0014](../docs/adr/0014-perfetto-integration-test-strategy.md): the
    claim is about what the trace processor pairs up, so the trace processor
    is what settles it.

## 1. Problem statement

Someone opens a trace of a fork loop and looks at the `Processes` track to see
how the workers overlapped. Each worker ran for seconds. All but the last are
drawn a few microseconds wide, stacked at the left edge, and the track reads
as one long process and a burst of failures.

The clip is what does it. Two spans that cross cannot share a track, so the
earlier one ends one nanosecond before the later one starts, and siblings
forked in a loop start microseconds apart. The width a worker loses is the
whole of its life past its next sibling's start. `real_start_ts` and
`real_end_ts` still hold what gcmon observed, and so does the worker's own
row, but the shared track is the one place meant to show the workers beside
each other.

## 2. Solution

Workers that started together are drawn as starting together. They nest,
longest outermost, each as wide as it was observed to be, give or take the few
microseconds their starts differed by. Staggered starts still clip as they do
today, and nothing a query reads from `real_start_ts`, `real_end_ts` or the
`Lifetime` slice changes.

## 3. User stories

1. As someone reading a trace of a worker pool, I want workers forked in one
   loop to keep their widths on the `Processes` track, so that I can see which
   of them outlived which.
2. As someone reading a trace, I want a drawn slice never to be wider than the
   process was observed for, so that a wide slice still means a long life.
3. As someone querying a trace, I want `clipped`, `real_start_ts` and
   `real_end_ts` to keep their meaning, so that the documented queries in
   `docs/perfetto-sql.md` return what they return today.
4. As an operator monitoring one process, I want a trace byte-identical to
   today's, so that this change costs me nothing.
5. As someone opening a trace of a very wide fan-out, I want every slice to
   close, so that a change made for readability does not leave `dur = -1`
   behind the trace processor's nesting limit.

## 4. Implementation decisions

**The sweep snaps before it clips.** `_clip_spans_to_laminar` sorts by start,
then groups spans whose starts lie within ε of the group's first, and draws
every span in a group from one start. Spans sharing a start nest, which the
longer-first sort already orders, so the clip finds nothing to cut inside a
group.

**A group is drawn from its latest start, never its earliest.** Moving a start
later keeps the drawn span inside the observed one, so ADR-0011's two clauses
hold: the span covers no stretch the process did not exist for, and the drawn
duration stays a lower bound. Snapping to the earliest start breaks both by up
to ε.

**A span that ends before the group's start is left out of the group.** A
start moved past its own end would invert the pair, and a process that short
is the child a reader is looking for. It keeps its start and clips as it does
today.

**`ClippedSpan` carries the move as it carries a clip.** The drawn pair moves
and the observed pair is carried through untouched. `clipped` widens with it:
true where either drawn end differs from the observed one. It compares the
ends alone today, which is enough while only an end can move.

**A group stops at the nesting limit.** The trace processor closes at most 512
nested slices and loses the rest in silence (ADR-0011). A group takes spans
until the depth they would nest to reaches that limit, and the spans past it
clip as they do today.

**ε is a constant, and its value is not chosen here.** What settles it is the
gap between sibling starts in a real fan-out capture: ε has to exceed that
jitter and stay below any gap a reader would take for a real ordering. One
capture of a `multiprocessing` pool and one of a fork loop, reading
`real_start_ts` off the `Processes` slices, gives both numbers. Rule 10 of
[CONVENTIONS.md](CONVENTIONS.md) applies once it is known: the clause goes
into ADR-0011 as `Accepted, unbuilt (spec 0070)` before the code does.

Rejected: a flag for ε. There is one value anyone would set, and a second one
would ship two definitions of a `Processes` slice, the ground ADR-0029 gives
for keeping liveness always on.

## 5. Seams and testing decisions

- **Seam:** the trace processor's `slice` table for the `Processes` track,
  which is the only place that says whether the pairs gcmon wrote were ones it
  could pair up.
- **New seam needed:** none. `_clip_spans_to_laminar` is already tested
  directly as a pure function, and that covers the grouping arithmetic.
- **What makes a good test here:** durations read back through the trace
  processor, with `misplaced_end_event` at 0. A unit test on the sweep alone
  passes on an emission order the reader cannot pair.
- **Prior art:** the direct sweep tests and the closeout tests in
  `tests/exporters/test_perfetto_process_lifetime.py`, and the randomized
  differential test in `tests/exporters/test_perfetto_emission_order_fuzz.py`.
- **Cases:**
  1. Siblings whose starts lie within ε read back nested, each `dur` within ε
     of its observed duration, and `misplaced_end_event` stays 0.
  2. Siblings whose starts lie further apart than ε clip as they do today.
  3. A span that ends before its group's start keeps its own start.
  4. No drawn span is wider than its observed pair, over the fuzz suite's
     random spans.
  5. A group wider than the nesting limit leaves no slice with `dur = -1`.
  6. A run with one process, and one whose spans never cross, produce the
     bytes they produce today.

## 6. Out of scope

- Bounding nesting depth for spans that nest without any snapping, such as
  processes all alive when the loop stops. ADR-0011 accepts it, and this
  changes nothing there.
- The vertical space deep nesting costs in the UI. It is the price of showing
  the widths.
- The `Lifetime` slice on a process's own row. It draws the observed pair and
  nothing on that row crosses it (ADR-0028).
- Snapping ends. A shared end already nests, and ADR-0029 gives processes
  alive in one tick the same end.
