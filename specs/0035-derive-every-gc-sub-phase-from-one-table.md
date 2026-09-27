# 0035: Derive every GC sub-phase from one table

- **Status:** Not started
- **Kind:** feature (cleanup)
- **Effort:** S
- **Origin:** code structure review of `src/gcmon`, 2026-08-15. Rewritten
  2026-09-27, after #189 moved each phase onto one row in `model/phases.py`.
- **Respects:**
  - [ADR-0003](../docs/adr/0003-gc-metrics-group-track.md): the `GC Metrics`
    group holds an interpreter's per-generation counters. This work amends the
    clause stating their order: it moves from `_COUNTER_ORDER` to
    `PauseData.counter_metrics`, and `uncollectable` moves from second to
    last.
  - [ADR-0005](../docs/adr/0005-counter-y-axis-share-key.md): one metric
    shares a y axis across generations, keyed on the metric name. Unchanged;
    `y_axis_share_key` stays the metric.
  - [ADR-0007](../docs/adr/0007-shared-trace-converter-pipeline.md): one
    conversion pipeline. Its Context and Consequences still describe the
    `has_*` guards; this work amends them to say the phase rows are the one
    home for every consumer rather than for the two backends.
  - [ADR-0014](../docs/adr/0014-perfetto-integration-test-strategy.md): assert
    a trace through the trace processor. Section 5 keeps that seam rather than
    reading the tables back.
  - [ADR-0026](../docs/adr/0026-two-subsystems-over-a-shared-base.md):
    `exporters` and `stats` may not import each other. Both read the phase
    rows, so the rows live in `model/`.

## 1. Problem statement

This is maintenance cost, and nothing an operator sees is wrong. Each phase is
one row in `model/phases.py`, and every consumer reads the rows. Three facts
beside them are still written out by hand:

| Fact | Where it is written | Kept agreeing by |
|---|---|---|
| Which counters a pause draws, and their order | `PauseData.counter_metrics` and `PauseData.counters` in `model/phases.py`, `_COUNTER_ORDER` in `exporters/perfetto_format.py` | nothing |
| What a JSONL line carries | `JSONL_FIELDS` in `model/names.py`, which only tests read | nothing |

The two counter lists already disagree on order: `counter_metrics` and
`counters` put `uncollectable` last, `_COUNTER_ORDER` puts it second. The
trace draws it second, because only `_COUNTER_ORDER` decides the ranks.

`JSONL_FIELDS` names `pid`, the loss record's fields, and the five
`GCStatsInfo` fields every record carries; no sub-phase field is in it.
`tests/exporters/test_track_names_are_documented.py` reads it, so a field the
code carries and `docs/formats.md` does not name passes. No module in `src/`
reads it, nor `SLICE_ARGS` beside it: the two lists are test data kept in the
package. An earlier attempt at the new incremental collector's seven fields
shipped that way: the code carried `heap_size_stop` on a pause slice and drew
it as a counter track, and no page mentioned it. Nothing failed.

Nothing checks that a field on `GCStatsInfo` reaches the trace. A field added
to the record that no row's `Info` names and `PauseData.args` leaves out
converts without an error and draws nowhere.

The stats layer spells a phase a "metric" throughout: `stats/metrics.py`,
`METRICS`, `ring_metrics()`, `metric_key` and `TStatsData`. A metric is a
counter series and nothing else (`CONTEXT.md`).

## 2. Solution

For an operator the cleanup changes one thing: `uncollectable` draws last in a
`GC Metrics` group rather than second. The `--stats` table and a capture are
otherwise byte-identical, and so is the trace but for the counter ranks.

What changes for a maintainer is the shape of the edit. A phase is one row, a
counter is a name in `PauseData.counter_metrics` and a line in
`PauseData.counters` that a test holds in the same order, and a field that
only wants to be readable is a line in `model/data.py` plus its key in
`PauseData.args`, which a test holds together.

## 3. User stories

1. As a maintainer adding a counter, I want to declare it once, so that which
   counters a pause draws and the order they draw in cannot disagree.
2. As a maintainer adding a field that is neither a phase nor a counter, I
   want a test to fail until `docs/formats.md` names it, so that a capture
   cannot hold a figure nobody can look up.
3. As a maintainer adding a field to `GCStatsInfo`, I want a test to fail
   until a row or the pause span reads it, so that a figure the collector
   reports does not drop out of the trace.
4. As a maintainer reading the stats layer, I want a phase called a phase, so
   that "metric" means a counter series wherever I meet it.
