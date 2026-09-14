# 0040: Derive the monitoring options from one table, and validate before reporting

- **Status:** Not started
- **Kind:** feature (cleanup)
- **Effort:** M
- **Origin:** code structure review of `src/gcmon`, 2026-08-15
- **Respects:**
  - [ADR-0021](../docs/adr/0021-write-one-trace-format.md): which formats
    exist.
  - [ADR-0013](../docs/adr/0013-rss-sampling.md): RSS behind a flag with an
    interval.

## 1. Problem statement

Run `gcmon run -s app.py -v --rate -1` and gcmon reports the configuration it
is about to use (`Format: perfetto`, `Rate: -1.0s`,
`Duration: until script exits`) and then refuses to start because the rate
must be positive. The echo is not a preview of a rejected configuration; it is
the same echo a successful run prints, emitted before anything is checked. An
operator reading a log has to reach the last line to know whether the lines
above it describe a run that happened.

Behind it, every monitoring option is written out three times: once as a
`get_env_*` function, once as an `add_argument` block whose help text re-names
the same environment variable, and once as a constructor parameter, a field
assignment and a keyword at the call site. `monitoring_options` imports
twenty-three names from the environment module to do it, the widest import in
the codebase. Adding an option means eight edits in three files, and the
failure mode is an option that works on the command line and silently ignores
its environment variable, or the reverse.

## 2. Solution

An operator sees one difference: the configuration echo appears only for a
configuration gcmon accepted. A rejected run prints the error and nothing
else. Everything else (flag names, defaults, help text, environment variables,
exit codes) is unchanged.

For a maintainer, an option becomes one row: its flag, its environment
variable, its type, its default and its help text, with argparse wiring and
environment defaults both derived from it.

## 3. User stories

1. As an operator reading a verbose log, I want the configuration echo to
   describe a run that actually started, so that I can tell an aborted
   invocation from a completed one at a glance.
2. As an operator who mistyped a rate, I want the error and nothing above it,
   so that the failure is the first thing I see.
3. As a maintainer adding a monitoring option, I want to add one row, so that
   its flag and its environment variable cannot disagree about the default.
4. As a maintainer, I want an option's help text to name its environment
   variable without my writing the name twice, so that renaming the variable
   cannot leave the help stale.
5. As a maintainer, I want every `GCMON_*` variable to accept exactly what it
   accepts today, so that `tests/cli/monitor/test_env.py` is the guard on the
   rewrite rather than a suite that had to be edited to pass.
6. As a maintainer of the `run` and `monitor` commands, I want option
   validation to raise rather than return `None`, so that a new command cannot
   forget the `if options is None` check and start a run with an unvalidated
   configuration.
7. As a maintainer, I want the options object to be a frozen struct like every
   other data carrier in gcmon, so that nothing downstream can mutate a
   validated configuration.
8. As an operator running `--help`, I want output identical to today's, so
   that the pages quoting it in `docs/cli.md` stay accurate.

## 4. Implementation decisions

**4.1: One `Option` row per option.** Flag names, destination, environment
variable, parser, argparse action, default and help template in one tuple; the
argparse wiring and the environment default both derive from it. The help
template carries a placeholder for the environment variable name so the name
is written once, which is the part that currently rots.

The rows are irregular in exactly three ways and the shape has to carry them:
`--verbose` is `action="count"`, `--stats` and `--rss` are `store_true`, and
`--table-format` parses through `_normalize_table_format`, which raises
`argparse.ArgumentTypeError` and must keep doing so: that is what produces
argparse's own error message for a bad value. An `action` field and a `parser`
field cover all three.

**4.2: `MonitoringOptions` becomes a frozen `msgspec.Struct` with a
`from_args` classmethod.** It is a ten-field hand-written `__init__` in a
codebase where every other data carrier is a struct. `from_args` validates and
raises a module-level error type; the caller catches once. Today each command
writes `options = get_monitoring_options(...)` followed by
`if options is None: return 1`, which is a check a third command can omit
without anything noticing.

