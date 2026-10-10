# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with
code in this repository.

## Agent skills

### Scope

A request covers what it names. The second issue found mid-task gets listed
and asked about, never fixed along the way. See
[`docs/agents/scope.md`](docs/agents/scope.md).

### Issue tracker

Issues are markdown files under `.scratch/<feature>/issues/`, which is
gitignored and so local to one working copy. Specs keep their tracked home in
`specs/`. See [`docs/agents/issue-tracker.md`](docs/agents/issue-tracker.md).

### Triage labels

The five canonical roles, names unchanged, written as a `Status:` line inside
each issue file rather than applied through a CLI. See
[`docs/agents/triage-labels.md`](docs/agents/triage-labels.md).

### Domain docs

Single-context: `CONTEXT.md` at the root plus `docs/adr/`, with `specs/` as
the forward-looking complement. A record has four sections, anchors through
the `Modules` field in its header, and carries no history. See
[`docs/agents/domain.md`](docs/agents/domain.md).

### Prose

Which file owns which kind of statement, what a docstring keeps, and the
CHANGELOG rules. See [`docs/agents/prose.md`](docs/agents/prose.md). The
`house-style` skill points at the same file.
