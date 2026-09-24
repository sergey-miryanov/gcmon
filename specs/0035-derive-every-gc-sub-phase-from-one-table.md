# 0035: Derive every GC sub-phase from one table

- **Status:** Not started
- **Kind:** feature (cleanup)
- **Effort:** L
- **Origin:** code structure review of `src/gcmon`, 2026-08-15. Rewritten
  2026-09-23, after `model/names.py` landed part of the table and the new
  incremental collector's fields were added by hand.
- **Respects:**
  - [ADR-0003](../docs/adr/0003-gc-metrics-group-track.md): the `GC Metrics`
    group holds an interpreter's per-generation counters. This work amends the
    clause stating their order: it moves from `_COUNTER_ORDER` to a row's
    position in the series table.
  - [ADR-0004](../docs/adr/0004-toplevel-shared-counters.md): a counter an
    interpreter owns may be drawn a level up, beside its own rows. This work
    amends the clause naming which ones: `_TOPLEVEL_COUNTER_METRICS` becomes a
    row's `scope`.
  - [ADR-0005](../docs/adr/0005-counter-y-axis-share-key.md): one metric
    shares a y axis across generations, keyed on the metric name. Unchanged;
    `y_axis_share_key` stays the metric.
  - [ADR-0007](../docs/adr/0007-shared-trace-converter-pipeline.md): one
    conversion pipeline. This extends its decision to the three consumers it
    did not reach, and amends its Context and Consequences to say the phase
    list has one home for every consumer rather than for the two backends.
  - [ADR-0009](../docs/adr/0009-nanoseconds-canonical-time-unit.md):
    nanoseconds internally. Every endpoint the normalizer shifts is
    nanoseconds; `duration` is the one field that is not, which is why it
    cannot be annotated until the encoder carries a double.
  - [ADR-0014](../docs/adr/0014-perfetto-integration-test-strategy.md): assert
    a trace through the trace processor. Section 5 keeps that seam rather than
    reading the table back.
  - [ADR-0026](../docs/adr/0026-two-subsystems-over-a-shared-base.md):
    `exporters` and `stats` may not import each other. Both read these tables,
    so the tables live in the base they share, which is what puts drawing
    order in `model/`.
  - [ADR-0027](../docs/adr/0027-group-every-row-an-interpreter-owns.md): every
    row an interpreter owns sits under its group. This work amends the clause
    ranking those rows, since a series' `scope` now decides its parent.

## 1. Problem statement

Nothing an operator sees is wrong today. This is the largest single
maintenance cost in the codebase. An attempt at the new incremental
collector's seven fields edited six modules in six shapes, and shipped
disagreeing with itself: `docs/formats.md` described a field named `next_gen`
that the code does not carry, and the code carried `heap_size_stop`, put it on
a pause slice and drew it as a counter track, which no page mentioned. Nothing
failed.

CPython's collector has eight optional sub-phases, and gcmon writes that list
out by hand in six modules. Adding the ninth means six edits, and a miss costs
a different thing in each: a miss in the converter costs a slice in the trace;
a miss in the stats table costs a `--stats` row; a miss in the normalizer
leaves one timestamp unshifted in a `combine --normalize` run, which reads as
a slice at the wrong place on the timeline rather than as an error.

Two guards turn a partial build into a `TypeError` rather than a missing
slice. `has_mark_alive` probes the scalar `alive_size`, and
`convert_item_to_trace_format` then subtracts `ts_mark_alive_start` from
`ts_mark_alive_stop`, so a build reporting the size without the timestamps
raises inside the conversion. `has_incremental` has the same shape against
`increment_size`, and the attempt above redefined it to probe a timestamp
instead.

ADR-0007 already made this argument once, for the two trace backends, and won
it. The same duplication survives across the other three consumers.

## 2. Solution

For an operator the cleanup changes nothing: the same slices, the same
`--stats` rows, the same capture bytes.

On top of it, a build running the new incremental collector reports the
collector's state on every pause, readable on a pause slice and in a capture:
`old_work`, `auto_collect`, `survivor_count`, `aging_threshold`,
`aging_spaces`, `aging_next` and `heap_size_stop`. None of them draws a
counter track.

What changes for a maintainer is the shape of the edit. A phase is one row, a
counter is one row, and a field that only wants to be readable is one line in
`model/data.py`.

## 3. User stories

1. As a maintainer tracking a new CPython GC sub-phase, I want to declare it
   once, so that a trace, a `--stats` table and a JSONL line cannot disagree
   about whether gcmon supports it.
