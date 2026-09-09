# ADR-0018: Require a value on `--stats`, and keep no bare alias

- **Status:** Accepted
- **Date:** 2026-08-18

## Context

`--stats` printed one table. Spec 0045, retired on landing and summarized in
[specs/RETIRED.md](../../specs/RETIRED.md), gave it two widths, so the flag
has to carry which one. That leaves bare `--stats`, the spelling everyone
already types.

An alias would keep it working: bare `--stats` means the wider view, which
argparse spells `nargs="?"` with a `const`. The change was first asked for
that way, and gcmon's parser cannot deliver it. `monitor` takes the pid as a
**required positional**, and argparse decides whether an optional with
`nargs="?"` eats the next token by whether that token starts with `-`. A pid
does not:

```
$ gcmon monitor --stats 12345          # works today
usage: monitor [-h] [--stats [{total,full}]] [-d DURATION] pid
monitor: error: argument --stats: invalid choice: '12345' (choose from 'total', 'full')
```

So the alias buys compatibility for three of the four orderings: it keeps
`--stats` before an option (`-d`, `-r`, `-o`), `--stats` last and
`--stats=full`, and breaks `--stats` immediately before the pid. `run` splits
its argv at the first `-m` or `-s`, both of which start with `-`, so it works
either way.

That left three options: a partial alias, an `argparse.Action` that recognises
an all-digit value and hands it back to the positional, or no alias.

The wider view also needed a name. `total` and `all` are synonyms: ask which
of `--stats=total` and `--stats=all` prints more and the words give no answer,
while "the grand total of everything" points the wrong way. `totals` reads as
a noun and escapes that, but it collides with two things a reader could expect
it to print: `docs/statistics.md` defines **lifetime totals** as one of the
three intervals a cell reports, and the statistics module names its
per-`(ring, gen)` figures `PauseTotals` and `LossTotals`. Neither is a block.

## Decision

**`--stats` requires a value.** `--stats=total` or `--stats=full`. Bare
`--stats` is a parse error, and so is any word outside the set this section
names.

**No alias, for either half.** Bare `--stats` does not mean `full`, and `all`
is not a hidden synonym for it.

**`GCMON_STATS` takes the same words**, and an unreadable value fails the run
rather than falling back.

**Four words ask for no table: `no`, `off`, `false` and `0`.** On the flag and
in the variable, they select what an unset flag selects. They are the falsy
complements of the truthy set (`1`, `true`, `yes`, `on`) the variable took
while it was a switch. The truthy words stay out: "no table" is one outcome
and "a table" is two, so `off` names something and `on` does not. A blank
`GCMON_STATS` reads as unset for the same reason.

**The wider view is `full`.** `total` names the block it prints, the string in
the table's first column. `full` is a size word, and the only candidate that
reads as the larger of a pair.

## Consequences

Every existing `--stats` invocation stops at parse time, with a message naming
the values, and there is no deprecation window. The release carrying this
already reshapes the table (one block per interpreter, where it printed one
per process), so an operator upgrading re-reads the output anyway and re-types
the flag in the same pass.

Nobody has to remember which view a bare `--stats` picked. A wrong guess would
have gone unnoticed, since the two views differ by how much they print rather
than by what the numbers mean.

The off words halve the break in the variable. `GCMON_STATS=0` in a shell
profile meant no table before this change and means no table after it, so only
the settings that asked *for* the table stop the run, and their author has a
view to choose. They also give the flag something it could not say: the
variable sets a default for every run in the shell, and `--stats=no` declines
it for one. `GCMON_RSS` has the same shape and no such escape, since `--rss`
is a `store_true` with no off spelling.

`GCMON_STATS` becomes the only gcmon environment variable that can fail a run.
Every other `get_env_*` falls back on an unreadable value:
`GCMON_FORMAT=bogus` yields `chrome`, `GCMON_TABLE_FORMAT=bogus` yields plain.
A variable selecting between two named views has no default that is one of
them.

Reversing this costs more than deleting a check. Re-admitting bare `--stats`
means choosing which view it prints, a decision nobody has had to make yet.

The names constrain what comes next. A flag choosing which *metrics* print
cuts across both blocks rather than choosing between them, so it has to be a
second flag: `total` and `full` name a set of blocks, and must not grow to
mean a set of rows.

## Alternatives considered

**Bare `--stats` as an alias for `full`, via `nargs="?"` and `const`.**
Rejected: it breaks `gcmon monitor --stats <pid>` while looking like
compatibility, so the operator it was meant to protect hits an error about a
flag they did not change.

**The same, plus an action that recognises an all-digit value and re-emits it
as the pid.** Rejected: it buys back one undocumented ordering at the price of
a parser that guesses. Every example in `README.md`, `docs/cli.md` and
`docs/statistics.md` puts the target before the options, and `run` already
tells operators to put gcmon's options first.

**`all` as the wider view, or as a hidden synonym for `full`.** Rejected: a
synonym of `total` cannot be its opposite, and a spelling that works while
documented nowhere is a second vocabulary.

**`totals` as the narrower view.** Rejected: it collides with *lifetime
totals*, a different interval that prints in the footer, and with the
per-`(ring, gen)` totals the statistics module is built on. `Total`, singular,
has one referent here.

**Keeping `--stats` as a boolean and adding a second flag for the view.**
Rejected: two flags for one idea, and the invalid combination (the view flag
without the enabling flag) left to be caught by hand.

**Taking the truthy words back alongside the falsy ones, with `1` meaning
`full`.** Rejected: it is the bare alias under another spelling, and it picks
for the operator which view `1` meant. `GCMON_STATS=1` says a table was wanted
without saying which, and that is the question worth stopping for.

**Silently ignoring an unreadable `GCMON_STATS`, as the other variables do.**
Rejected for this variable: consistency with the flag is worth more than
consistency with the other variables, because the failure mode is a long
capture that prints no table at the end.

## Implementation

`src/gcmon/cli/monitor/monitoring_options.py` declares `--stats` and refuses
a bad `GCMON_STATS`; `src/gcmon/cli/monitor/_env.py` reads the raw value. The refusal
does not sit with the reading, because every `get_env_*` runs while the parser
is being built, before logging is configured. The options builder turns it
down instead, alongside `rate`, `duration` and `flush_threshold`, once logging
exists.

`StatsView` in `src/gcmon/stats/views.py` holds the view, beside the
`TableFormat` behind `--table-format`, and each member's value is the word the
operator types. The enum owns the vocabulary: it feeds argparse `choices`, and
maps a typed word to a view, to `None` for the words in `STATS_OFF_WORDS`, or
to `ValueError`. The usage line and the parser cannot drift apart, and
`monitoring_options` keeps only the flag and the message naming `GCMON_STATS`.
No table is `None` rather than a member: there is nothing to render for it,
and one member cannot carry four words.

[CLI usage](../cli.md#--stats) lists the words the flag and the variable take;
[Statistics](../statistics.md) reads what the two views print.
`tests/stats/test_views.py` pins the vocabulary, every word the flag takes and
every word it refuses. `tests/stats/test_stats_output.py` locks in that the
narrower view carries the wider one's cells unchanged, line for line where the
ring labels fit under the `PID:IID` header, and cell for cell where they do
not and the first column pads one wider.
