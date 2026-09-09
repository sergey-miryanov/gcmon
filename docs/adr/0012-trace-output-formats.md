# ADR-0012: Support Perfetto output in `combine`, and dual output only in live mode

- **Status:** Superseded by [ADR-0021](0021-write-one-trace-format.md)
- **Date:** 2026-06-25, amended:
  - 2026-06-27: `chrome+perfetto` added

> gcmon writes one trace format now. ADR-0021 carries forward the half of this
> record that survives, meaning `combine --output-format perfetto`, the
> normalization split, Perfetto not being an input, and `-o` used verbatim.
> Everything below is left as written.

## Context

Two related questions about where trace formats are produced.

**Offline.** `gcmon combine` merged Chrome JSON or JSONL inputs and wrote
Chrome JSON or JSONL. Producing a Perfetto trace from existing captures meant
re-running `monitor --format perfetto`, which is impossible after the fact.

**Live.** A monitoring session produced one format. If you wanted both a
`chrome://tracing` file and a `ui.perfetto.dev` file, you ran the monitor
twice against different runs, and the two traces described different
executions.

The pieces to fix both already existed:
[ADR-0007](0007-shared-trace-converter-pipeline.md) made `list[TraceEvent]` a
format-independent intermediate, and
[ADR-0008](0008-buffered-exporter-and-encoder-protocol.md) made
`ProtobufEventEncoder` usable on its own, outside any exporter.

## Decision

### `combine --output-format perfetto`

All paths except `jsonl → jsonl` funnel through a single `list[TraceEvent]`,
then dispatch on output format. `jsonl → jsonl` keeps its fast path, since it
needs no intermediate.

| from ↓ / to → | chrome | jsonl | perfetto |
|---|---|---|---|
| chrome | yes | **rejected** | yes |
| jsonl | yes | yes | yes |
| perfetto | n/a | n/a | n/a |

`chrome → jsonl` exits 1: the Chrome format has already lost the
`TGCStatsInfo` structure JSONL needs. **Perfetto is not accepted as an
input.** That would require a protobuf decoder, which is substantial work and
sits outside the encoder's remit
([ADR-0001](0001-hand-rolled-perfetto-protobuf-encoder.md)).

**Normalization is per input file** on each `TraceEvent`-based path.
`chrome → chrome` already worked this way; `jsonl → chrome` normalized over
the combined list. The refactor unified them on the per-file contract.
`jsonl → jsonl` still normalizes over the merged dict, on purpose: those items
keep their original `TGCStatsInfo` / `TInstantMsg` structure, and per-pid
zeroing across the merge is the established behaviour.

The CLI does not derive a file extension from `--output-format`; it uses the
`-o` path verbatim.

### `--format chrome+perfetto` on `monitor` and `run`

`CombinedTraceExporter` is a thin forwarder over a real `TraceExporter` and a
real `PerfettoExporter`. It implements `EventsExporter` directly and does
**not** extend `BufferedTraceExporter`, so each sub-exporter owns its own
buffer, locks and meta-dedup state. Closing takes Chrome inside a `try` and
Perfetto in the `finally`, so a Chrome failure still closes Perfetto.

One `-o` argument becomes two files via `Path.stem`, which strips only the
last extension:

| `-o` | chrome | perfetto |
|---|---|---|
| `trace` | `trace.json` | `trace.pftrace` |
| `trace.json` | `trace.json` | `trace.pftrace` |
| `trace.foo` | `trace.json` | `trace.pftrace` |
| `out/gcmon` | `out/gcmon.json` | `out/gcmon.pftrace` |

Both files keep the parent directory, so the existing parent-directory check
covers both.

**`chrome+perfetto` is rejected by `combine`.** Combining is a single-output
operation; dual output is a live-mode concern. `combine`'s `--output-format`
choices stay `["jsonl", "chrome", "perfetto"]`.

**`GCMON_FORMAT` accepts the same set as `--format`**, `perfetto` and
`chrome+perfetto` included. Its whitelist had omitted both, so
`GCMON_FORMAT=perfetto` fell back to Chrome.

## Consequences

- You can convert existing captures to Perfetto without re-running anything.
- One monitoring session yields both viewers' formats, describing the same
  run.
- **Cross-file ordering is not guaranteed under multi-threaded callers.** If
  two threads interleave between the Chrome and Perfetto calls the forwarder
  makes, the two files may order events differently. The monitor loop is
  single-threaded, so the primary use case is unaffected.
- **Partial failure is not rolled back.** Chrome is written first, so if the
  Perfetto sub-exporter raises mid-write, the Chrome file may contain events
  the Perfetto file does not. Two back-to-back monitor runs fail the same way.
- In `combine`, the pids are historical and usually dead, so cmdline lookup
  fails and descriptors go out without it. See
  [ADR-0010](0010-process-identity-cmdline-and-start-marker.md).
- `chrome+perfetto` stays **undocumented in the README** on purpose. It is an
  internal debugging convenience, and documenting it would promise support for
  the ordering and partial-failure caveats above. `--help` still lists it.
- Combining is not streaming. The whole output is built in memory, as before.

## Alternatives considered

- **A `MultiEncoder` driving two encoders from one `BufferedTraceExporter`.**
  Rejected: it would have to reconcile two flush thresholds and two file-mode
  lifecycles inside one buffer. Two independent sub-exporters are simpler and
  reuse code that is already tested.
- **Extending `combine` to dual output.** Rejected as scope creep; `combine`
  takes one `-o` and writes one file.
- **Auto-deriving the output extension from `--output-format` in `combine`.**
  Rejected: you control `-o`, and rewriting a path you typed would surprise
  you.
- **A custom cmdline provider for `combine`.** Rejected: the default degrades
  gracefully, and historical pids have no cmdline to find.

## Implementation

- `src/gcmon/cli/commands/convert_cmd.py` holds the `--output-format` choices
  and the `chrome → jsonl` rejection.
- `src/gcmon/exporters/combine.py` combines the inputs: the `jsonl → jsonl`
  fast path, per-file normalization in the load loop, and the `perfetto`
  branch.
- `src/gcmon/exporters/combined_exporter.py` derives the two paths and
  forwards to both sub-exporters.
- `src/gcmon/exporters/exporter_factory.py` handles the `chrome+perfetto`
  case.
- `src/gcmon/cli/_env.py` holds the `GCMON_FORMAT` whitelist.
- Tests: `tests/test_convert_cmd_perfetto.py` is trace-processor driven and
  carries the chrome↔perfetto content-equivalence assertions;
  `tests/exporters/test_combined_exporter.py` and
  `test_combined_exporter_integration.py` cover the forwarder;
  `tests/monitoring/test_monitor_cmd.py` checks end-to-end that both files are
  written.
