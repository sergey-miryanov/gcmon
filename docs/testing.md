# Testing gcmon

How the suites are split, how to run each one, and the conventions a test in
them keeps. [`CONTRIBUTING.md`](../CONTRIBUTING.md) covers setting up a
working copy; [ADR-0014](adr/0014-perfetto-integration-test-strategy.md) holds
the reasoning behind the split.

## The default suite

```bash
poetry run just test
```

Trace-processor tests sit in it, behind no marker and with no `importorskip`.
The `perfetto` package is a dev-group dependency, so a developer running
`pytest` has it, and the tests import it at module level. The first run
downloads the trace-processor binary and later runs read the cache.

Coverage has a floor of 80%, set by `fail_under` in `pyproject.toml`.

## The deselected suites

`addopts` in `pyproject.toml` carries
`-m 'not stress and not benchmark and not fuzz and not architecture'`, so a
passing `pytest` covers less than it looks.

| Marker | Command | CI job | What it covers |
|---|---|---|---|
| `stress` | `just stress` | `stress-test` | thread safety of the exporter and control-client pipelines |
| `fuzz` | `just fuzz` | `fuzz-test` | randomized differential tests against the real trace processor |
| `architecture` | `just architecture` | `architecture` | the code's structure, read without running it |
| `benchmark` | `just bench` | CodSpeed workflow | performance benchmarks |

`just stress` runs two passes: `-k "control" --count 40` and
`-m stress --count 20`. The repetition is what gives a probabilistic test its
chance to fail.

`just fuzz` passes no `--count`. The seeds are fixed, so repeating a trial
re-runs the same trace; widen coverage by raising the trial count in the test.
The marker is there for cost, since the trace processor starts once per trial.

The `stress-test` and `fuzz-test` jobs are skipped on `main` and on
`release/*`.

## Writing a stress test

- Release the threads with `threading.Barrier`, never `time.sleep`.
- Use `join(timeout=...)` as a watchdog only.
- Capture each worker's exceptions into a list and assert it empty after the
  join. No worker asserts on shared state.
- The contract is no deadlock and no uncaught exception. The operating system
  decides the interleaving, so a test does not assert on it.

## Naming a value the code also holds

A name the outside world sees is pinned in one place and imported everywhere
else. Three files hold the pins:

| File | What it pins | Against |
|---|---|---|
| `tests/exporters/test_track_names_are_documented.py` | every track and slice name | `docs/formats.md` |
| `tests/infra/test_env_names_are_documented.py` | every environment variable | `docs/cli.md`, `docs/pyperf.md` |
| `tests/model/test_names.py` | that the table is shaped right and agrees with what a conversion writes | the converter |
| `tests/infra/test_vocabulary_is_used.py` | that no module respells a word the two vocabulary modules own | the modules themselves |

Every other test imports the constant or the formatter, so renaming a row, a
slice or a variable touches the code, its page and one tuple rather than every
assertion that mentions it. A name two packages render lives in
`gcmon.model.names`, the base `exporters` and `stats` share: `GC_PHASES` is
where a phase's label and category are decided, and a `--stats` row and a
timeline slice read it rather than each spelling the phase out. The metric
constants beside it are the same word in three roles, a record attribute, a
JSONL field and a counter track, so a test that builds a record and a test
that reads the trace back cannot disagree about it.

Derive a name the code derives. A per-generation counter's display name is a
function of its generation and its metric, so `tests/helpers.py` has
`gen_counter` build it; passing both the metric and the finished name let a
test assert a name the converter would never write.

`gcmon.support.vocabulary` holds what is not a name in a trace: the program
name, the encoding, the subcommands and the words `--format` takes. It sits in
`support` because `analysis` and `exporters` branch on the format words and
may not import `cli`.

A test that reads a constant back and asserts it equals its own literal is not
a pin. The rename that changes the constant changes the assertion in the same
edit, so it fails only when someone is already looking at it. Pin a name
against something written independently: a page under `docs/`, or the code
that emits it. `tests/model/test_names.py` checks the phase table against the
converter for that reason, and spells no name out.

One form keeps its literal: `sys.platform == "win32"` is how mypy and pyrefly
narrow a platform, and a constant defeats it.

The exception is a value that is not a name gcmon draws. The Chrome capture in
`tests/cli/analyze/test_convert_cmd.py` is a file an earlier release wrote;
the two process names in
`tests/exporters/test_perfetto_emission_order_fuzz.py` are labels a stack has
to tell apart; the names in `tests/exporters/test_perfetto_builders.py` are
arguments to a builder that writes down whatever it is handed. Each says so in
a comment.

Import for a value the test does not care about, such as the category on a
`Slice` it builds only to read back. Keep the literal for a threshold or a
default the test exists to hold still: `OVERRUN_SHARE` read back from the code
would assert nothing.

## Where each kind of test lives

| Directory | What it holds |
|---|---|
| `tests/exporters/` | the encoder, the Perfetto format, the track state, and the trace-processor suites |
| `tests/monitoring/` | the monitor, its loop, the reader and the samplers |
| `tests/cli/` | the parsers and each subcommand end to end |
| `tests/model/` | the record structs, the loss arithmetic, the schedule |
| `tests/stats/` | the `--stats` views, the table and the per-ring arithmetic |
| `tests/analysis/` | `combine` and the JSONL round trips |
| `tests/control/` | the control plane, client and server |
| `tests/architecture/` | the layering and lock-order checks |
| `tests/benchmarks/` | the CodSpeed benchmarks |
| `tests/support/`, `tests/infra/`, `tests/pyperf/` | the shared helpers, the repo scripts, and the pyperf hook |

Two helpers are worth knowing before writing a Perfetto test.
`tests/helpers.py` holds the reader every trace-processor test goes through,
and `tests/exporters/test_perfetto_exporter_integration.py` holds the
trace-processor fixture and the trace-writing helper. The oracle that compares
a `.pftrace` against the events it was built from lives in
`tests/cli/analyze/test_convert_cmd_perfetto.py`.