2. As a maintainer adding a field that is neither a phase nor a counter, I
   want to declare it in `model/data.py` and nowhere else, so that making a
   figure readable costs one line.
3. As a maintainer, I want a misspelled field name to fail a test rather than
   silently produce a phase that never appears, so that the failure arrives in
   CI and not in a capture.
4. As a maintainer, I want a field the code carries and no page describes to
   fail a test, so that a capture cannot hold a figure nobody can look up.
5. As a maintainer running a build that reports a phase's scalar without its
   timestamps, I want no slice for that phase, so that the conversion does not
   raise inside a live run.
6. As a maintainer reading `trace_converter`, I want the nine near-identical
   blocks to be one loop, so that the one thing that differs between phases is
   visible instead of buried.
7. As an operator comparing `--stats` against a Perfetto trace of the same
   run, I want the two to cover exactly the same set of phases, so that a row
   missing from one is a real finding and not a gap in gcmon.
8. As an operator running `gcmon combine --normalize` over JSONL, I want every
   timestamp on a record shifted, so that no phase slice lands at an absolute
   timestamp beside rebased siblings.
9. As an operator on a build with the new incremental collector, I want the
   collector's state on the pause I am looking at, so that a long pause can be
   read against the work the collector was carrying.
10. As an operator on a stock build, I want a trace and a capture identical to
    what gcmon writes today, so that this work is invisible to me.
11. As a gcmon maintainer reviewing a sub-phase patch, I want the diff to be
    one table row plus its test, so that review is about whether the phase is
    right rather than whether six edits match.
12. As someone reading the codebase for the first time, I want one place that
    answers "what does a record carry, and where does each field go", so that
    I do not have to reconcile six partial lists.
13. As a maintainer, I want the conversion benchmark to hold, so that
    collapsing the cascades does not make a large capture slower to convert.

## 4. Implementation decisions

**Where the list is written out.** Each module holds its own copy, in its own
shape:

| Module | Shape today |
|---|---|
| `model/names.py` | `GC_PHASES`, holding each phase's label, category and per-generation names, and `GEN_COUNTER_METRICS` |
| `model/protocol.py` | eight per-phase `Protocol` classes, nine `has_*` TypeGuards, and `to_mapping`'s nine `if has_*` blocks |
| `stats/metrics.py` | nine `Metric` classes and `METRICS`, each a name and a two-field getter |
| `exporters/trace_converter.py` | `convert_item_to_trace_format`: nine near-identical `Slice` blocks, and two counter lists |
| `exporters/perfetto_format.py` | `_COUNTER_ORDER` and `_TOPLEVEL_COUNTER_METRICS` |
| `analysis/jsonl_io.py` | `normalize_jsonl_timestamps`: eight `if has_*` subtraction blocks |

`model/data.py` is the seventh and stays as it is: it is the msgspec decode
target `read_jsonl` converts into, and section 4.4 makes it the field
declaration every consumer reads rather than a seventh copy of the list.

**4.1: One phase table, in `model/names.py`.** A row per phase, ordered as the
collector runs them, extending the `Phase` that module already holds:

```python
class Phase(NamedTuple):
    label: str  # slice name, and the --stats row
    category: str  # "gc.mark.alive"
    start: str  # may name ANOTHER row's stop field
    stop: str
    stats_key: str  # "mark_alive"
    args: tuple[str, ...]  # record fields carried onto this slice
    slice_names: Mapping[int, str]  # both, rendered per generation
    categories: Mapping[int, str]
```

`start` may name a field another row owns. Three phases begin where the one
before them stopped: finalize-garbage at the handle-weakrefs stop,
handle-resurrected at the finalize-garbage stop, clear-weakrefs at the
handle-resurrected stop.

The pause is a row like the others, with `start` and `stop` its mandatory
timestamps. `stats_output` keys its exact-versus-estimated column off the
pause's `stats_key`, as it keys off `PAUSE_KEY` today.

`slice_names` and `categories` stay pre-rendered per generation, as they are
now: a record emits up to nine slices, and formatting a name per slice is what
that spelling was introduced to stop.

**4.2: A phase is present when both fields it reads are present, so there is
no `present` column.** Every `has_*` guard probes a proxy field, and the three
cascade phases take two guards each. Checking `getattr(item, phase.start) is
not None and getattr(item, phase.stop) is not None` is equivalent to every one
of the nine guards, including the three that `and` two guards together, and it
fixes the `TypeError` in section 1: a build reporting `alive_size` without its
timestamps draws no Mark Alive slice instead of raising.

**4.3: One series table, beside it.** Which fields draw a counter track, and
where:

```python
class Series(NamedTuple):
    metric: str  # the field name; also the y-axis share key
    scope: Scope  # GENERATION | INTERPRETER
    omit_zero: bool = False
```

The rows: `collected`, `uncollectable` (`omit_zero=True`), `candidates`,
`duration` at `GENERATION` scope, and `heap_size` at `INTERPRETER`. **The
table's order is the drawing order.** `_COUNTER_ORDER` and
`_TOPLEVEL_COUNTER_METRICS` both go, and `perfetto_format` keeps a
scope-to-parent mapping holding no field names. `_INTERPRETER_ROW_ORDER`
stays: it ranks rows, not fields.

`omit_zero` carries the one existing exception, that a run collecting
everything draws no `uncollectable` row rather than a row of zeros. It is a
column and not a global rule because `0` is a reading for the other four: a
pause that collected nothing wants `collected` on its row, flat, and not a gap
in the series.

`rss` is not in the table: no record field produces it, and a `ProcessTrack`
owns it.

**4.4: A record's fields are walked, not listed.** `to_mapping` walks
`GCStatsInfo.__struct_fields__` and writes every field that is not `None`,
replacing nine `if has_*` blocks. Two consequences, both wanted: a build
reporting a phase's scalar without its timestamps no longer writes
`"ts_fill_increment_start": null` into the capture, and adding a field to
`model/data.py` puts it in a capture with no other edit.

`dir(item)` is rejected: it sorts alphabetically, so every key in every
capture moves; it returns properties, and on `GenLoss` that is `no_loss`, a
derived boolean with no field to read it back into; and it walks the type's
whole attribute list once per record rather than the field names.

**A capture stays byte-identical**, which needs four lines of `model/data.py`
reordered: `increment_size` and `alive_size` move beside their own timestamps,
so declaration order matches the order `to_mapping` writes today. That is the
only divergence across all 28 fields. `tests/helpers.py` is the one
construction site and passes every field by keyword, so the reorder is safe.

**4.5: What a span carries.** The pause span carries every field the record
has, less the fields some phase uses as an endpoint. Those are what the
timeline already draws, and a reader gets them by clicking the phase. The
excluded set is the union of the table's `start` and `stop` columns, so it is
derived and not a list. Each phase span carries what its own `args` column
names, as today.

Two dictionaries carry what the walk cannot derive:

```python
_ANNOTATION_NAMES: Mapping[str, str] = {GEN: GENERATION}
_DROPPED_AT_GEN: Mapping[str, frozenset[int]] = {
    INCREMENT_SIZE: frozenset({2}),  # today: annotated for gen < 2
    ALIVE_SIZE: frozenset({0}),  # today: annotated for gen > 0
}
```

The first keeps the annotation spelled `generation`, which is what a trace
carries today. The second keeps the two generation restrictions the converter
applies inline today. The collector reports `0` there because the phase does
not run, and a reading of `0` is worse than no reading. Opt-in, so a field not
named draws at every generation.

With both, the pause span carries today's annotations in the record's
declaration order, which moves `heap_size` ahead of `collections` and
`clear_weakrefs_count` ahead of `deleted_garbage_count`. No field order keeps
both a capture and a trace byte-identical, and a trace processor reads an arg
by key, so the trace is the one that gives way.

**4.6: What goes away.** The eight per-phase `Protocol` classes and the nine
`has_*` guards, including `has_pause_ts`: nothing narrows to `TMarkAliveInfo`
or its siblings once the table drives every consumer, and the walk runs only
on an item `is_gc_stats` has already claimed. `is_gc_stats`, `is_instant` and
`is_loss` stay: they discriminate record *kinds*, and the loss work rests on
them.

`stats/metrics.py` is retired. `METRICS` becomes one comprehension over the
phase table, so `stats_output` and `streaming_stats` read `GC_PHASES`
directly. Spec 0039 landed that module in 2026-08-21 as "a module named for
the table this spec derives".

That rename reaches further than two imports. The stats layer spells a phase a
"metric" throughout, in `stats.metrics`, `ring_metrics()`, `metric_key` and
`TStatsData`, and after this a metric is a counter series and nothing else
(`CONTEXT.md`). The sweep is mechanical and belongs in the same change as the
`CONTEXT.md` entry.

**4.7: The new collector's seven fields.** Seven lines in `model/data.py`,
each `int | None = None`, and seven rows in `docs/formats.md`. No `Phase` row,
no `Series` row: none of them is an interval, and none draws a counter track.
Each reaches a trace as an annotation on the pause span and a capture as a
field, both through sections 4.4 and 4.5 with no further edit.

