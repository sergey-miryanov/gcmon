# 0035: Derive every GC sub-phase from one table

- **Status:** Not started
- **Kind:** feature (cleanup)
- **Effort:** L
- **Origin:** code structure review of `src/gcmon`, 2026-08-15
- **Respects:**
  [ADR-0007](../docs/adr/0007-shared-trace-converter-pipeline.md) (one
  conversion pipeline; this extends its decision rather than changing it),
  [ADR-0009](../docs/adr/0009-nanoseconds-canonical-time-unit.md) (nanoseconds
  internally)

## 1. Problem statement

Nothing an operator sees is wrong today. This is the largest single
maintenance cost in the codebase: CPython's collector has eight optional
sub-phases, and gcmon writes that list out by hand in six separate places.
Adding the ninth means editing all six, in six different shapes, and nothing
fails if one is missed: the sub-phase simply does not appear in that output. A
miss in the converter costs a slice in the trace; a miss in the stats table
costs a `--stats` row; a miss in the normalizer leaves one timestamp unshifted
in a `combine --normalize` run, which reads as a slice at the wrong place on
the timeline rather than as an error.

ADR-0007 already made this argument once, for the two trace backends, and won
it. The same duplication survives across the other three consumers.

## 2. Solution

For an operator, nothing changes: the same slices, the same `--stats` rows,
the same JSONL fields, byte for byte. What changes is that a maintainer adding
a sub-phase writes one row in one table, and every consumer picks it up, or a
test tells them the row is wrong before it reaches a trace.

## 3. User stories

1. As a maintainer tracking a new CPython GC sub-phase, I want to declare it
   once, so that a trace, a `--stats` table and a JSONL line cannot disagree
   about whether gcmon supports it.
2. As a maintainer, I want a misspelled field name to fail a test rather than
   silently produce a sub-phase that never appears, so that the failure
   arrives in CI and not in a capture.
3. As a maintainer reading `trace_converter`, I want the eight near-identical
   sub-phase blocks to be one loop, so that the one block that differs is
   visible instead of buried.
4. As an operator comparing `--stats` against a Perfetto trace of the same
   run, I want the two to cover exactly the same set of sub-phases, so that a
   row missing from one is a real finding and not a gap in gcmon.
5. As an operator running `gcmon combine --normalize` over JSONL, I want every
   timestamp on a record shifted, so that no sub-phase slice lands at an
   absolute timestamp beside rebased siblings.
6. As a gcmon maintainer reviewing a sub-phase patch, I want the diff to be
   one table row plus its test, so that review is about whether the phase is
   right rather than whether six edits match.
7. As someone reading the codebase for the first time, I want one place that
   answers "what sub-phases does gcmon know about", so that I do not have to
   reconcile six partial lists.
8. As a maintainer, I want the conversion benchmark to hold, so that
   collapsing the cascades does not quietly make a large capture slower to
   convert.

## 4. Implementation decisions

**The six sites.** Each holds its own copy of the sub-phase list, in its own
shape:

| Site | Shape today |
|---|---|
| `data.GCStatsInfo` | the optional timestamp and count fields |
| `protocol` | eight per-phase `Protocol` classes and nine `has_*` TypeGuards |
| `protocol.to_mapping` | seven `if has_*(item):` blocks assigning field by field |
| `stats.METRICS` | nine `Metric` classes, each a name and a two-field getter |
| `trace_converter.convert_item_to_trace_format` | eight near-identical `Slice` blocks |
| `jsonl_io.normalize_jsonl_timestamps` | eight `if has_*(item):` subtraction blocks |

