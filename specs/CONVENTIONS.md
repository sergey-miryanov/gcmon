# Spec conventions

How to write a spec here, and how to retire one. The open set is in
[README.md](README.md), the numbers that no longer have a file in
[RETIRED.md](RETIRED.md).

## Templates

- [TEMPLATE-bugfix.md](TEMPLATE-bugfix.md): something is broken.
- [TEMPLATE-feature.md](TEMPLATE-feature.md): enhancements, ergonomics,
  cleanups. Adds a user-perspective solution statement and user stories.

Pick by whether the change fixes something or adds something, not by size.
Both carry the same seams-and-testing section 5; until you have written it you
cannot tell whether the work is finishable. Work that fits neither, because it
produces an upstream issue instead of a change to this repo, says so in its
header. Treat that as the exception, not a third template.

## Conventions

**1. Anchor on symbols, never line numbers.** Cite the class and the method,
not the file and the line. Line numbers rot within one release. Quote code
only where the defect or the decision **is** the code, trimmed to the
decision-rich part and labelled with its symbol. A line in an external source
is the exception; pin it to a tag or a version.

**2. Sketch the seam before the solution.** Every spec says how you will test
the change, and at what level, before anyone starts. Prefer an existing seam,
the highest one that can observe the change, and keep their total low. The
ladder, highest first: an assertion on what the output means, one on the bytes
it was written in, a unit test on the component that produced them, a private
attribute. Do not hide a new suite behind a marker unless it is slow or
probabilistic, because a deselected test catches nothing.

**3. State the problem from the operator's perspective.** Lead with what an
operator sees, or what someone reading the output afterwards cannot tell; name
the faulty expression after that. Feature specs go further and carry user
stories. A change with no operator-facing consequence is a cleanup, not a bug,
however wrong the code looks.

**4. Use the project's vocabulary, and respect the ADRs.** One entry read out
of the target's ring is a **record**; one thing written into a trace is an
**event**. gcmon identifies an interpreter by its **iid** and names its
group in a trace after it. An interval whose records the target overwrote
before
gcmon read them is a **loss window** or a **blind interval**, never "missing
data". A `Processes`-track slice is a **span**. A group of layers on one side
of the capture file is a **subsystem**
([ADR-0026](../docs/adr/0026-two-subsystems-over-a-shared-base.md)), never a
tower, a side or a half. Timestamps are nanoseconds inside gcmon, and the
encoder converts them
([ADR-0009](../docs/adr/0009-nanoseconds-canonical-time-unit.md)). Link the
ADRs a spec must not contradict in its header, and if implementing it
overturns one, amend the ADR rather than the code alone. Give each record its
own bullet under `Respects:` rather than a comma run, with what it constrains
beside it: a sentence naming four records leaves no room to say which one the
work amends.

**5. Say what is out of scope**, with the reason for each; that is what keeps
a spec landable. An alternative left open ("the implementer picks one") is a
decision you skipped. Make it, or name the fact that would settle it.

**6. Assert what the output means, not that it parsed.** The characteristic
bug of a hand-written encoder is a field written under the wrong number or a
message nested in the wrong parent: the file still parses, and it reads wrong.
A round-trip test reads a value back through the same constant it wrote with,
so it passes on a wrong field number and a right one alike. Assert through a
reader you did not write.

**7. Run the design against every ADR you linked.** Rule 4 says to link the
ADRs a spec must not contradict. Linking is the cheap half: check the design
against each one before you write section 4, and name what each constrains, in
its `Respects:` bullet or in section 4. The failure this catches is a spec
defeated by a record in its own header. A record you cannot write that line
for is one you listed without reading.

**8. Derive a fact once, rather than twice with a test between them.** A spec
that computes the same fact in two places and proposes a test to keep them
agreeing has left the design unfinished. Weigh computing it once in the same
section, and say why not. The phrasing to catch in your own draft is "these
can drift, and the test that stops them is".

**9. Check the deferrals against section 1.** Rule 5 asks for a reason beside
each item that is out of scope. This asks whether what remains still fixes the
complaint. Read section 1 as though everything in section 6 were deferred
forever: a complaint that still reads true means the spec covers part of the
work. A follow-up spec written the same day as the last implementation commit
is this check failing late.

**10. Write the record before a spec that amends it.** Rule 4 covers the
accidental case, where implementing a spec overturns an ADR nobody expected it
to. This is the planned one: a header that names the record the work amends is
saying the decision has not been taken yet. Take it in the ADR, and the specs
under that record become implementation steps. Inside a draft the tell is a
section 4 with one entry the others rest on, and more than one record to move.

## Lifecycle

**Delete the file when a spec retires; move its row to
[RETIRED.md](RETIRED.md).** This folder is the open set, not a history: the
prose goes, git keeps it. The number outlives the file, because commit
messages, ADRs and other specs cite it, and the row answers what that number
was in one line: that it landed, when, and where the durable part went.

Name the outcome in the row's **Kind** column as **Landed**, **Declined** or
**Superseded**, with a date for the first two and the superseding spec for the
third, and drop the link. Keep the summary, in the past tense if it read as a
complaint, and point at whatever survived: an ADR, the spec that replaced it,
or nothing.

**Cap the summary at 40 words**: the complaint, what it is today, and the
pointer. The mechanism, the argument and the numbers one run produced belong
to the ADR the row points at, and git has the file that was deleted.

Retiring one spec is therefore three edits: delete the file, cut its row from
`README.md` including any mention in the suggested order, and paste it into
`RETIRED.md` with its outcome. If the work settled something durable, write an
ADR under [`docs/adr/`](../docs/adr/README.md) first, so the row has somewhere
to point.

Assign numbers in order and **never reuse or renumber one**, so a reference to
a spec keeps meaning one thing. Take the next number from the highest in
either table. A gap in the *folder* means a spec retired.

Mark a spec **Pinned** in its status line when a characterization test locks
the behaviour it wants to change, and name that test: fixing the bug then
means changing the test in the same commit. The shape to watch for is a test
that asserts the buggy behaviour because it covers the branch instead of
stating what should happen.