**4.3: Validate, then describe.** `from_args` validates. A separate
`describe()` returns the lines the commands log, and the commands log them
only after construction succeeds. This is the operator-visible half of the
spec and the only behaviour change in it.

The order of the checks themselves is preserved, so an invocation with two bad
values reports the same one it reports today.

**4.4: The RSS format-capability warning is not part of this.** It is the one
line in `get_monitoring_options` that is not validation: it asks whether the
chosen format will discard RSS samples, using a hand-maintained tuple of
format names. [0036](0036-one-exporter-method-per-record-kind.md) moves that
question to the exporter, which can answer it. If 0036 lands first, the line
is already gone; if not, it moves into `describe()` verbatim and 0036 removes
it from there. Either order works.

**4.5: Environment variable names and semantics are frozen.** Every `GCMON_*`
name, every accepted truthy spelling (`1`, `yes`, `on`, `true`), every
default. They are documented in [docs/cli.md](../docs/cli.md) and in the help
text, and `tests/cli/monitor/test_env.py` pins them.

**Rejected: a settings library.** It would replace a table gcmon controls with
a dependency's opinions about precedence and error text, for eleven options,
and gcmon's runtime dependency tree is deliberately small
([ADR-0001](../docs/adr/0001-hand-rolled-perfetto-protobuf-encoder.md) made
the same call for protobuf).

**Rejected: generating the environment variable name from the flag**
(`--rss-interval` → `GCMON_RSS_INTERVAL`). It holds for all eleven today,
which is precisely why it is a trap: the first option that needs to differ
would force the convention back open, and the saving is one short string per
row.

## 5. Seams and testing decisions

- **Seam:** `tests/cli/test_cli.py`, at the command line, the highest seam
  available, because everything here exists to turn an argv and an environment
  into a configuration, and that is observable from outside.
  `tests/cli/monitor/test_env.py` and
  `tests/cli/monitor/test_monitoring_options.py` cover the environment
  defaults and the validation rules at the level they are written today.
- **New seam needed:** none.
- **What makes a good test here:** drive `main()` with an argv and a patched
  environment and assert on exit code and emitted log lines. A test that reads
  a default out of the option table and compares it to the same table proves
  nothing; assert the *resolved* value after parsing, which is what an
  operator gets. For the ordering change, assert that a rejected invocation
  emits the error and **no** configuration lines; asserting the error appears
  is not enough, since it appears today too.
- **Prior art:** `tests/cli/monitor/test_env.py` for the environment-variable
  matrix; `tests/cli/monitor/test_monitoring_options.py` for the validation
  cases and the `RSS_CAPABLE_FORMATS` parametrization; `tests/cli/test_cli.py`
  for driving `main()` end to end.
- **Cases:**
  1. Every option resolves identically from a flag, from its environment
     variable, and from neither: the same matrix
     `tests/cli/monitor/test_env.py` covers today, unchanged.
  2. A flag overrides its environment variable, as today.
  3. `--rate -1` exits 1, prints the rate error, and prints no configuration
     lines.
  4. Two invalid options report the same one reported today.
  5. `--table-format nonsense` still fails through argparse with argparse's
     message.
  6. Regression guard: `gcmon run --help` and `gcmon monitor --help` are
     byte-identical to today's output. Capture both as golden files first; the
     help text is the public surface this refactor is most likely to move by
     accident.

## 6. Out of scope

- Adding, removing or renaming any option, environment variable or default.
- The `run` command's argv split (`_split_run_args`), which is about passing
  arguments to the target and has nothing to do with option declaration.
- The `combine` command's options, which are declared inline in its own module
  and are not shared with anything.
- Logging setup and verbosity handling in `cli`, beyond `--verbose` being a
  row in the table.
- The RSS capability warning's correctness.
  [0036](0036-one-exporter-method-per-record-kind.md) owns that.

## 7. Further notes

The echo-before-validate ordering is the only operator-visible change in the
spec and it is cosmetic; it is filed here rather than as its own bug because
the fix is a consequence of separating validation from reporting, and doing it
alone would mean touching the same function twice.
