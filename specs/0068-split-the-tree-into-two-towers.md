# 0068: Split the tree into two towers

- **Status:** Not started
- **Kind:** feature (cleanup)
- **Effort:** M
- **Origin:** design session 2026-09-02 on running two applications out of one
  source tree
- **Respects:** [ADR-0026](../docs/adr/0026-two-towers-over-a-shared-base.md)
  (the towers and what each holds),
  [ADR-0001](../docs/adr/0001-hand-rolled-perfetto-protobuf-encoder.md)
  (`perfetto` stays out of the runtime, and this spec adds nothing that
  imports it),
  [ADR-0023](../docs/adr/0023-the-pyperf-hook-annotates-and-does-not-drive.md)
  (the hook runs inside the target, which is why it is monitor-tower code)

## 1. Problem statement

Nothing in the tree separates the two kinds of command gcmon has. `cli` may
import every layer, so a command that only reads a tracefile may reach into
the monitoring layer, and the layer test will pass.

The permission is about to be exercised. `gcmon combine` reads a capture and
writes a trace; it touches no process and no interpreter internals. Specs 0061
and 0063 add two more commands with the same shape, and both land in `cli`
beside `monitor` and `run`.

`exporters/` already holds both directions. `jsonl_io` and `combine` consume a
file; the Perfetto modules and `jsonl_exporter` produce one, and the layer's
name describes half its contents.

## 2. Solution

The package becomes two towers over a shared base, and the layer table says
so. `analysis/` holds what consumes a capture, `cli.analyze` the commands that
drive it, `cli.monitor` the commands that attach to a process, and
`cli.shared` what both towers' parsers need and neither owns.

Nothing an operator does changes: the commands, their flags, their output and
the help text are what they were. Two public import paths move, and two names
leave `gcmon.exporters`.

## 3. User stories

1. As an operator, I want every command to behave as it did, so that this
   costs me nothing.
2. As a maintainer adding an offline command, I want the layer test to fail
   when I import the monitoring layer from it, so that the boundary holds
   without my remembering it.

## 4. Implementation decisions

**What moves.** `exporters/combine.py` and `exporters/jsonl_io.py` become
`analysis/combine.py` and `analysis/jsonl_io.py`;
`cli/commands/convert_cmd.py` becomes `cli/analyze/convert_cmd.py`.
`monitor_cmd`, `run_cmd`, `monitoring_options` and `cli/_env.py` become
`cli/monitor/`. `parser_factory` becomes `cli/shared/parser_factory.py`, which
both towers may import and which imports nothing.

`monitoring_base.py` goes to `cli/monitor/loop_runner.py`. It holds
`run_monitoring_loop`, and its old name meant "shared by the monitoring
commands", which now collides with the base the two towers stand on.

`_env` moves whole. Every one of its getters has one caller,
`monitoring_options`, and the variables it names are `monitor`'s and `run`'s.

`jsonl_io` moves whole. `JsonlExporter` serializes through
`model.protocol.to_mapping` and calls nothing in it; `write_jsonl`'s only
caller is `combine_files`. Nothing in the monitor tower reads or writes a
capture file after the fact.

**The layer table** gains the towers, and `layer_of` answers by subdirectory:

```python
"analysis":    frozenset({"model", "exporters", "support"}),
"cli.shared":  frozenset(),
"cli.monitor": frozenset({"model", "exporters", "stats", "control",
                          "monitoring", "support", "cli.shared"}),
"cli.analyze": frozenset({"model", "exporters", "stats", "analysis",
                          "support", "cli.shared"}),
"cli":         frozenset({...every layer, the three above included...}),
FOLDED = {"pyperf": "cli.monitor"}
```

`analysis` is denied `stats`. `combine` and `jsonl_io` read and write files
and compute nothing, and spec 0061's fold from records into a `StreamingStats`
sits in `cli.analyze`, which has it.

`cli.shared` takes nothing and is taken by both towers, so no tower imports
`cli`, where `main.py` reaches both. `ROOT_CLI` is unchanged and stays `cli`:
`__init__.py` and `__main__.py` belong to neither tower. `layer_of` tries the
two-segment name before the head, so `cli.monitor.run_cmd` answers
`cli.monitor` and `cli.main` answers `cli`. A directory under `cli/` the table
does not name places nothing rather than falling back to `cli`, which is
permitted every layer: that fallback is how an offline command written into
`cli/report/` would import `monitoring` and pass.

`FOLDED` stops parking a question and answers one, because `pyperf/hook.py`
imports `control` and runs inside the target (ADR-0023). A tower is defined by
which side of the capture it sits on (ADR-0026), and the hook sits beside a
live process.

**One set of re-exports goes.** `gcmon.exporters.__init__` drops
`combine_files` and `convert_jsonl_to_trace_format`, which follow `combine`
and `jsonl_io` into `analysis`, a layer `exporters` may not import. Nothing
shims them: a module-level `__getattr__` is a deferred import of gcmon's own
code, and every `__init__.py` stays eager.

`gcmon.__init__` is unchanged: the root belongs to `cli` by direction and
reaches every layer, so `EventsMonitor`, `ChildProcess` and
`ChildProcessRunner` stay and `from gcmon import ...` gives the names it gave
before.

**The CHANGELOG.** The standing WIP line, "The modules moved into layers, so
every deep import path changed and the old ones are gone", already covers
`gcmon.exporters.combine` and `gcmon.exporters.jsonl_io` becoming
`gcmon.analysis.*`. The two names leaving `gcmon.exporters` take one line
under `### Internal`, in the shape `gcmon.TraceExporter` took.

## 5. Seams and testing decisions

- **Seam:** the walk in `tests/architecture/test_layering.py`, which is where
  a boundary can fail, and `cli.main.main` called with an argument vector for
  the behaviour that must not change.
- **New seam needed:** none; neither seam moves.
- **What makes a good test here:** the table asserted by the walk that already
  reads `src/`, and behaviour asserted through what `main` returns and prints
  rather than through the shape of an import.
- **Prior art:** `tests/architecture/test_layering.py` for the boundary, and
  the existing CLI tests for `main`.
- **Cases:**
  1. An import from `analysis` into `monitoring`, from `cli.analyze` into
     `control`, or from either tower into `cli`, fails the layer test.
  2. `layer_of` places every module in the tree: `cli.monitor.run_cmd` answers
     `cli.monitor`, `cli.main` answers `cli`, and `unplaced` finds nothing.
  3. `combine` round-trips a capture from its new home.
  4. Regression: the full suite passes unchanged.

## 6. Out of scope

- **The tracefile reader.** Spec 0061. This spec creates `analysis/` and adds
  nothing that imports `perfetto`.
- **The `analysis` extra.** Spec 0061 declares it beside the reader that
  imports it. Declaring it here ships a release whose extra installs a wheel
  nothing calls.
- **Workload sanitizers and `compare`.** Specs 0062 and 0063.
- **Two distributions.** Rejected in
  [ADR-0026](../docs/adr/0026-two-towers-over-a-shared-base.md); the towers
  are what would make it mechanical if the question returns.
- **Moving `stats` into the analysis tower.** The live table and the offline
  one are one accumulation, and spec 0061 depends on their staying so.
