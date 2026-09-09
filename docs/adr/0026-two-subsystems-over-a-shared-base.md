# ADR-0026: Split the package into a monitor and an analysis subsystem

- **Status:** Accepted
- **Date:** 2026-09-02

## Context

The package is one linear stack, and `tests/architecture/test_layering.py`
enforces it: `support`, then `model`, then `exporters` and `stats`, then
`control`, then `monitoring`, then `cli`.

Two kinds of work sit in that stack. One attaches to a live process and writes
a file. The other reads a file gcmon already wrote and reports on it. The
boundary between them is the file itself, and it is not a new idea here: a
capture is what `monitor` produces and what `combine` consumes.

A linear stack cannot say that. `cli` is permitted every layer beneath it, so
a command that only reads a tracefile may import `monitoring`, and nothing
objects. The permission is not hypothetical: specs 0061 and 0063 add two
commands that read files and nothing else, and they land in `cli` beside
`monitor` and `run`.

`exporters/` already holds both directions. `jsonl_io` and `combine` consume a
file; the Perfetto modules and `jsonl_exporter` produce one. The layer's name
describes half its contents.

[ADR-0001](0001-hand-rolled-perfetto-protobuf-encoder.md) argued gcmon's
runtime dependency tree from the package being installable next to the process
it watches. That premise holds for the pyperf hook, which runs inside the
target ([ADR-0023](0023-the-pyperf-hook-annotates-and-does-not-drive.md)) and
so hands it whatever that path imports. `monitor` and `run` read the target
from outside and hand it nothing, and the analysis side reads a file the
target never sees.

## Decision

- The package is two subsystems over a shared base.
- The base is `support`, `model`, `stats` and `exporters`. Each subsystem's
  row in the table says which of the four it takes: `analysis` reads and
  writes files and takes three, and `stats` is reached from `cli.analyze`
  above it.
- A subsystem is defined by which side of the capture file it sits on.
  `pyperf` and `control` are monitor-subsystem code because they run beside a
  live process, whatever they import.
- A dependency the analysis subsystem takes is not one the target inherits.
  ADR-0001's argument against `perfetto` in the runtime tree therefore narrows
  to the pyperf hook, and `perfetto` may serve the analysis path.
- The monitor subsystem is `control`, `monitoring`, `pyperf` and
  `cli.monitor`.
- The analysis subsystem is `analysis` and `cli.analyze`.
- **Neither subsystem imports the other.** `cli` itself, meaning `main.py` and
  the two root modules, is the one place both are reachable, because it
  assembles the parser from both.
- `cli.shared` holds what both subsystems' parsers need, and imports nothing.
  It exists so that a subsystem never imports `cli`, where `main.py` reaches
  both.
- `analysis` holds what consumes a file gcmon wrote: `combine`, `jsonl_io`,
  and the tracefile reader spec 0061 adds. `combine` writes a trace as its
  output, and belongs here regardless, because what it reads is a capture.
- `stats` stays in the base. The live statistics table and the offline one are
  the same accumulation, and spec 0061 exists in the shape it does so that
  they cannot drift apart.
- The layer table in `tests/architecture/test_layering.py` carries the
  subsystems, and `layer_of` answers `cli.monitor`, `cli.analyze` or
  `cli.shared` by subdirectory, trying the two-segment name before the head. A
  directory under `cli/` it does not name is placed nowhere, so `unplaced`
  fails on it and no new directory is handed the CLI's permissions by sitting
  still.

## Consequences

- `gcmon.exporters` stops re-exporting `combine_files` and
  `convert_jsonl_to_trace_format`. Both are public names, and both follow the
  modules that hold them into `analysis`, which `exporters` may not import.
  `gcmon` itself is unchanged: the root belongs to `cli` by direction and
  reaches every layer.
- No boundary is enforced between the subsystems' tests. A monitor-subsystem
  test reads back what `JsonlExporter` wrote by calling `read_jsonl`, which is
  analysis-subsystem code; the layer walk reads `src/` only, and one
  distribution ships both.
- A third subsystem is now cheap to argue for and expensive to add by
  accident. The table names two, and a directory that belongs to neither has
  to say which it is.

## Alternatives considered

**One stack with two tips**, adding `analysis` as another layer under `cli`.
Rejected because `cli`'s permission to import every layer is exactly the
import the split exists to prevent: an analysis command reaching into
`monitoring` would still pass.

**Two distributions**, one per subsystem. Rejected. gcmon is pure Python, so
splitting buys no per-platform wheel and no build simplification, and the
layer test is the stronger boundary of the two: it fails on the offending
import, at the line, in the commit that wrote it, where packaging fails at
install time on someone else's machine.

**Splitting `jsonl_io` between the subsystems**, keeping the write half in
`exporters`. Rejected because there is nothing to split. `JsonlExporter`
serializes through `model.protocol.to_mapping` and never calls `write_jsonl`,
whose only caller is `combine_files`. The module is analysis-side entire.

## Implementation

- `tests/architecture/test_layering.py` holds the table, `FOLDED` and
  `layer_of`.
- `src/gcmon/analysis/`, `src/gcmon/cli/monitor/`, `src/gcmon/cli/analyze/`,
  `src/gcmon/cli/shared/`.
