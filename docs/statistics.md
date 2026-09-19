# Statistics

Use `--stats` to display a statistics table at the end of monitoring. The
table reports GC pause durations (p50, p90, p95, p99) and counts per
generation, in a `Total` block for the whole run, with one block per
interpreter under it if you ask for them.

The flag takes the view as a required value. `--stats=total` prints the
run-wide `Total` block, `Read Time` and the footer, and `--stats=full` adds
one block per interpreter. [CLI usage](cli.md#--stats) lists every word the
flag and `GCMON_STATS` accept, including the ones asking for no table.

**On a single-interpreter run `--stats=total` costs you nothing.** The block
it drops, `12345:0`, repeats the `Total` block above it line for line. Reach
for `--stats=full` on a target running sub-interpreters or a tree of
processes, where the per-interpreter blocks say which interpreter carried the
pause time.

Read it as: **P99 is your tail latency** (1 in 100 pauses is at least this
long), **Sum divided by the monitoring wall time gives the share of the
monitored window spent in GC**, and **Count and Avg show how many pauses there
were and how long a typical one took**. A P99 GC pause that exceeds your
request SLO is a good starting point for tuning.

The last row, `Read Time`, is monitor-side cost rather than target-process
cost: it measures how long each read of a target's GC stats took, recorded
once per successful poll of every monitored PID and aggregated into a single
row; with child processes its `Count` is polls × PIDs, and there is no per-PID
breakdown.

The first read of a process has to locate its GC state before it can read it,
which costs far more than the read itself: one outlier per monitored process,
charged to this row. That is why `Avg` sits above `P50` here, while the
percentiles describe the ordinary reads.

Use it to sanity-check `--rate`: a mean `Read Time` approaching `--rate` means
a tick is close to outlasting its own position on the schedule, and the
summary's tick counts say whether it did. See
[How gcmon reads a process](monitoring.md#polling).

## Example Output

```bash
$ gcmon monitor 12345 --stats=full --table-format md

| PID:IID | Metric                   |   Count |             Sum |    Avg |    P50 |    P90 |    P95 |    P99 |    Cov |      F |
|---------|--------------------------|---------|-----------------|--------|--------|--------|--------|--------|--------|--------|
| Total   | GC Pause(0)              |  42/210 |  55.795/240.595 |  1.328 |  1.323 |  1.922 |  2.304 |  2.373 |  20.0% |  4.312 |
|         | GC Pause(1)              |   18/25 | 116.416/154.216 |  6.468 |  7.026 |  8.468 |  8.890 |  9.655 |  72.0% |  1.325 |
|         | GC Pause(2)              |       5 |         167.709 | 33.542 | 38.937 | 42.645 | 43.270 | 43.770 | 100.0% |  1.000 |
|         |                          |         |                 |        |        |        |        |        |        |        |
|         | GC Deduce Unreachable(0) | 42/~210 | 37.197/~160.397 |  0.886 |  0.882 |  1.281 |  1.536 |  1.582 |  20.0% |  4.312 |
|         | GC Deduce Unreachable(1) |  18/~25 | 77.611/~102.811 |  4.312 |  4.684 |  5.645 |  5.927 |  6.437 |  72.0% |  1.325 |
|         | GC Deduce Unreachable(2) |       5 |         111.806 | 22.361 | 25.958 | 28.430 | 28.846 | 29.180 | 100.0% |  1.000 |
|         |                          |         |                 |        |        |        |        |        |        |        |
| 12345:0 | GC Pause(0)              |  42/210 |  55.795/240.595 |  1.328 |  1.323 |  1.922 |  2.304 |  2.373 |  20.0% |  4.312 |
|         | GC Pause(1)              |   18/25 | 116.416/154.216 |  6.468 |  7.026 |  8.468 |  8.890 |  9.655 |  72.0% |  1.325 |
|         | GC Pause(2)              |       5 |         167.709 | 33.542 | 38.937 | 42.645 | 43.270 | 43.770 | 100.0% |  1.000 |
|         |                          |         |                 |        |        |        |        |        |        |        |
|         | GC Deduce Unreachable(0) | 42/~210 | 37.197/~160.397 |  0.886 |  0.882 |  1.281 |  1.536 |  1.582 |  20.0% |  4.312 |
|         | GC Deduce Unreachable(1) |  18/~25 | 77.611/~102.811 |  4.312 |  4.684 |  5.645 |  5.927 |  6.437 |  72.0% |  1.325 |
|         | GC Deduce Unreachable(2) |       5 |         111.806 | 22.361 | 25.958 | 28.430 | 28.846 | 29.180 | 100.0% |  1.000 |
|         |                          |         |                 |        |        |        |        |        |        |        |
|         | Read Time                |     300 |           5.700 |  0.019 |  0.016 |  0.021 |  0.024 |  0.031 |        |        |

1. Coverage: Gen0 20.0%, Gen1 72.0%. Count and Sum read sampled/exact; percentiles are sampled and read high.
```

*Values in milliseconds, per GC generation (0, 1, 2).*

The first column reads `PID:IID`: the process, then the interpreter inside it.
A target running sub-interpreters prints `12345:0`, `12345:1` and so on, each
row describing that interpreter alone, and `Total` folds them together. Every
process has an interpreter 0, so an ordinary run reads `12345:0`.

An operating system hands the same pid out again once the process holding it
has gone, and a long run watching a tree of short-lived workers will see that.
Each process that held the pid gets a block, and the ones after the first say
which they are: `12345:0#2` is the second process to run under pid 12345.
Their counts, their coverage and their history stay apart.

## Three intervals, and which one a cell reports

A target's collector can run faster than gcmon reads the records it writes, so
some GC runs never reach the table. See
[How gcmon reads a process](monitoring.md). Every number below describes one
of three intervals:

- **Sampled**: the records gcmon read. `Avg` and every percentile are sampled.
- **Exact, over the observed span**: every GC run between the first and last
  record gcmon saw, including those whose records never reached it. `Count`
  and `Sum` report this, reconstructed from the target's cumulative counters.
- **Lifetime totals**: everything an interpreter has collected since it
  started, monitored window included. They appear in the footer under the
  table. They overlap the other two rather than extending them, so they stay
  out of `Cov` and `F`.

Always write the qualifier: bare *lifetime* means a process's span on the
`Processes` track ([output formats](formats.md#perfetto-output)), a wall-clock
interval.

The observed span starts at the first record gcmon read. gcmon cannot tell
"ran before we attached" from "lost", so an earlier run falls outside the span
and counts as neither.

## `Count` and `Sum` cells: `sampled/exact`

Where the two differ, the cell shows both:

```
Count           Sum
42/210          55.795/240.595
```

gcmon read 42 gen-0 pauses totalling 55.795 ms. 210 gen-0 runs finished in
that window, taking 240.595 ms. A cell carries one number when nothing was
lost.

Sub-phase rows (`GC Mark Alive`, `GC Deduce Unreachable`, `GC Delete Garbage`,
…) mark their second number with a leading `~`: `42/~210`. CPython accumulates
a total for the whole pause only, so a sub-phase has no exact counterpart.
Those are estimates, the sampled value scaled by `F`.

## The `Cov` and `F` columns

**`Cov`** is the share of records gcmon read, `sampled ÷ exact`. At `20.0%`,
four out of five records in that row never reached a poll. It never rounds to
`100.0%` while anything is missing: a row that lost 8 records of 1771 prints
`<100.0%`.

**`F`** is the multiplier taking a sampled pause sum to the exact one,
`exact_sum ÷ sampled_sum`. It scales the sub-phase rows, and prints `>1.000`
for the same reason.

Both are blank on rows with no generation, such as `Read Time`.

If any one interpreter's coverage falls below 90%, gcmon logs one advisory per
session, naming the process, the interpreter and the generation of the least
covered ring. A starved interpreter beside a busy one trips it on its own
figure. It says what survives the loss and no more; the end-of-run summary
carries the remedy, where the coverage and the tick counts are both final.
Polling faster may observe more, but it will not lift `Cov` to 100%;
[How gcmon reads a process](monitoring.md) covers why.

## Percentiles are sampled and read high

`P50` through `P99` describe the records gcmon read rather than every run that
happened, and the difference skews one way. A long run delays the next one, so
its record sits in the ring slot for longer and is likelier to survive until
the next poll. Long pauses are over-represented among the survivors, so **the
reported percentiles read high**, the more so the lower `Cov` is.

`Total`'s percentiles mix one more thing: every interpreter and every process
the run watched. The ring rows below keep those apart, so read the shape off
one of them.

`F` does not fix this. It is a ratio of two totals, so applying it to a
quantile would assume the sampled and unsampled pauses share a shape, which is
what the bias denies. gcmon reports the quantiles it measured instead. On a
low-`Cov` row, trust the counts and sums and distrust the shape.

The trace draws the intervals the missing records fell in, on a `GC Loss`
track. See [output formats](formats.md#gc-loss-slices).

## The notes under the table

Below the table gcmon prints a numbered note for each thing the cells cannot
say. Any of them may be absent, and a session that read every record from a
target that collected nothing before gcmon attached prints no footer at all. A
lone note still reads `1.`, so read the wording rather than the number.

**1. Coverage.**

```
1. Coverage: Gen0 20.0%, Gen1 72.0%. Count and Sum read sampled/exact; percentiles are sampled and read high.
```

The `Cov` column gathered across every ring, which is the scope `Total`
reports, plus the rule for reading the two-number cells. It appears whenever
anything was lost, listing only the generations that lost something.

**2. Lifetime totals.**

```
2. Since each interpreter started, monitored window included, summed over 3 interpreters in 2 processes: Gen0 4820 in 6231.400 ms.
```

The third interval above. It changes no cell in the table. The counts say what
the figure folded: three interpreters that started at different moments, in
two processes. A run watching one interpreter reads
`1 interpreter in 1 process`.

**3. Rings with no row.** `--stats=full` only.

```
3. 2 rings got no row: gcmon was already tracking 256 interpreters at once. Those records are counted in Total.
```

gcmon holds detailed statistics for a bounded number of interpreters at once,
the count this note states, and a process that exits hands its slots back. An
interpreter that starts while every slot is busy gets no block of its own, and
gcmon logs a warning the first time it happens. `Total` still counts every
record, so the rows can add up to less than the run and this note says by how
many rings.

`--stats=total` prints no ring rows, so gcmon leaves the note out. The warning
naming the pid and the interpreter is logged under either view. Notes 1 and 2
are run-wide and print under both.

## Without `[stats]` extra

gcmon keeps a fixed number of samples in memory and sorts them for exact
percentiles. Once the buffer is full it drops the oldest, so a long session
loses its early shape.

## With `[stats]` extra

Install the optional `ddsketch` dependency for high-accuracy, memory-efficient
statistics:

```bash
pip install gcmon[stats]
```

This installs [DDSketch](https://github.com/DataDog/sketches-py), which:
- Takes every sample, with no buffer limit
- Computes approximate quantiles with 0.1% relative accuracy
- Holds constant memory however long the session runs

Install it for a long run or a fast `--rate`.

`Count`, `Sum`, `Cov`, `F` and the lifetime totals are running values, and do
not depend on how many samples the buffer retains.
