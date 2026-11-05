# Contributing to gcmon

Bug reports, questions and pull requests are welcome. Read the
[AI policy](.github/AI_POLICY.md) first: it asks you to be able to explain
your changes in your own words.

## Getting set up

gcmon needs CPython 3.15.1 or newer, both for the process it watches and for
itself; it will not install below that.

```bash
poetry install --all-groups --all-extras
poetry run pre-commit install
```

`--all-groups --all-extras` is not optional. The extras (`stats`, `cmdline`)
and the dev group carry test dependencies, and a partial install fails tests
that look unrelated to what is missing.

## Running the tests

```bash
poetry run just test
```

The `stress`, `fuzz`, `architecture` and `benchmark` suites are **deselected
by default** and run only when you name their marker, so a passing `pytest`
covers less than it looks. [`docs/testing.md`](docs/testing.md) has the
command for each, the CI job that runs it, and the conventions a test in it
keeps. CI runs the stress and fuzz suites in jobs of their own, so a change
that passes locally can still fail there.

## Type checking and linting

Two type checkers run in strict mode, and both have to pass:

```bash
poetry run just typecheck
poetry run just lint
```

`pre-commit` covers the rest: ruff's checker and formatter, `typos` and
`codespell`, `actionlint` and `zizmor` over the workflows, JSON-schema checks
on the GitHub configuration, and the whitespace hooks. One of those,
`mixed-line-ending --fix=lf`, keeps the tree LF-only, which matters if you
work on Windows.

## Prose

Markdown wraps at 78 columns. The tool is idempotent per file and rewrites
nothing but the wrapping:

```bash
poetry run just wrap docs/cli.md
poetry run just wrap --check docs/cli.md
```

Python wraps at 120 instead (`line-length` in `pyproject.toml`); the two
numbers are not a contradiction, one is for code and one is for prose.
[docs/agents/prose.md](docs/agents/prose.md) says which file owns which kind
of statement, which is worth reading before adding a paragraph anywhere.

## Where the design lives

- [`docs/adr/`](docs/adr/README.md) records decisions already taken and why
  the design looks as it does. If your change overturns one, amend the record
  in the same pull request.
- [`specs/`](specs/README.md) holds work that is specified but not yet built,
  and [`specs/CONVENTIONS.md`](specs/CONVENTIONS.md) the rules a spec follows.
  Several open issues point at a spec; read it before starting.

## Opening a pull request

Branch off `main`. Then, before you submit:

1. Run the tests and the checkers above **before changing anything**, so you
   know the tree was green when you started.
```bash
poetry run just check
```
2. Make the change, and keep it to what the issue names. Neighbouring problems
   you spot belong in the pull request description, not in the diff.
3. Add tests covering it.
4. Update the documentation if the change is visible from outside gcmon.
5. Add a `CHANGELOG.md` entry under `## WIP` if the change is user-facing, in
   whichever of `Breaking changes`, `Features` or `Bugfixes` fits. Internal
   work needs no entry.
6. Run the tests and the checkers again.
```bash
poetry run just check
```

Then say what you changed and how you checked it, in your own words.
