# 0062: Name a workload from a sanitized command line

- **Status:** Not started
- **Kind:** feature (enhancement)
- **Effort:** M
- **Origin:** design session 2026-08-23 on comparing two tracefiles; the
  comparison in spec 0063 needs rows that mean the same thing in two files
- **Respects:**
  - [ADR-0010](../docs/adr/0010-process-identity-cmdline-and-start-marker.md):
    the command line is a debug annotation on the process slice, and absent
    where it cannot be read.
  - [ADR-0016](../docs/adr/0016-the-ring-is-the-statistics-unit.md): the ring
    stays the unit statistics are accumulated for.
  - [ADR-0018](../docs/adr/0018-stats-requires-a-view-and-keeps-no-bare-alias.md):
    the view words come from one enum.

## 1. Problem statement

The statistics table has two levels: the whole run, and one block per ring.
Under a benchmark harness neither is the level anyone is asking about.

Run a pyperformance suite and the table has one `Total` block folding sixty
benchmarks together, and several hundred ring blocks keyed by pid. The pid
means nothing after the run, and the operator's question is about a benchmark:
which of the sixty spends the most time in GC. Answering it means grouping the
blocks by hand, using command lines that are on the process slices but not in
the table.

The same shape appears in a cyclotron run, where the interesting unit is a
workload configuration rather than a process, and in any tree where several
processes are doing one job.

## 2. Solution

A **workload** is every process in a capture that ran the same thing. The
table gains a level for it, between `Total` and the ring blocks:
`--view workload` prints one block per workload, and `--view full` prints
those and the rings under them.

What counts as "the same thing" is a **sanitizer**, named on the command line.
`--sanitizer pyperformance` reads a benchmark name off each worker and folds
that benchmark's processes into one block; `--sanitizer cyclotron` does the
same for a workload configuration. With no sanitizer named, two processes
belong to one workload when their command lines are identical.

A sanitizer also decides what is not a workload at all. Under `pyperformance`
the manager process and every calibration worker are excluded, and the table
says how many processes it left out.

## 3. User stories

1. As an operator running a pyperformance suite, I want one block per
   benchmark, so that I can see which benchmark spends the most time in GC
   without grouping pids by hand.
2. As an operator running cyclotron, I want the same, keyed by its
   configuration rather than by pid.
3. As an operator whose harness has no sanitizer, I want the default to group
   by exact command line, so that the level is still there and still means
   something.
4. As an operator reading a pyperformance table, I want the manager process
   and the calibration workers left out, so that processes that ran no
   benchmark do not sit in the totals.
5. As an operator, I want to be told how many processes a sanitizer excluded,
   so that a missing block has an explanation in the output rather than in the
   source.
6. As an operator attached to a process gcmon did not start, I want a table
   that still works when no command line can be read, so that this level never
   costs me a capture.
7. As a maintainer adding a sanitizer, I want it to be one function taking a
   command line, so that I can test it without a trace.
8. As a maintainer, I want the flag's word list and the docs to come off the
   registry, so that they cannot drift.

## 4. Implementation decisions

**A sanitizer maps a command line to a key, or to nothing.** One function, one
argument, testable with no trace involved. Returning nothing excludes the
process, which is how the pyperf manager and the calibration workers leave the
table without a second mechanism.

Rejected: a sanitizer that strips volatile arguments and returns the rest. It
leaves the key as a full path and a dozen flags, which is unreadable as a
block heading, and it puts the decision about which arguments are volatile in
the same place for every harness.

**The registry is a dict in one module, and the flag reads its keys.** The
phase rows in `model/phases.py` work the same way: `--stats` reads each
phase's key off its row. `--sanitizer` takes a registry key, and both the
parser's choices and the documented list come off the registry, which is what
spec 0040 is pushing for elsewhere.

Built-in only. An entry-point plugin surface is a public API that cannot be
withdrawn, and the two sanitizers anyone has asked for ship in tree.

**One sanitizer applies to a whole invocation.** A key produced by one
sanitizer and a key produced by another do not compare, and spec 0063 exists
to compare them, so this is a property of the run rather than of a file.

**The key is recoverable from the command line alone; the label may not be.**
A pyperformance worker carries its benchmark module and its worker task
number, which together separate every benchmark in a suite, including the
eleven that share `bm_base64`. The human name for one of those lives in the
harness's own results file and not in the command line. The key is therefore
correct without any sidecar, and the block heading reads `bm_base64#3` where
the name is unavailable. Spec 0063 adds the optional sidecar that turns that
into `ascii85_large`.

**Processes with no command line make one unnamed workload.** ADR-0010 already
writes no command line when the process was not started by gcmon, has already
exited, or the `[cmdline]` extra is absent. Those processes fold into one
block labelled as unnamed rather than being dropped, so their records stay in
the table and stay out of the named blocks.

**The level applies to the live table as well.** `--stats=workload` on
`monitor` and `run`, from the same enum and the same registry. Live, a command
line needs the `[cmdline]` extra, and without it every process falls into the
unnamed workload, which is the honest result rather than a special case.

**The ring stays the accumulation unit.** ADR-0016 is untouched: a workload
block is a sum over the rings whose process carries that key, computed the way
`Total` is already computed over all of them.

## 5. Seams and testing decisions

- **Seam:** the sanitizer functions directly, and `stats_output.print_stats`
  for the level. A sanitizer takes a string and returns a key, so its tests
  need no trace, no process and no fixture.
- **New seam needed:** none beyond the registry itself.
- **What makes a good test here:** real command lines, quoted from a capture,
  asserted to the key they should produce. A test built from a command line
  invented for the test would pass on a sanitizer that never sees a real one.
- **Prior art:** the phase rows in `model/phases.py`, whose keys `--stats`
  reads, and the tests that walk them; the `StatsView.parse` tests for word
  handling.
- **Cases:**
  1. Five pyperf workers of one benchmark fold into one block; a sixth
     benchmark's workers make a second.
  2. A pyperf manager and a calibration worker are excluded, and the count of
     exclusions is reported.
  3. The eleven benchmarks sharing `bm_base64` produce eleven blocks, keyed by
     worker task.
  4. With no sanitizer, two processes with identical command lines fold and
     two with differing ones do not.
  5. A process with no command line lands in the unnamed workload, and the
     table is otherwise unchanged.
  6. Regression guard: `--view total` and `--view full` print what they print
     today, and a run with no `--sanitizer` and one process is unchanged.

## 6. Out of scope

- **Pairing workloads across two files**, and the results-file sidecar that
  names them. Spec 0063.
- **User-supplied sanitizers**, for the reason in section 4.
- **A sanitizer per file.**
- **Splitting a workload back into its processes.** `--view full` already
  prints the rings, and a per-worker level between them would compare workers
  that have no relationship to each other across two runs.
- **Reading the harness results file.** Spec 0063 introduces it; here the key
  is what matters and it comes off the command line.

## 7. Further notes

**Workload** and **sanitizer** are new vocabulary and belong in `CONTEXT.md`
when this lands. `CONTEXT.md` puts *group* on **Block**'s avoid-list and
*cohort* on **Generation**'s, so neither was available. Cyclotron's own
`--workload` flag names something narrower than the word does here, which is
the kind of collision `CONTEXT.md` exists to settle.

`docs/statistics.md` gains the third level and `docs/cli.md` the flag.

Spec 0063 depends on this. Nothing depends on spec 0061, though the two land
in the same table.