5. As an operator on a stock build, I want a capture identical to what gcmon
   writes today and a trace drawing the same tracks, so that this work is
   invisible to me.
6. As a maintainer, I want the conversion benchmark to hold, so that the
   cleanup does not make a large capture slower to convert.

## 4. Implementation decisions

**4.1: A sub-phase row's `Info` is readable at runtime**, so the completeness
tests in section 5 case 3 can hold it against `GCStatsInfo`. The conversion
does not read it: `check` keeps probing one field per phase, since the
collector reports a phase's scalar and its timestamps together, and
`_CHECKED_FIELDS` stays the key `sub_phase_rows` builds for every msgspec
record.

Reading `Info` at runtime needs it on the row's protocol. mypy does not count
a nested class as a protocol member, so each sub-phase row declares its fields
as `class _Info(Protocol)`, takes `_Info` in `bounds` and `args`, and exposes
`Info: ClassVar[type] = _Info`. A `SubPhaseRow(PhaseRow, Protocol)` declares
`Info`, and `SUB_PHASE_ROWS` is typed as a tuple of it. `PauseData` is not a
sub-phase row and keeps `type Info = TGCStatsInfo`.

**4.2: The counters draw in `PauseData.counter_metrics` order.**
`perfetto_format` builds `_COUNTER_RANKS` from `PAUSE_ROW.counter_metrics`,
and `_COUNTER_ORDER` goes. `counter_metrics` is `collected`, `candidates`,
`duration`, `uncollectable`, the order `PauseData.counters` emits, so
`uncollectable` draws last in a `GC Metrics` group, where `_COUNTER_ORDER`
puts it second.

`PauseData.counters` stays a hand-written list: it runs on every record, and
walking `counter_metrics` there buys nothing a test cannot hold. A test
asserts it emits its per-generation metrics in `counter_metrics` order.
`_TOPLEVEL_COUNTER_METRICS` stays too: it holds `heap_size` alone, and no
other list names it.

**4.3: The tests build the JSONL field list from the records.** `JSONL_FIELDS`
and `SLICE_ARGS` leave `model/names.py`. The tests that read them,
`tests/exporters/test_track_names_are_documented.py` and
`tests/model/test_names.py`, take a line's fields from the `__struct_fields__`
of `GCStatsInfo`, `LossMsg`, `GenLoss` and `InstantMsg`, and name `pid`, which
the writer adds. `InstantMsg` is in no list today, and `docs/formats.md` names
its `type` but not `name` or `ts`, so the page gains both. `SLICE_ARGS` moves
as it is: its names are annotations the exporters write, not fields of a
record. The move deletes a copy rather than adding a test. The
documented-names test then reads every field a record can carry, so a field
`docs/formats.md` does not name fails it.

`test_every_jsonl_field_is_one_a_written_line_carries` then has to write a
record carrying every sub-phase, and an instant event. `GCStatsInfo` omits a
`None` field from a line, so a stock record leaves the sub-phases out.

**4.4: The stats layer says phase.** `stats/metrics.py` is retired. Its
`PAUSE_KEY` and `METRICS` keys become a `key: ClassVar[str]` on each row, and
`phase_bounds` and `phase_spans` move to `stats/streaming_stats.py`, their one
caller. `METRICS`, `ring_metrics()`, `metric_key`, `TStatsData` and
`StreamingStats.metrics` are renamed for phases in the same change as the
`CONTEXT.md` entry.

**Rule 8, on `PauseData.args` naming fields `GCStatsInfo` also declares.**
Deriving the pause span's args from the struct means walking its fields on
every record, and conversion is on the live path: a named dict literal is what
the benchmark in story 6 holds. The test in section 5 case 3 keeps the two
agreeing instead. Every other list in section 1 is derived or deleted.

**Rejected: generate the phase rows from `GCStatsInfo` by field-name
convention**, pairing anything matching `ts_<name>_start` with
`ts_<name>_stop`. Four of the eight phases do not follow the convention,
including all three that begin at a predecessor's stop, so the generator needs
a table of exceptions, which is the rows, plus a generator.

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

- **Seam:** `convert_item_to_trace_format`, through
  `tests/exporters/test_trace_converter.py`, for the pause span's args.
  `test_full_gen1_sub_slices_present` in
  `tests/cli/analyze/test_convert_cmd_perfetto.py` observes the sub-phases as
  slices in a trace through the trace processor, and
  `tests/fixtures/monitored_run_perfetto_trace.txt` pins the whole trace.