| Field | What it reports |
|---|---|
| `old_work` | Work the collector carries into the next increment |
| `auto_collect` | Whether the collector ran itself rather than being called; `1` for automatic |
| `survivor_count` | Objects that survived this run |
| `aging_threshold` | Threshold an object ages past before promotion |
| `aging_spaces` | Aging spaces the collector keeps |
| `aging_next` | Aging space the next run will use |
| `heap_size_stop` | The heap at the end of the pause, against `heap_size` at its start |

`next_gen` is not among them: it is a field `docs/formats.md` described on the
attempt above and no build reports. `auto_collect` is what that build carries.

`heap_size_stop` draws no counter. Beside `heap_size` it would be a second row
for one quantity, and at `INTERPRETER` scope no `y_axis_share_key` is set, so
the two would not share a scale. On the pause span, beside `heap_size`, it
says how much the heap moved across the collection.

**Accepted cost: the cascades are statically checked and a table indexed by
field-name strings is not.** mypy and pyrefly verify today that
`item.ts_mark_alive_start` exists on the type a guard narrowed to;
`getattr(item, phase.start)` verifies nothing. Three tests convert a typo from
a silently absent phase into a failure, named in section 5.

**Rule 8, on deriving a fact once rather than twice with a test between.**
Three of the four tests in section 5 keep the tables agreeing with
`GCStatsInfo`, and the fact cannot be derived once. Generating the tables from
the struct needs the phase pairing, which four of the eight phases do not
follow by name; generating the struct from the tables needs a third list of
the fields that draw nothing, which is most of them. What *is* derived once:
`JSONL_FIELDS` stops being a hand-written list and becomes
`GCStatsInfo.__struct_fields__`, which deletes a copy rather than adding a
test.

**Rejected: generate the phase table from `GCStatsInfo` by field-name
convention**, pairing anything matching `ts_<name>_start` with
`ts_<name>_stop`. Four of the eight phases do not follow the convention,
including all three that begin at a predecessor's stop, so the generator needs
a table of exceptions, which is the table, plus a generator. Convention is
used as an *assertion* instead, in the second test below.

**Rejected: keep the guards and share only the naming strings.** That is what
ADR-0007 rejected when it merged the two trace backends: "the guards were the
small part."

**Rejected: every field on every span.** It takes a full record from 34
annotations to 252. Encoding a record costs what its annotations cost, and the
encoder is where a live run spends its time, so a fast-collecting target
outruns the monitor.

**Rejected: making a trace a complete carrier of every record**, by annotating
the phase endpoints too. It more than doubles a pause span's annotations to
duplicate what a capture is for: a trace holds events drawn for a viewer, a
capture holds what they were made from (`CONTEXT.md`). A consumer needing
every field reads a capture.

## 5. Seams and testing decisions

- **Seam:** `test_full_gen1_sub_slices_present` in
  `tests/cli/analyze/test_convert_cmd_perfetto.py`, through the trace
  processor. It is the highest seam available: it queries the eight sub-phase
  slice names for one generation, so it observes the phases as *slices in a
  trace*, which is what the table exists to produce, rather than observing the
  table. `tests/stats/test_metrics.py` and the capture round-trip cover the
  two consumers the trace processor cannot see.
- **New seam needed:** none for behaviour. One new test file for the four
  completeness assertions below, which read `model/data.py` and the tables and
  no output.
- **What makes a good test here:** assert the trace still *means* the same
  thing: that a record carrying all eight sub-phases produces the same eight
  named slices, at the same timestamps, on the same track. A test that reads
  the phase list out of the table and checks the converter emitted that list
  proves only that the loop ran; it would pass with every row wrong. Pin the
  expected slice names and categories as literals, since they are the public
  surface an operator queries in PerfettoSQL.
- **Prior art:** `tests/exporters/test_perfetto_format.py` for per-slice
  assertions, and `TestTheCountersInsideAMetricsGroupAreRanked` there, which
  names each metric rather than reading the order back, the shape the
  completeness tests copy; `test_every_slice_matches_the_events_behind_it` for
  the equivalence shape; `tests/exporters/test_track_names_are_documented.py`
  for reading a page.
