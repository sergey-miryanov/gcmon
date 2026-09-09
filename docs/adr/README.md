# Architecture Decision Records

An ADR records a decision that shaped gcmon's design, along with the reasoning
and the evidence behind it: the trade-offs accepted, the alternatives
rejected, and the constraints the decision puts on future work. Read one when
you want to know why a piece of the design looks the way it does.

An ADR does not document *what* the code does. The code and its tests are the
authority on that. Each ADR anchors into the source by module path and by the
names the outside world sees (a slice or arg name in a trace, a JSONL field, a
CLI flag, a `--stats` column), so you can check the two against each other.
Renaming one of those is itself a decision, and the record moves with it.

Forward-looking work that has been specified but not yet built lives in
[`specs/`](../../specs/README.md), not here: one file per open item, deleted
when it lands. A spec that settles a durable design question graduates into a
record below.

## Conventions

- **Filename:** `NNNN-kebab-case-title.md`. Assign numbers in order and
  **never reuse or renumber them**, so a reference to ADR-0007 keeps meaning
  the same record.
- **Status:** `Accepted`, `Accepted, unbuilt (spec NNNN)`, or
  `Superseded by ADR-NNNN`. A record is unbuilt while its decision is taken
  and the code has not caught up: its anchors name modules that do not exist
  yet, and the spec it names is the work that creates them. Drop the qualifier
  in the commit that lands the spec. Leave a superseded record in place; the
  history is worth keeping. Do not delete or rewrite one. Write a new record
  that supersedes it and link both ways.
- **Date:** when the change shipped, not when you wrote the file. An unbuilt
  record dates the decision instead, and takes the merge date when the work
  lands. A record that has not shipped yet has no history to keep, so rewrite
  it in place rather than appending a note about what you changed. One that
  has shipped grows a sublist instead: `2026-06-27, amended:` on the Date
  line, then a `YYYY-MM-DD: what changed` bullet per amendment, oldest first.
  Date each from its own merge, since a branch that runs for days drifts off
  the date in the draft, and end it `see ADR-NNNN` where another record drove
  the change. Two amendments that shipped the same day get a bullet each.
- **Sections:** Decision holds the rules, one to a bullet, each of which
  something in the code obeys. Consequences holds what follows from them and
  never restates one: "there is now one prune" is the decision, not a
  consequence of it. Alternatives holds designs that were weighed and
  rejected, not choices made inside the decision. "The argument is
  keyword-only" belongs in Decision or nowhere, and listing it as an
  alternative invents a debate that did not happen.
- **Numbers:** the ones that are the decision belong in the record, meaning
  the defaults, thresholds and limits somebody chose, along with an
  illustration anyone can reproduce. A reading taken from one run does not: a
  collection rate, a bar's width, a byte count, an error bound. Those date a
  record to the machine that produced them and settle nothing the shape does
  not settle on its own.
- **Anchors:** module paths, class names and the names outside the module
  boundary. A record names no function or method: architecture does not turn
  on what a helper is called, and a record that tracks internal names goes
  stale on every refactor. Point the other way instead, from a docstring
  citing ADR-NNNN, which survives the rename of the code around it. The one
  exception is a method that *is* the decision, meaning a protocol's members
  or a seam's entry point: ADR-0008 spells the encoder's three methods because
  they are what it decided, and ADR-0017 spells the monitor's tick for the
  same reason. Domain vocabulary stays whatever its spelling in code, so a
  state a record argues about, such as `INVALID_PROCESS`, keeps its name even
  though an enum member holds it.
- **New records:** copy [`0000-template.md`](0000-template.md).

Amend an existing ADR when the reasoning is refined or when a name it anchors
on moves. Write a new one when the decision itself changes. A rename inside a
module is neither.

## Index

