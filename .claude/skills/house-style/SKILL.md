---
name: house-style
description: gcmon's prose rules. Which file owns which kind of statement, and what to cut from docs, ADRs, docstrings, comments and CHANGELOG entries. Load before writing or editing any of them, and when asked to trim, simplify prose, or run stop-slop over a branch.
---

# House style

The rules live in the repo, at [`docs/agents/prose.md`](../../../docs/agents/prose.md): the
routing table for who owns which statement, the CHANGELOG conventions, what a docstring keeps,
and the mechanical pass. Read it before writing or editing prose here, and treat it as the
authority over anything below. `docs/adr/README.md` and `specs/CONVENTIONS.md` own the ADR and
spec rules in turn.

`/stop-slop` handles the generic AI tells. Run it after the routing is right, not instead of it.
Its floor is *What stays* in `prose.md`: a convention it flags is a convention, not a finding.

## Writing

Write the trimmed version first. Prose here gets cut on every pass, so the short draft is the
deliverable: asking for more costs one line, cutting costs a review round.

Route each statement before drafting it. Most of what gets cut in review was not badly written,
it was written in the wrong file: a mechanism in the CHANGELOG, an argument in a docstring, a
CPython detail on a user-facing page.

Run the mechanical pass before showing a draft, not after review: em dashes out, `prose.md`'s
intensifiers out, and every count, ratio and directional claim checked against the code. "Cost
proportional to reads rather than processes" is the shape that reads fine and inverts under a
reviewer who knows the code.

## An ADR on a feature branch

A record is not done when it is written. Re-read it at each commit that moves what it anchors
on: module paths, a count a refactor changed, an alternative that stopped being hypothetical
once the code took that shape. Set the Date from the merge, because a branch that runs for days
will have drifted off the date in the draft.

When a spec or an `internals/` page takes over material the record was carrying, the record
keeps the verdict and a link and gives up the evidence. ADR-0022 grew four commits of zstd
measurements because nothing else could hold them, and shed them the same day Spec 0058 existed.

## Trimming a branch

1. `git diff main...HEAD --stat` for the files the branch touched.
2. Go in order: docs, ADRs, docstrings, comments, CHANGELOG.
3. Run `/stop-slop` over what survives, then apply the two tests in
   `prose.md`'s *Checking an edit* to everything it rewrote.
4. Rewrap what changed: `poetry run python .github/scripts/wrap_markdown.py <files>`
   (78, 80 at the outside).
5. Report per file what was cut and where the argument moved. Leave the commit to the author.
