# 0037: Choose a trace's output encoder in one place

- **Status:** Not started
- **Kind:** feature (cleanup)
- **Effort:** S
- **Origin:** code structure review of `src/gcmon`, 2026-08-15. Reduced to
  this on 2026-08-26 when 0065 deleted the meta events the rest of it was
  about; see
  [ADR-0024](../docs/adr/0024-an-event-names-the-track-it-is-drawn-on.md).
- **Respects:**
  - [ADR-0007](../docs/adr/0007-shared-trace-converter-pipeline.md): one
    conversion pipeline.
  - [ADR-0008](../docs/adr/0008-buffered-exporter-and-encoder-protocol.md):
    `combine` uses an encoder without an exporter.
  - [ADR-0021](../docs/adr/0021-write-one-trace-format.md): which formats
    there are.

## 1. Problem statement

Two call sites decide what a format name means. `EventsExporterFactory`
matches on `output_format` to build the live exporter; `combine_files` matches
on it again to choose the encoder for a combined trace. A format added to one
is not added to the other, so a format can be supported live and missing
offline, and the failure is a `ValueError` naming a word the CLI accepts.

## 2. Solution

One `encoder_for(output_format)` that both call, so the set of formats gcmon
writes is stated once.

## 3. User stories

1. As a maintainer adding an output format to `combine`, I want the format
   dispatch to be the one the live path already uses, so that a format cannot
   be supported live and missing offline.

## 4. Implementation decisions

**One encoder dispatch, still without an exporter.** `combine_files` stops
matching on `output_format` itself and calls a shared
`encoder_for(output_format)` used by both `EventsExporterFactory` and
`combine_files`.

This deliberately does **not** route `combine` through an `EventsExporter`:
ADR-0008 chose composition precisely so an encoder can run without one, and
`combine` has every event in memory and needs neither the buffer nor the flush
threshold. Sharing the dispatch gets the benefit without overturning the
record.

**Rejected: feed `combine` through `EventsExporterFactory` and delete
`convert_to_trace_format` outright.** It is the larger and more tempting
change: it would make the offline path exercise the live code end to end,
which is the strongest possible guard against divergence. It loses on two
counts. It contradicts ADR-0008's consequence that the encoder protocol has no
dependency on the exporter base, which was a decision and not an accident. And
it gives `combine` a buffering lifecycle it has no use for, including an
`add_process_liveness` call it has no source for. The fact that would settle
it differently: a third consumer of the conversion pipeline appearing, at
which point one shared path is worth more than the encoder's independence.

## 5. Seams and testing decisions

- **Seam:** the dispatch function itself, plus
  `tests/cli/analyze/test_convert_cmd.py`, which drives `gcmon convert` end to
  end for each format word.
- **New seam needed:** none.
- **What makes a good test here:** one that fails when the two paths disagree,
  rather than one per path. Drive every format word through both and assert
  neither refuses a word the other accepts; a per-path test is what let the
  dispatch grow two copies.
- **Cases:**
  1. Every format `EventsExporterFactory` accepts, `combine_files` accepts.
  2. A word neither accepts raises from both, with the same message.
  3. Regression guard: `gcmon convert` over a fixed capture produces the same
     trace for each format.

## 6. Out of scope

- Routing `combine` through an `EventsExporter` (see section 4, rejected with
  the condition that would reopen it).
- The two timestamp normalizers. `combine._normalize_trace_timestamps` works
  on `TraceEvent` and `jsonl_io.normalize_jsonl_timestamps` on records; they
  exist because there are two representations, not because of this
  duplication. [0035](0035-derive-every-gc-sub-phase-from-one-table.md) turns
  the second into a table walk, which is most of the cost of the second one.

## 7. Further notes

This and [0036](0036-one-exporter-method-per-record-kind.md) both touch the
exporter package and are independent. 0036 collapses the interface the factory
builds against, so taking this one first leaves it less to carry.
