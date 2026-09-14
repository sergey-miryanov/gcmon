# 0050: Name the poll interval for what it is

- **Status:** Not started
- **Kind:** feature (ergonomics)
- **Effort:** S
- **Origin:** split out of spec 0049, 2026-08-17; 0049 landed the same day
- **Respects:**
  - [ADR-0013](../docs/adr/0013-rss-sampling.md): `--rss-interval` is
    decoupled from the poll interval and stays a separate option.
  - [ADR-0019](../docs/adr/0019-schedule-tick-starts-on-a-fixed-grid.md): the
    interval is between tick starts; this renames the number, not what the
    loop does with it.

## 1. Problem statement

`--rate 0.1` does not set a rate. A rate is a frequency, and this is a
duration in seconds, the interval between one poll and the next. An operator
who reads the name literally sets `--rate 10` expecting 10 Hz and gets one
poll every ten seconds, which on a short run looks like gcmon recording
nothing.

gcmon's own output does not help. `monitoring_options` echoes `Rate: 0.1s`, a
rate quoted in seconds, and the flag's own help text reads "Seconds between
poll starts", which is a duration under a frequency's name. ADR-0013 writes
"the GC poll runs at 10 Hz by default" in its Context and "the 0.1 s GC poll
rate" in its Decision, meaning the same setting both times. The neighbouring
option gets it right: `--rss-interval` is also a duration in seconds and says
so.

## 2. Solution

The option is `--interval`, and the environment variable is `GCMON_INTERVAL`.
It reads the same way as `--rss-interval` next to it, and the log line becomes
`Interval: 0.1s`.

`--rate` and `GCMON_RATE` stop being accepted. An invocation using either gets
argparse's unrecognized-argument error, or the default, rather than a silent
second spelling for one option.

## 3. User stories

1. As an operator new to gcmon, I want the option that sets a duration to be
   named for a duration, so that I do not have to run an experiment to find
   out which direction the number goes.
2. As an operator reading the coverage advisory, I want the option it names to
   be the option in my command line, so that the advice is actionable without
   translation.
3. As someone comparing `--interval` with `--rss-interval`, I want the two to
   be the same kind of number in the same unit, so that the relationship the
   two warnings describe is obvious.
4. As a gcmon maintainer, I want one name in the source and one name in the
   docs, so that a reader of the code and a reader of the README are talking
   about the same thing.

## 4. Implementation decisions

1. **`--interval` is the name.** Not `--period`, which is accurate but rarer
   in this kind of tool, and not `--poll-interval`, which is longer and gains
   nothing once `--rss-interval` establishes that a bare `--interval` is the
   poll one. The short form `-r` becomes `-i`.
2. **The internal spelling changes with it.** `MonitoringOptions.rate` becomes
   `interval`, and `MonitorLoop`'s constructor parameter follows, as do
   `ENV_RATE`, `get_env_rate` and `parse_rate` in `_env`. Leaving them as
   `rate` would preserve exactly the confusion the spec exists to remove.
3. **Every reference moves, and the ADRs are amended rather than rewritten.**
   Roughly 140 lines across 31 files name the option or one of its internal
   spellings: `docs/cli.md`, `docs/monitoring.md`, `docs/formats.md`,
   `docs/rss.md`, `docs/statistics.md`, three ADRs, the specs that cite it
   ([0033](0033-loss-counter-track.md) and
   [0040](0040-derive-the-monitoring-options-from-one-table.md)), the coverage
   advisory in `EventsMonitor`, and the tests. `docs/adr/README.md` makes this
   explicit: an ADR anchors on the names the outside world sees, and renaming
   one of those is itself a decision, so the record moves with it.
4. **`CONTEXT.md`'s Rate entry is re-headed**, keeping the definition and
   adding the old spelling to its `_Avoid_` line. The concept does not change;
   only which word names it.

**Rejected: keeping `--rate` as a hidden alias.** It costs two lines in the
option table and one branch in `_env`, and it buys two spellings for one
option, which is the confusion this spec exists to remove. Every doc, test and
error message then has to decide which name is the real one, and the answer
has to be maintained. The break is one error message on one invocation.

## 5. Seams and testing decisions

- **Seam:** `tests/cli/test_cli.py`, which already parses argument vectors and
  asserts the resulting options, and `tests/cli/monitor/test_monitor_cmd.py`
  for the env-var path. Argument parsing is the highest seam that can observe
  a flag rename, and both suites exist.
- **New seam needed:** none.
- **What makes a good test here:** assert the value that reaches
  `MonitoringOptions`, from the flag and from the variable, rather than
  asserting the parser's internal attribute names. The hazard in a rename is a
  call site that kept the old spelling and now silently takes a default.
- **Prior art:** `tests/cli/test_cli.py` for the flag cases;
  `tests/cli/monitor/test_monitor_cmd.py` for the env-var cases and for the
  pattern of setting `GCMON_*` around a parse.
- **Cases:**
  1. `--interval 0.05` sets the interval; `-i 0.05` does too.
  2. `GCMON_INTERVAL` sets it.
  3. A flag beats a variable, as it does today.
  4. `--rate 0.05` exits with argparse's unrecognized-argument error, and
     `GCMON_RATE` set alone leaves the default in place. This is the one
     operator-visible break, and asserting it is what makes it deliberate
     rather than incidental.
  5. Regression guard: the log line, the validation errors and the advisory
     name the new option, and no message anywhere still says "rate".

## 6. Out of scope

- **How the interval is honoured.** 0049 owns the scheduling; this spec
  renames the number it schedules against and changes no behaviour. 0049 has
  landed, so the ordering it wanted is settled and what remains here is the
  prose it wrote under the old name.
- **`--rss-interval`.** Already correctly named. It keeps its own name and its
  independence from the poll interval (ADR-0013).
- **Accepting a frequency.** A `--rate 10` meaning 10 Hz, or a unit suffix
  like `100ms`. Both are real ergonomic ideas and both are a different
  feature: this spec makes one name honest, it does not add an input format.
  Adding a frequency later is easier once the duration has a duration's name.
- **`GCMON_*` variables other than this one.** None of the rest is misnamed.

## 7. Further notes

The rename is plausibly upstream of the defect spec 0049 fixed. `sleep(rate)`
after the work is a natural thing to write if you are thinking "rate", and an
obviously wrong thing to write if you are thinking "the interval between poll
starts". That was an argument for doing this soon rather than for doing it
first: 0049 made the timing contract precise, and this makes the name match
the contract.

**CHANGELOG.** One entry under `Breaking changes`, naming `--rate` and
`GCMON_RATE` as gone and `--interval` and `GCMON_INTERVAL` as what replaces
them. One fact, so it does not also appear under `Features`.
