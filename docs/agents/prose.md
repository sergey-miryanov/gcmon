# Prose conventions

Which file owns which kind of statement, and what to cut from the rest.
[`docs/adr/README.md`](../adr/README.md) is the authority on ADRs and
[`specs/CONVENTIONS.md`](../../specs/CONVENTIONS.md) on specs. This page
covers the CHANGELOG, docstrings, comments and the user-facing pages, plus the
routing that spans all of them.

Write the trimmed version first. A draft that carries its own justification
gets cut on review, and asking for more costs one line.

## Who owns what

| Statement | Home | Keep it out of |
|---|---|---|
| A change an operator can observe | `CHANGELOG.md` | any mechanism behind it |
| Internal work: refactors, performance, tests | the standing `### Internal` line | an entry of its own |
| A new user-facing documentation file | `### Documentation` | edits to a page that exists |
| Why the design has this shape | `docs/adr/` | user docs, docstrings, comments |
| Work specified but not built | `specs/` | ADRs |
| How to drive gcmon, how to read its output | `docs/*.md` | CPython internals |
| A CPython or OS internal the design rests on | `docs/internals/` | `docs/*.md` and the ADRs |
| What the code below cannot say itself | the docstring summary line | a body narrating the code |
| A constraint that must not regress | a test name and its assertion | a comment asserting it |
| A reading from one run: a rate, a byte count | nowhere | all of the above |

## The CHANGELOG

`.github/scripts/extract_changelog.py` lifts a version's whole `##` section
into the GitHub release notes verbatim, so every line reaches users and a new
`###` heading is safe.

- **`Features`, `Bugfixes` and `Breaking changes` describe what an operator
  sees.** A change that alters a printed number belongs there; the reason it
  changed does not.
- **Internal work gets one standing line** under `### Internal`, phrased at
  the level of "Stability, correctness and performance improvements". Later
  internal work joins that line.
- **`### Documentation` is for new user-facing pages.** Correcting an existing
  one is internal work and falls under the standing line.
- **Entries take the one-line shape of their neighbours.**
- **One entry, one claim.** A bullet carrying two splits into two. Length is
  the symptom, not the rule: compressing a double entry keeps the fault and
  only loses words.
- **A fact appears once in a release.** `Breaking changes` says what is gone,
  `Features` what exists now, `Bugfixes` what was wrong. Before an entry
  stands, look for its object in the other two sections.
- **An entry names a defect the previous release could reach.** Date it
  against the tag. Code written and fixed inside one cycle reached no
  operator, and joins the standing `### Internal` line.
- **The claim gets checked against the code.** An entry states behaviour, and
  the source or the test says whether that behaviour is what ships.

## Docstrings and comments

Keep the summary line. Every sentence after it has to say something neither
the code below nor another file already says: a rejected alternative, a
measured cost, an ordering constraint that is not visible locally.

A comment restating an invariant that a test enforces goes stale, so delete it
and let the failing test carry the rule. Where the reason is architectural,
cite `ADR-NNNN` from the docstring rather than restating the argument; the
citation survives the refactor that would have stranded the copy.

## User-facing pages

`docs/*.md` says what gcmon does and how to read its output. CPython internals
stay out, including anything that a future release could change under us.
Links run from an ADR to a page, never back: a page names no ADR and no spec.

## What lands nowhere

- Numbers measured on one machine: a collection rate, a bar's width, an error
  bound. They date the text to the machine that produced them.
- The journey. What was searched, what broke, what was tried. The decision is
  the part worth keeping, and the record already holds it.
- A clause after the claim giving its reason or its use, whatever it opens
  with: *so*, *since*, *which is what*, *rather than*, or no marker at all. "A
  per-process figure is a `GROUP BY`" is the reader being told how to use the
  result. This holds outside `docs/adr/`; a record's reasoning is what the
  record is for.
- A literary phrase where a standard term exists. "Case and surrounding space
  are forgiven" is "case-insensitive, surrounding whitespace stripped".
- The document justifying its own existence. "Worth recording because", "this
  is worth spelling out since the obvious reading is that it was missed",
  "written down so the next person to look finds it known". A statement earns
  its place or gets cut, and a defence of keeping it is not the same thing as
  earning it.
- An intensifier or a hedge carrying no fact: *strictly*, *deliberately*,
  *exactly*, *precisely*, *outright*, *quietly*, *for now*. Cut the word and
  check the sentence still claims what it claimed. *Exactly* counting a thing
  and *strictly* bounding a set are terms, and stay.
- The value a test asserts. The routing table sends the constraint to the
  test, and the number it compares against goes with it. "The same ring
  without the departure opens one" beats "emits a window of 297", which is a
  fixture copied into prose and stale the moment the fixture changes.

## What stays

The cutting rules above have a floor. A convention is not slop, and a pass
that trims one does damage that reads like tidying.

- A formulaic container the reader expects: the CHANGELOG's `###` headings, an
  ADR's Status and Context, a template's sections. The community reads the
  container, not around it.
- The standing `### Internal` line. It repeats across versions because that is
  what standing means.
- A table or a list where the facts are genuinely enumerable. Prose is not the
  more human form of a five-row table.
- A one-word answer. Terseness is the house default here, not a draft that ran
  out of effort.
- A habit the file already keeps. Edit toward the voice in front of you, not
  toward a generic one.

## Checking an edit

Three tests on anything a trimming pass rewrote, before it stands.

Read it once, at reading speed. A backtrack fails it. Three constructions
cause most of them: a preposition stranded between the subject and its verb
("a process gcmon polled and read no collections from draws a row"), a pronoun
whose referent changes mid-sentence ("a process's row ends where gcmon last
read it"), and a term defined by itself ("refuses a word `--format` would
refuse").

Strike each word the pass added. If the sentence still parses and still claims
what it claimed, the word was filler and goes. Then put back each phrase the
pass replaced. If the old wording was sound and said the same, the old wording
wins. A shorter version that dropped a claim does not win on length.

Repair fails the last two tests and stays: the subject a split run-on needs, the verb
that replaces a nominalisation, the article that makes a sentence grammatical.
A passage that ends longer than it began was not trimmed.

## Mechanical

- No em dash. No section sign, which is hard to type on an ordinary keyboard.
- Wrap at 78, 80 at the outside:
  `python .github/scripts/wrap_markdown.py <files>`.
- Use the vocabulary in `specs/CONVENTIONS.md` rule 4: record, event, iid,
  loss window, span. Do not coin a synonym for one of them.
- Every file is LF. A rewrite that flips the endings buries the real diff;
  `wrap_markdown.py` skips a CRLF file rather than write one.
