# gcmon documentation

The [README](../README.md) covers what gcmon is, how it compares to the
alternatives, what it cannot do, and how to get a first trace. These pages
cover everything after that decision: how to drive the CLI, what comes out of
it, and how to read the result.

## Using gcmon

| Page | What it covers |
|---|---|
| [monitoring.md](monitoring.md) | How gcmon collects the GC record stream every other page reads: the polling loop, why some collections never reach it, and what its counters recover |
| [cli.md](cli.md) | Subcommands (`monitor`, `run`, `combine`), every option, and the environment variables that back them |
| [formats.md](formats.md) | The three `--format` values, what a trace contains, and the JSONL event schema |
| [statistics.md](statistics.md) | The `--stats` table, how to read it, and the `[stats]` extra |
| [rss.md](rss.md) | RSS tracking, its sampling behaviour, and the `[cmdline]` extra |
| [pyperf.md](pyperf.md) | The pyperf hook, the marks it writes, and its environment variables |
| [control-plane.md](control-plane.md) | Starting, stopping, and annotating monitoring from inside your application |
| [perfetto-sql.md](perfetto-sql.md) | The trace schema and example PerfettoSQL queries |

## Developing gcmon

| Page | What it covers |
|---|---|
| [../CONTRIBUTING.md](../CONTRIBUTING.md) | Setting up a working copy, running the tests and the checkers, and what a pull request needs |
| [testing.md](testing.md) | The test suites, the markers that gate them, and the conventions each one keeps |
| [adr/README.md](adr/README.md) | Architecture decision records: why the design looks the way it does |
| [../specs/README.md](../specs/README.md) | Open work: what has been specified but not yet built, and the order to take it in |
| [../specs/CONVENTIONS.md](../specs/CONVENTIONS.md) | The rules a spec follows, the two templates, and what retiring one means |
| [../specs/RETIRED.md](../specs/RETIRED.md) | Spec numbers that no longer have a file, and what became of each |
| [agents/](agents/issue-tracker.md) | How agent skills work in this repo: scope, issue tracker, triage labels, domain docs |
| [agents/prose.md](agents/prose.md) | Prose conventions: which file owns which kind of statement, and what to cut from the rest |
| [RELEASE.md](RELEASE.md) | The release process |
