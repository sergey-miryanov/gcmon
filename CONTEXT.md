# gcmon

gcmon reads another Python process's garbage-collection records out of its
memory and turns them into a trace and a statistics table. Everything below is
vocabulary: what a word means here, and which spelling to use when several are
in circulation. `docs/adr/` records decisions and `specs/` records open work.

## Language

### How gcmon reaches a process

**Attach**:
What gcmon does once per pid before it can read that process at all: work out
where the target's runtime lives and how its structures are laid out. Held for
as long as gcmon keeps reading that pid, given up when gcmon sees the pid go,
and done again from scratch if a process comes back on it. Costs far more than
a read does, which is why it is worth naming separately from the reads it
makes possible.
_Avoid_: connect, open, handle (that is one platform's mechanism for it),
session

### How often gcmon looks

**Tick**:
One pass of the monitoring loop. gcmon reads the clock once to stamp the pass,
drops the pids that have left the tree, polls every pid still there, samples
RSS, and answers one report. The unit the **rate** is expressed in, and the
unit an **overrun** is counted in.
_Avoid_: poll (that is one pid's read, below), iteration, cycle, round, sweep

**Poll**:
One pid's read inside a tick: one look at that process's rings, bounded by its
own two instants. A tick over a tree of ten is ten polls, each with its own
pair, which is why the instant a poll starts is the thing that bounds a **loss
window** rather than the instant that stamps the tick.
_Avoid_: tick (that is the whole pass), sample, scan, query

**Rate**:
The interval an operator asks for between one tick's start and the next, in
seconds, spelled `--rate`. A duration, despite what the word usually means:
gcmon expresses nothing in Hz. Separate from what a tick costs, which the
target's size decides and nobody requests.
_Avoid_: frequency, Hz, poll rate (reads as the cost of a poll), delay, sleep,
period (fine in prose about the arithmetic, not as the name)

**Overrun**:
What a tick does when it outlasts the rate, leaving its successor no time to
start when it was due. gcmon never makes up a tick it missed, so a run that
overruns polls less often than asked and reads less of the target than the
rate implies. Said of one tick and of a whole run alike.
_Avoid_: saturation, lag, backlog, drift, slip, skipped slot (a **slot** is a
position in a ring buffer and nothing else)

### What the target writes, what gcmon writes

**Record**:
One entry gcmon read out of the target's ring buffer, describing one finished
GC run.
_Avoid_: sample, entry, datapoint

**Event**:
One thing gcmon wrote into a trace. A record becomes one or more events.
_Avoid_: record (for the trace side), slice (that is one shape of event)

**Mark**, **marker**: One instant a workload wrote into a trace to say where
it was, under either spelling. An **event** is what gcmon wrote from a record;
a mark is what the workload wrote itself. A name starts `gcmon:` and the
workload fills in the rest. The pyperf hook spells its half
`<benchmark>:<n>:<i>:begin` or `:end`; the two numbers and the two sides are
that hook's, not every mark's.
_Avoid_: annotation, label, event (that is gcmon's side)

**Batch**:
The events one flush writes, compressed into a single packet in the trace. It
is the unit a killed run loses: whole batches reach the file, and the one
being written when the run died does not.
_Avoid_: chunk, block, flush (that is the act, not what it wrote)

**Trace**:
The file gcmon writes for a viewer: events drawn on tracks, opened in the
Perfetto UI. One run writes one, live or through `combine`. **Tracefile** and
**trace file** say the same thing and are interchangeable with it.
_Avoid_: timeline, profile, output file

**Track**:
One row in a trace. An **event** names the track it is drawn on: a
**process**'s row for its marks and its RSS, an **interpreter**'s **pause
row** for that interpreter's collections, or its **loss** row.
_Avoid_: lane, thread (none of the three is one), tid, row (in output; fine in
prose)

**Interpreter group**:
The `Interpreter {iid}` row every track one **interpreter** owns hangs under:
its pause row, its loss row, its `heap_size` and its **counter group**. One
per **iid**, holding no events of its own.
_Avoid_: thread group, iid track, interpreter track (that is the pause row)

**Interpreter list**:
The `Interpreters` row holding a process's **interpreter groups**, one per
process, under the **process track**. The groups inside it sort by **iid**.
_Avoid_: interpreters track, iids group

**Process track**:
A process's own row, and what its other rows hang under: the list of its
interpreters, and the counters drawn beside that list rather than inside it.
Named `Process 12345`, or `Process 12345#2` for the second process to hold the
pid.
_Avoid_: process group, pid track, parent track

**Counter group**:
The `GC Metrics` row a process's per-generation counters hang under, one per
interpreter, itself under that interpreter's group.
_Avoid_: metrics track, group (unqualified), counter track (that is one
counter's own row)

**Capture**:
The JSONL file gcmon writes: one JSON object per line, holding the records
gcmon read and the loss windows between them. A **trace** holds events drawn
for a viewer; a capture holds what they were made from, which is why `combine`
reads one and writes the other.
_Avoid_: dump, log, export, trace file (that is the Perfetto file)

**Span**:
A slice bounding a process's observed lifetime, on the shared `Processes` row
or on the process's own.
_Avoid_: lifetime (unqualified; see below), duration, extent

**Intern id**:
The number a packet writes in place of a string the trace has already spelled
out: a slice name, a category, or the name of a debug annotation. Perfetto
spells it `iid` on the wire and gcmon does not, because an **iid** here is an
interpreter.
_Avoid_: iid (that is the interpreter), string id, symbol, handle, reference

### The things gcmon counts

**Ring**:
One CPython ring buffer, identified by `(pid, iid, gen)`. It carries its own
`collections` counter, and it is the unit statistics are reported for. A pid
outlives the process holding it, so what a run keeps to the end carries the
**pid epoch** as well.
_Avoid_: buffer, per-generation stats, slot array

**Interpreter**:
One CPython interpreter inside a process, identified by its **iid**. Each
keeps its own collector, its own rings and its own cumulative counters, and
gcmon draws every row one owns under a group named after its iid. Perfetto's
own `iid` on an interned string is a different thing; see **Intern id**.
_Avoid_: subinterpreter (an iid of 0 is an interpreter too), thread, isolate

**Generation**:
One of the collector's three age tiers, `0`, `1` or `2`, spelled **gen** in
code and `Gen0`–`Gen2` in output.
_Avoid_: tier, level, cohort

**Block**:
One heading in the statistics table and the rows under it: either the run-wide
`Total` or one ring's. The unit `--stats` selects: `total` prints the first
alone, `full` prints both kinds.
_Avoid_: section, group, table (the whole thing is the table), totals (the
per-ring `PauseTotals` and `LossTotals` are companion figures on a row, and
lifetime totals are an interval; neither is a block)

**Pid epoch**:
Which process held a pid, counting from 1 and advancing when gcmon sees one
exit. Spelled `pid_epoch`, and part of every key a run keeps to the end, so a
successor on a recycled pid never writes into its predecessor's figures.
_Avoid_: index (that is CPython's write cursor into a ring buffer, and nothing
else), generation (the collector owns it), epoch (bare, reads as a point in
time)

**Loss window**:
An interval whose records were overwritten before any poll read them, bounded
by the two poll instants either side of it. In a name, **loss** is the window
itself and **lost** is what was in it: a `LossTrack` names the row it is drawn
on, `lost_count` the records it swallowed. The window has a width of its own,
so a `loss_duration` and a `lost_pause_ns` are different numbers.
_Avoid_: missing data, dropped events, gap (in output; fine in prose about the
arithmetic), `loss_count` for a count of records

### The three intervals a number can describe

Every figure gcmon prints answers to one of these, and the two-number cells in
the `--stats` table are there to say which.

**Sampled**:
The records gcmon read. Percentiles and averages are always this, and they
read high: a long run delays the next one, so its record survives in the ring
more often than a short one's.
_Avoid_: observed (means the span, below), measured, actual

**Exact**:
Every GC run between the first and last record gcmon read on a ring, whether
or not its record survived to be read. Reconstructed from the target's
cumulative counters, and exact in the arithmetic sense.
_Avoid_: true, real, total

**Lifetime totals**:
Everything one interpreter has collected since it started, monitored window
included. Always written with the qualifier: bare **lifetime** means the span
above, the interval gcmon observed a process over, which the `Lifetime` slice
on that process's row draws. The source names the counters underneath rather
than the interval (`CumulativeCounters`, `StreamingStats.observe_cumulative`,
`cumulative_totals_by_gen`), so the bare word is left to the span everywhere
outside this prose.
_Avoid_: lifetime (bare), cumulative total (the counters underneath are
cumulative, the interval is not), since-start count

**Observed span**:
The interval from the first record gcmon read on a ring to the last. What
happened before it counts as neither sampled nor lost, because gcmon cannot
tell "ran before we attached" from "was overwritten".
_Avoid_: monitoring window (that is wall time, and wider), capture

### How complete a capture is

**Coverage**:
Sampled count over exact count, in `[0, 1]`. Printed as the `Cov` column and
as a percentage in the footer and the advisory.
_Avoid_: completeness, hit rate, fidelity

**Scale factor**:
The multiplier taking a sampled pause sum to the exact one. Printed as the `F`
column. It corrects a sum, never a percentile.
_Avoid_: correction factor, weight, extrapolation
