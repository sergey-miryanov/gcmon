# 0071: Draw every process's span at its observed width on the `Processes` row

- **Status:** Not started
- **Kind:** feature (enhancement)
- **Effort:** M
- **Origin:** the "one lifetime track per pid" alternative in ADR-0011, and
  Perfetto's documented answer to overlapping slices: a track per span, merged
  by name
  ([converting.md, v58.2](https://github.com/google/perfetto/blob/v58.2/docs/getting-started/converting.md#asynchronous-slices-and-overlapping-events))
- **Respects:**
  - [ADR-0011](../docs/adr/0011-process-lifetime-and-ordering.md): this work
    amends it. The single shared track, the laminar clip, the emission-order
    rules, the `real_*` and `clipped` annotations and the whole-track-at-close
    clause go; "one lifetime track per pid" moves from Alternatives into the
    Decision. The slice name, `pid`, `pid_epoch`, "no span is dropped", the
    root descriptor and the ranking stay as they are.
  - [ADR-0002](../docs/adr/0002-perfetto-track-uuid-and-hierarchy.md): UUIDs
    come off the counter, keyed by identity. A `Process` gains a second UUID,
    for its track on the shared row. Root parentage stays "`parent_uuid`
    absent".
  - [ADR-0028](../docs/adr/0028-draw-every-process-a-row-of-its-own.md): the
    `Lifetime` slice draws the observed pair and is untouched. Three clauses
    are amended: "the shared row draws the clipped one", the reason the
    `Processes` slice of a retired process waits for close, and what a killed
    run keeps.
  - [ADR-0029](../docs/adr/0029-report-liveness-and-fold-it-into-the-span.md):
    what an observation is. Its remark that a shared end nests instead of
    clipping loses its subject.
  - [ADR-0013](../docs/adr/0013-rss-sampling.md): one instant per sampling
    pass. The decision stands on "the spread carries no information"; the
    sentence about which sibling is clipped goes.
  - [ADR-0015](../docs/adr/0015-gc-loss-spans-on-their-own-track.md): cites
    ADR-0011's clipping sweep twice as the thing the `GC Loss` track does not
    need. Both citations are reworded, and nothing it decides moves.
  - [ADR-0010](../docs/adr/0010-process-identity-cmdline-and-start-marker.md):
    a process's row and its `Lifetime` slice. Neither moves; the killed-run
    argument this work extends is ADR-0028's.
  - [ADR-0014](../docs/adr/0014-perfetto-integration-test-strategy.md): the
    claim is about what the trace processor pairs up and what the UI merges,
    so the trace processor settles it.

## 1. Problem statement

Someone opens a trace of a process tree and reads the `Processes` row to see
which processes ran beside which. Two processes whose lifetimes cross cannot
both be drawn as observed: the earlier one is cut one nanosecond before the
later one starts. A worker pool forked in one loop comes out as one long slice
and a stack of slivers at the left edge, each of them a worker that ran for
seconds. The width a process lost is recoverable from `real_start_ts` and
`real_end_ts` in the Args panel and from the process's own row, but the shared
row is the one place meant to show lifetimes beside each other, and it is the
one place that draws them wrong.

Someone querying the trace has the same trap in another form: `dur` on a
`Processes` slice is not a duration gcmon observed, and every query has to
know to subtract two annotations.

## 2. Solution

Every span on the `Processes` row is as wide as its process was observed for.
Spans that overlap sit in lanes of one row, the way the Perfetto UI draws
async slices, and the row is as tall as the largest number of processes alive
at once. A lane says nothing about parentage.

`ts` and `dur` on a `Processes` slice are the observed pair, so the
`real_start_ts`, `real_end_ts` and `clipped` annotations are gone. A trace
from a run that was killed still holds the span of every process gcmon had
already let go of.

## 3. User stories

1. As someone reading a trace of a worker pool, I want every worker drawn as
   wide as it ran, so that I can see which outlived which.
2. As someone reading a trace, I want one `Processes` row however many
   processes the run had, so that the timeline stays the length it is today.
3. As someone querying a trace, I want `dur` on a `Processes` slice to be the
   observed duration, so that a query needs no annotation arithmetic.
4. As someone querying a trace, I want `pid`, `pid_epoch` and `cmdline` on
   every span as they are today, so that a span still says which process it
   is.
5. As an operator whose run was killed, I want the spans of the processes that
   had already exited to be in the file, so that the shared row is not empty
   for the run I most need to read.
6. As someone reading a trace with `Processes` spans, I want no
   `misplaced_end_event` and no slice with `dur = -1` at any process count, so
   that nothing about the row needs a caveat.
7. As someone reading JSONL or `--stats`, I want nothing to change.

## 4. Implementation decisions

**A track per process, every one of them named `Processes`.** Each carries the
name, no `parent_uuid`, and nothing else, which is the descriptor the shared
track has today. The trace processor merges root-level tracks that share a
name, and the UI draws the merged set as one row with
`experimental_slice_layout` picking the lanes. `PerfettoTrackState` maps a
`Process` to the UUID of its track on the shared row, beside the map it keeps
for the process's own track, and ADR-0002's list of identities gains the
entry. A `Process` is one holder of a pid, so a reused pid gets a track per
holder with no further rule.

**The default merge, with no `sibling_merge_behavior` and no
`sibling_merge_key`.** Rejected: a shared key. It says what the name already
says, and it costs the hand-written encoder two fields whose only test is that
the merge still happens.

**The drawn pair is the observed pair.** `_clip_spans_to_laminar` and
`ClippedSpan` go, and both slice emitters take the `ProcessSpan` the state
already returns. The `REAL_START_TS`, `REAL_END_TS` and `CLIPPED` annotations
go with them: each would restate `ts`, `ts + dur` or `false` on every slice
(rule 8). `pid`, `pid_epoch` and `cmdline` stay. `clipped` is the only bool
gcmon writes, so the builder that writes one and the field number it uses go
too.

**The pair stays adjacent and BEGIN-first, and the END drops its name.** A
zero-length span written END-first still reads as `dur = -1`. The name on the
END existed to keep two spans on one pid from closing each other on a shared
track; each now has a track of its own, and the `Lifetime` END is already
unnamed.

**A retired process's span goes out with its row.** `emit_retired_process_row`
writes the process's `Processes` descriptor and pair beside its `Lifetime`
bar. ADR-0011 holds the whole track to close because the sweep needed every
span in hand and because a live process's END moves with each flush. Neither
applies to a retired process: no span is measured against another, and its own
is final, which is the ground that function already stands on. One flag,
`has_process_row_drawn`, guards both slices, since they are always written
together; the `_process_lifetime_emitted` flag keeps its one meaning, that the
closeout has gone out. Processes still held at close are drawn there, as
today.

**The closeout sorts once.** `finalize_perfetto_packets` orders the remaining
spans by `(start_ts, process)`, the key `rank_processes` sorts by, and ranks
from that list. The sweep's sort was what made the closeout's bytes
independent of arrival order, and nothing else did.

**Identify a span by its name or its annotations, never by `track_id`.** The
trace processor folds the descriptors at import: a span lands on an existing
track of its merge group whose stack is empty, so the `track` table holds as
many `Processes` rows as the peak number of live processes.
`track.name = 'Processes'` selects what it selected before. No slice nests on
any of those rows, so the 512-nested-slice limit cannot be reached from this
track.

**User-facing pages.** `docs/formats.md` and `docs/perfetto-sql.md` lose the
warning against reading `dur`, the `clipped` query, and the `real_*`
arithmetic in the other queries. The CHANGELOG says the spans are drawn at
their observed width and names the three annotations that are gone.

## 5. Seams and testing decisions

- **Seam:** the trace processor's `slice` table joined to
  `track.name = 'Processes'`, plus `_track_event_tracks_ordered_groups` from
  `viz.summary.track_event`, which is the query the UI builds its rows from.
- **New seam needed:** none.
- **What makes a good test here:** durations read back through the trace
  processor and compared with the spans that went in, with
  `misplaced_end_event` at 0. That stat has severity `data_loss`, so a filter
  on `error` alone never sees it.
- **Prior art:** the `order_id` assertions in
  `tests/exporters/perfetto_integration/test_row_contents.py`, the crossing
  trace in that package's `traces.py`, the killed-run trace beside it, and the
  random span generator in the exporters' emission-order fuzz module.
- **Cases:**
  1. The crossing fixture from ADR-0011's Context reads back with every `dur`
     equal to the observed duration and `misplaced_end_event` at 0.
  2. `_track_event_tracks_ordered_groups` holds one root-level row named
     `Processes`, and its `track_ids` cover every `Processes` slice.
  3. Over the fuzz suite's random spans, every slice reads back at its
     observed `ts` and `dur`, and none has `dur = -1`. Zero-length spans and
     spans sharing a timestamp are in the set.
  4. Two holders of one pid read back as two slices, `Process <pid>` and
     `Process <pid>#2`, each at its own width. The pid-reuse integration
     module already asserts this and needs no new case.
  5. A retired process's `Processes` pair is in the packets
     `emit_retired_process_row` returns, and the closeout does not write it
     again. Read through the trace processor as well, on the killed-run trace:
     the row holds the retired process's span and not the running one's.
  6. The closeout's bytes are the same for two arrival orders of the same
     spans.
  7. A `Processes` BEGIN carries `pid`, `pid_epoch` and `cmdline`, and none of
     `real_start_ts`, `real_end_ts`, `clipped`.
  8. JSONL output is byte-identical to today's. The JSONL suite asserts the
     lines already and needs no new case; it passing unchanged is the check.
- **Tests that go:** the direct tests on `_clip_spans_to_laminar`, the
  differential test on emission order, the measurement of the 512 limit, and
  the one pinning a named END against the stack. Each asserts a property of
  one shared track. `test_liveness.py` reads a span from `ts` and `dur` where
  it reads the `real_*` annotations today. One order claim survives, that the
  BEGIN goes first, and it keeps a negative control of its own: a zero-length
  span written END-first reads as `dur = -1`. The fuzz module is named for the
  emission order it fuzzes, so it is renamed with its subject.

## 6. Out of scope

- A lane that means parentage. Laying children out under their parent needs a
  depth computed from the process tree and a UI that draws it, which the stock
  UI does not offer; `experimental_slice_layout` packs by time alone.
- Bounding the row's height. It equals peak concurrency, which is the price of
  showing the widths. A row several hundred lanes tall was opened in the UI
  and draws.
- The `Lifetime` slice, the process rows, their ranks and the root descriptor.
  None of them depended on the clip.
- A `sibling_order_rank` for the `Processes` row. It sorts by name at the top
  level, as it does today.
- JSONL. The `Processes` row is Perfetto-only.

## 7. Further notes

- **Rule 10 sets the order.** ADR-0011 is rewritten first, as
  `Accepted, unbuilt (spec 0071)`, and the code follows. The other five
  records change in the commit that lands the code, since each only cites what
  ADR-0011 decides.
- **Spec 0070 retires in the commit that lands this one**, superseded by it.
  It mitigates the clip, so it stays buildable for as long as the clip is in
  the code. The retirement is the usual three edits: delete the file, cut its
  row and its "Not in the run" bullet from `README.md`, and add its row to
  `RETIRED.md`.
- **Which readers merge.** Perfetto v52.0 moved the merge from the UI into the
  trace processor
  ([CHANGELOG, v52.0](https://github.com/google/perfetto/blob/v58.2/CHANGELOG));
  before it the UI merged by name on its own. ADR-0011 already names 0.57 as
  the oldest reader that orders process rows, so this adds no floor. A reader
  that merges nothing draws a row per process, with every span intact.