- **New seam needed:** none for behaviour. The completeness assertions in case
  3 join `tests/model/test_phases.py`, which reads `model/data.py` and the
  rows and no output.
- **What makes a good test here:** assert the trace still *means* the same
  thing. A test that reads the counter list out of `counter_metrics` and
  checks the converter drew that list proves only that the loop ran. Pin the
  expected counter names and their ranks as literals, since they are what an
  operator queries in PerfettoSQL.
- **Prior art:** `TestTheCountersInsideAMetricsGroupAreRanked` in
  `tests/exporters/test_perfetto_format.py`, which names each metric rather
  than reading the order back;
  `tests/exporters/test_track_names_are_documented.py` for reading a page.
- **Cases:**
  1. The counters inside a `GC Metrics` group draw `collected`, `candidates`,
     `duration`, `uncollectable`, pinned as literals in
     `TestTheCountersInsideAMetricsGroupAreRanked`, and `heap_size` still sits
     in its interpreter's group. `PauseData.counters` emits its per-generation
     metrics in `counter_metrics` order.
  2. `tests/exporters/test_track_names_are_documented.py` reads the
     `__struct_fields__` of `GCStatsInfo`, `LossMsg`, `GenLoss` and
     `InstantMsg`, so a field the code carries and `docs/formats.md` does not
     name fails, and `model/names.py` holds no `JSONL_FIELDS` or `SLICE_ARGS`.
  3. Completeness, one test each: every `ts_`-prefixed field on `GCStatsInfo`
     but the pause's `ts_start` and `ts_stop` is named by some sub-phase row's
     `Info`; every field on `GCStatsInfo` that is not a timestamp and no
     sub-phase row's `Info` names is a key of the pause span's args.
  4. Regression guard: `--stats` byte-identical for a capture recorded on an
     instrumented build, a capture byte-identical for a fixed record set, the
     monitored-run fixture differing only in the counter ranks, and
     `tests/benchmarks/test_bench_trace_conversion.py` within its budget.

## 6. Out of scope

- **Any change to slice names, categories, arg keys or the JSONL schema.**
  What proves the cleanup broke nothing is that its output diff is the counter
  order in 4.2 and nothing else.
- **Moving the `Phase` constants out of `model/names.py`.** Each row reads its
  `Phase` from there, and `GC_PHASES` is what the documented-names test reads
  for the slice labels. The rows would hold the same strings under another
  import.
- **Dropping loss records from a capture.** All four `GenLoss` numbers
  reconstruct from consecutive GC records, since `duration` is cumulative
  (`RingAccumulator._gen_loss`), but the window's bounds are two poll instants
  no record carries, and three consecutive polls that each lose records
  collapse into one reconstructed gap. That is a format change amending
  [ADR-0015](../docs/adr/0015-gc-loss-spans-on-their-own-track.md), with its
  own spec.
- **The loss record's own fields.** `GenLoss` and `LossMsg` are one shape each
  with no optional variants (ADR-0015); there is nothing to tabulate. Spec
  0033 would add loss-derived series and is not blocked by this.
- **The `--stats` table layout** in `stats_output`. It consumes the rows; it
  does not care where they came from.
- **Adding any sub-phase CPython does not report today.**

## 7. Further notes

**What the change touches.** It changes no output but the counter order in
4.2.

| | |
|---|---|
| code | `Info` readable through `SubPhaseRow`, `_COUNTER_ORDER` deleted, `JSONL_FIELDS` and `SLICE_ARGS` moved to the tests, `stats/metrics.py` retired and the stats layer's "metric" swept, the `model/names.py` docstring that still counts six lists |
| docs | `sub-step` → `sub-phase` in `docs/formats.md` and `docs/perfetto-sql.md`, three `CONTEXT.md` entries, ADR-0003 and ADR-0007 amended, spec 0062's prior-art line repointed from `METRICS` |
| proof | cases 1 to 4 in section 5 |

**Vocabulary this settles**, in `CONTEXT.md`: a **phase** is one named
interval of a collection, drawn as a slice and totalled as a `--stats` row.
The whole pause is one, the eight inside it are **sub-phases**, and `sub-step`
is not a spelling gcmon uses. A **metric** is a figure drawn as a counter
series, and after 4.4 the word means nothing else. A **field** is one
attribute a record carries, spelled the same as a JSONL key and a span
annotation.

**On convention rule 10.** This spec amends ADR-0003 and ADR-0007. The
amendments are taken in the records themselves, in the same change, alongside
the code they describe rather than ahead of it.