- **Cases:**
  1. A record carrying every sub-phase produces the identical `TraceEvent`
     list before and after: same order, same names, same categories, same
     args.
  2. A record carrying none of them produces only the `GC Pause` span and the
     counters.
  3. A record carrying a phase's scalar and not its timestamps produces no
     slice for that phase, and does not raise.
  4. The generation restrictions hold: `increment_size` is annotated for gen 0
     and 1 and not gen 2; `alive_size` for gen 1 and 2 and not gen 0.
  5. Completeness, one test each: every field name in every `Phase` row exists
     on `GCStatsInfo`; every `ts_`-prefixed field on `GCStatsInfo` appears in
     the union of the `start` and `stop` columns; every `Series.metric` exists
     on `GCStatsInfo`.
  6. `tests/exporters/test_track_names_are_documented.py` reads
     `GCStatsInfo.__struct_fields__` in place of `JSONL_FIELDS`, so a field
     the code carries and `docs/formats.md` does not name fails. It catches
     both halves of what the attempt in section 1 shipped.
  7. Regression guard: `--stats` byte-identical, a capture byte-identical for
     a fixed record set, `tests/fixtures/monitored_run_perfetto_trace.txt`
     differing only in the annotation order 4.5 names, and
     `tests/benchmarks/test_bench_trace_conversion.py` within its budget.
     Conversion should get cheaper: one probe per field replaces sixteen guard
     calls on a full record, each a `getattr`.

## 6. Out of scope

- **Any change to slice names, categories, arg keys or the JSONL schema**,
  beyond the seven fields in 4.7 and the `duration` annotation in section 7.
  What proves the cleanup broke nothing is that its one output diff is the
  annotation order in 4.5.
- **Dropping loss records from a capture.** All four `GenLoss` numbers
  reconstruct from consecutive GC records, since `duration` is cumulative
  (`RingAccumulator._gen_loss`), but the window's bounds are two poll instants
  no record carries, and three consecutive polls that each lose records
  collapse into one reconstructed gap. That is a format change amending
  [ADR-0015](../docs/adr/0015-gc-loss-spans-on-their-own-track.md), with its
  own spec.
- **Counter tracks for any of the seven fields.** Drawing them is a judgement
  about what an operator wants plotted, not about the table. Adding one later
  is a row.
- **The loss record's own fields.** `GenLoss` and `LossMsg` are one shape each
  with no optional variants (ADR-0015); there is nothing to tabulate. Spec
  0033 would add loss-derived series; the third completeness test is scoped to
  GC records and does not block it.
- **The `--stats` table layout** in `stats_output`. It consumes the phase
  table; it does not care where the table came from.
- **Adding any sub-phase CPython does not report today.**

## 7. Further notes

**Two changes, in order.** The table lands first and changes no output. The
fields land second.

| | First: the table | Second: the fields |
|---|---|---|
| code | both tables, the phase loop, the `to_mapping` walk, the normalizer's endpoint union, the four-line `data.py` reorder, the guards and `Protocol` classes deleted, `stats/metrics.py` retired and the stats layer's "metric" swept | seven `data.py` lines, the `DOUBLE_VALUE` annotation arm, `EventArgs` widened to admit `float`, `duration` annotated on the pause span |
| docs | `sub-step` → `sub-phase` in `docs/formats.md` and `docs/perfetto-sql.md`, three `CONTEXT.md` entries, ADR-0007 and ADR-0003/0004/0027 amended, spec 0062's prior-art line repointed | seven `docs/formats.md` rows, one CHANGELOG line |
| proof | `--stats` and capture byte-identical, the trace differing only in the annotation order of 4.5; the four completeness tests | the documented-names test passes only once the rows exist |

The second change is seven declarations and one encoder gap. `duration` is the
one `float` on a record, `_build_debug_annotation` has no double arm, and a
float falling through to the int encoder raises
`TypeError: unsupported operand type(s) for &: 'float' and 'int'`. None of the
seven fields needs that arm: `auto_collect` is an `int`. If anything else in
the second change reaches outside `model/data.py` and `docs/formats.md`, the
table is wrong and the first change is not finished.

**Vocabulary this settles**, in `CONTEXT.md`: a **phase** is one named
interval of a collection, drawn as a slice and totalled as a `--stats` row.
The whole pause is one, the eight inside it are **sub-phases**, and `sub-step`
is not a spelling gcmon uses. A **metric** is a figure drawn as a counter
series, and after 4.6 the word means nothing else. A **field** is one
attribute a record carries, spelled the same as a JSONL key and a span
annotation.

**On convention rule 10.** This spec amends four records and its section 4.1
is the entry the rest rests on, which is the shape rule 10 says to take in an
ADR first. The amendments are taken in the ADRs themselves, in the first
change above, alongside the table they describe rather than ahead of it. The
argument for the trade, one place to edit bought with the static checking of
field access, lives here and is not a record of its own. If that turns out to
be the wrong call, the ADR to write is the one ADR-0003, ADR-0004 and ADR-0027
would each point at.