`data.GCStatsInfo` stays as it is: it is the msgspec decode target and its
fields are the JSONL schema, which is public and documented in
[docs/formats.md](../docs/formats.md#jsonl-output). The table describes those
fields; it does not replace them.

**4.1: One `Phase` table, in the model layer.** A tuple per sub-phase, ordered
as the collector runs them. The shape, which is the decision:

```python
class Phase(NamedTuple):
    key: str  # "mark_alive": the METRICS key and the --stats row identity
    label: str  # "Mark Alive": slice name; "(gen)" is appended at the call site
    category: str  # "gc.mark.alive"
    present: str  # the field whose presence means the target reported this phase
    start: str  # may name *another* phase's stop field
    stop: str
    args: tuple[str, ...] = ()  # record fields carried onto the slice's args
    gens: frozenset[int] | None = None  # None means every generation
```

`present` is separate from `start` and that separation is the whole reason a
naive table is wrong. Three phases begin at the preceding phase's stop:
finalize-garbage starts at the handle-weakrefs stop, handle-resurrected at the
finalize-garbage stop, clear-weakrefs at the handle-resurrected stop.
`has_finalize_garbage` accordingly probes the *stop* field, not the start. A
table that assumed `present == start` would ask whether the previous phase
ran.

`gens` carries the two generation restrictions the converter applies today
(increment size is shown for generations under 2, alive size for generations
over 0) so they stop being inline conditions in the middle of a block.

**4.2: The `GC Pause` row is in the table.** It is not optional and it is not
a sub-phase, but `stats.METRICS` already treats it as one and `stats_output`
keys its exact-versus-estimated column off `metric_key == "pause"`. Keeping it
in the table with `present` set to its start field preserves that, and the one
caller that must distinguish it keeps doing so by key.

**4.3: The four consumers become walks over the table.** `to_mapping` walks it
for the optional fields and keeps its explicit list of the mandatory ones.
`stats.METRICS` is derived from it and the nine `Metric` classes go: each is a
name and a two-field getter, which is what a row already is.
`convert_item_to_trace_format` becomes a loop emitting one `Slice` per
present phase whose interval is non-empty, preserving today's
`stop - start > 0` guard, which now also fixes the slice's duration.
`normalize_jsonl_timestamps` walks the start and stop field names.

**4.4: The `has_*` guards and the per-phase `Protocol` classes go.** Once the
table drives every consumer, nothing narrows to `TMarkAliveInfo` or its
siblings; a single `phase_present(item, phase)` replaces nine guards.
`is_gc_stats`, `is_instant`, `is_loss` and `has_pause_ts` stay: they
discriminate *record kinds*, which is a different job and one the loss work
relies on.

**Accepted cost: the cascades are statically checked and a table indexed by
field-name strings is not.** This is the strongest objection to the spec and
it is real. mypy and pyrefly verify today that `item.ts_mark_alive_start`
exists on the type the guard narrowed to; `getattr(item, phase.start)`
verifies nothing. The mitigation is the same one
[0030](0030-exporter-hygiene-batch.md) section 4.1 chose for `_COUNTER_RANKS`:
a test that asserts every field name in every row exists on
`data.GCStatsInfo`, checked by name. That converts a typo from a silently
absent sub-phase into a test failure, which is a better outcome than today's,
where a *missing* row is not caught by anything at all.

**Rejected: generate the table from `GCStatsInfo` by field-name convention**
(pair anything matching `ts_<name>_start` with `ts_<name>_stop`). Four of the
eight phases do not follow the convention, including all three that begin at a
predecessor's stop, so the generator would need a table of exceptions, which
is the table, plus a generator.

**Rejected: keep the guards and share only the naming strings.** That is
precisely what ADR-0007 rejected when it merged the two trace backends: "the
guards were the small part."

**Open, to settle when picked up:** whether `to_mapping` keeps its
hand-written mandatory-field block or moves to `msgspec.to_builtins` with the
optional fields omitted. It reads `TGCStatsInfo`, a `Protocol`, so the record
may be a `_remote_debugging` object rather than a struct; whether
`to_builtins` handles that is the fact that settles it. Out of scope either
way: the JSONL bytes must not change.

## 5. Seams and testing decisions

- **Seam:** the chrome↔perfetto content-equivalence test in
  `tests/test_convert_cmd_perfetto.py`, through the trace processor. It is the
  highest seam available: it observes the sub-phases as *slices in a trace*,
  which is what the table exists to produce, rather than observing the table.
  `tests/stats/test_metrics.py` and the JSONL golden file cover the two
  consumers the trace processor cannot see.
- **New seam needed:** none for behaviour. One new test *file* at an existing
  seam, the table-completeness test in section 4, which asserts against
  `data.GCStatsInfo`, not against the table itself.
- **What makes a good test here:** assert the trace still *means* the same
  thing: that a record carrying all eight sub-phases produces the same eight
  named slices, at the same timestamps, on the same track. A test that reads
  the phase list out of the table and checks the converter emitted that list
  proves only that the loop ran; it would pass with every row wrong. Pin the
  expected slice names and categories as literals in the test, since they are
  the public surface an operator queries in PerfettoSQL.
- **Prior art:** `tests/exporters/test_perfetto_format.py` for per-slice
  assertions; `tests/test_convert_cmd_perfetto.py` for the cross-encoder
  equivalence shape; `tests/stats/test_metrics.py` for the METRICS coverage
  assertions.
- **Cases:**
  1. A record carrying every sub-phase produces the identical `TraceEvent`
     list before and after: same order, same names, same categories, same
     args.
  2. A record carrying none of them produces only the `GC Pause` pair and the
     counters.
  3. The generation restrictions hold: increment size appears for gen 0 and 1
     and not gen 2; alive size for gen 1 and 2 and not gen 0.
  4. Every field name in the table exists on `data.GCStatsInfo`.
  5. Regression guard: byte-identical JSONL for a fixed record set, and
     `tests/benchmarks/test_bench_trace_conversion.py` within its current
     budget. The converter comments call the args dict the largest single cost
     of converting a record, so a table-driven rewrite must not add a
     per-phase allocation.

## 6. Out of scope

- Any change to the JSONL schema or to slice names, categories and arg keys.
  All are public; this is a refactor with no output diff.
- The `--stats` table layout in `stats_output`. It consumes `METRICS`; it does
  not care where `METRICS` came from.
- Splitting `stats/stats.py` and `model/data.py` into modules by concern. That
  was 0039, landed 2026-08-21: the nine `Metric` classes and `METRICS` are
  `stats/metrics.py`, a module named for the table this spec derives.
- The loss record's own fields. `GenLoss` and `LossMsg` are one shape each
  with no optional variants
  ([ADR-0015](../docs/adr/0015-gc-loss-spans-on-their-own-track.md)); there is
  nothing to tabulate.
- Adding any sub-phase CPython does not report today.

## 7. Further notes

This extends ADR-0007 rather than contradicting it: that record decided a
`TGCStatsInfo` becomes a `TraceEvent` in exactly one place, and it holds. What
it did not reach was the three consumers that read the record for something
other than a trace: the JSONL writer, the stats accumulator and the offline
normalizer. When this lands, ADR-0007's Context and Consequences should be
amended to say the sub-phase list has one home for *every* consumer, not only
for the two backends.