| # | Decision | Status | Modules |
|---|---|---|---|
| [0001](0001-hand-rolled-perfetto-protobuf-encoder.md) | Hand-roll the Perfetto protobuf encoder; keep `perfetto` out of the runtime dependency tree | Accepted | exporters |
| [0002](0002-perfetto-track-uuid-and-hierarchy.md) | Allocate track UUIDs sequentially and parent every track explicitly | Accepted | exporters |
| [0003](0003-gc-metrics-group-track.md) | Parent per-generation counters to a non-OS-scoped `GC Metrics` group track | Accepted | exporters |
| [0004](0004-toplevel-shared-counters.md) | Emit `heap_size` and `rss` as single top-level counters, outside the `GC Metrics` group | Superseded by 0024 | exporters |
| [0005](0005-counter-y-axis-share-key.md) | Use the metric name itself as `CounterDescriptor.y_axis_share_key` | Accepted | exporters |
| [0006](0006-begin-end-slice-pairs.md) | Represent durations as Begin/End pairs in both backends | Superseded by 0024 | exporters, model |
| [0007](0007-shared-trace-converter-pipeline.md) | Convert GC stats to `TraceEvent` once, in a shared pipeline | Accepted | exporters, model, monitoring |
| [0008](0008-buffered-exporter-and-encoder-protocol.md) | Split exporters into a buffering base class and a pluggable `EventEncoder` | Accepted | exporters |
| [0009](0009-nanoseconds-canonical-time-unit.md) | Store `TraceEvent.ts` in nanoseconds; convert at the encoder | Accepted | exporters, model, support |
| [0010](0010-process-identity-cmdline-and-start-marker.md) | Carry process cmdline in two places, and force the process track to render | Accepted | exporters, monitoring |
| [0011](0011-process-lifetime-and-ordering.md) | Show process lifetimes on one shared track, ordered by first observation | Accepted | exporters, monitoring |
| [0012](0012-trace-output-formats.md) | Support Perfetto output in `combine`, and dual output only in live mode | Superseded by 0021 | cli, exporters |
| [0013](0013-rss-sampling.md) | Sample RSS in a standalone `RssSampler`, on a `tid = -1` sentinel track | Accepted | cli, exporters, monitoring |
| [0014](0014-perfetto-integration-test-strategy.md) | Validate traces against the real trace processor; deselect slow suites by marker | Accepted | tests |
| [0015](0015-gc-loss-spans-on-their-own-track.md) | Draw reconstructed GC loss on a per-interpreter track, one span per poll interval | Accepted | exporters, model, monitoring, stats |
| [0016](0016-the-ring-is-the-statistics-unit.md) | Report statistics per ring, and drop the per-process row from the `--stats` table | Accepted | monitoring, pyperf, stats |
| [0017](0017-monitor-owns-the-pid-lifecycle.md) | Give the monitor every piece of per-pid state, and leave the loop the clock | Accepted | monitoring |
| [0018](0018-stats-requires-a-view-and-keeps-no-bare-alias.md) | Require a value on `--stats`, and keep no bare alias | Accepted | cli, stats |
| [0019](0019-schedule-tick-starts-on-a-fixed-grid.md) | Schedule tick starts on a fixed grid, and skip the positions a slow tick misses | Accepted | cli, model, monitoring, stats |
| [0020](0020-attach-to-a-process-once.md) | Attach to a process once, and let go the moment a read fails | Accepted | monitoring |
| [0021](0021-write-one-trace-format.md) | Write one trace format, and read only JSONL back | Accepted | cli, exporters |
| [0022](0022-compress-each-batch-of-packets.md) | Compress each batch into one `TracePacket.zstd_compressed_packets` field | Accepted | exporters |
| [0023](0023-the-pyperf-hook-annotates-and-does-not-drive.md) | Mark the benchmark from the pyperf hook, and drive nothing | Accepted | model, pyperf |
| [0024](0024-an-event-names-the-track-it-is-drawn-on.md) | An event names the track it is drawn on, and the encoder derives the rest | Accepted | exporters, model |
| [0025](0025-create-every-process-in-one-place.md) | Create every process in one place, and carry it instead of a pid | Accepted | cli, control, exporters, model, monitoring |
| [0026](0026-two-subsystems-over-a-shared-base.md) | Split the package into a monitor and an analysis subsystem | Accepted | analysis, cli, exporters, monitoring |
