# 0077: Carry the RSS sample on each pause

- **Status:** Not started
- **Kind:** feature (enhancement)
- **Effort:** S
- **Origin:** design session 2026-10-26, on what a compact layout would need
  to keep RSS without its counter rows
- **Respects:**
  - [ADR-0013](../docs/adr/0013-rss-sampling.md): one instant stamps every
    sample in a pass, and only a pid the most recent GC poll found live is
    sampled. Amended when this lands: the sampler runs before the poll phase,
    on the previous tick's live set, and a sample is no longer backdated by a
    whole poll phase.
  - [ADR-0007](../docs/adr/0007-shared-trace-converter-pipeline.md): the
    converter knows nothing of layouts or of where a value came from. It takes
    the sample as a parameter and writes it.
  - [ADR-0008](../docs/adr/0008-buffered-exporter-and-encoder-protocol.md):
    `PerfettoExporter` is one class, so the last sample per process lives
    there.
  - [ADR-0019](../docs/adr/0019-schedule-tick-starts-on-a-fixed-grid.md): tick
    starts stay on the grid. The poll phase starts later inside a tick by the
    length of the RSS pass.
  - [ADR-0029](../docs/adr/0029-report-liveness-and-fold-it-into-the-span.md)
    and [ADR-0011](../docs/adr/0011-process-lifetime-and-ordering.md): every
    event widens its process's span, so a sample can now widen it by one tick
    past the last poll that read the process.
  - [ADR-0014](../docs/adr/0014-perfetto-integration-test-strategy.md): the
    arg is asserted through the trace processor.

## 1. Problem statement

Under `--rss`, a pause and the process's memory sit on different rows. To read
RSS at a pause, a reader finds the `rss` counter, hovers it at the pause's
timestamp, or joins the two in SQL. A layout without counter rows would carry
no RSS.

The sample is also stamped earlier than it was read. The loop polls first and
samples after, so on a wide tree a value lands up to a whole poll phase before
`psutil` returned it.

## 2. Solution

Under `--rss`, every `GC Pause` slice carries an `rss` arg: the process's most
recent RSS sample, in bytes, when gcmon read the pause. With `--rss-interval`
equal to `--rate`, that is the sample from the tick that read the pause. A
pause read before the process's first sample carries no `rss`.

The `rss` counter is unchanged. gcmon reads each sample at the start of its
tick, before the poll.

## 3. User stories

1. As someone reading a trace in the Perfetto UI, I want a pause's details to
   show the process's RSS, so that I can read the memory behind a long pause
   without finding the counter row.
2. As someone querying a trace, I want `rss` on the pause slice, so that RSS
   at each pause is one `extract_arg` and not a join against the counter.
3. As a maintainer of a future layout without counter rows, I want RSS on the
   pauses, so that the layout keeps an RSS value per pause without a row per
   process.
4. As an operator running without `--rss`, I want my trace unchanged, so that
   the arg costs nothing when I did not ask for RSS.
5. As someone reading the `rss` counter, I want each sample read at the
   instant it is stamped, so that a value does not sit a poll phase before the
   read that produced it.
6. As someone reading the run rows, I want a pause on `Gen0..2` to carry the
   same `rss` as its copy on the process rows, so that both sets of rows
   agree.
7. As someone loading `gcmon extract`'s table, I want an `rss` column, so that
   each record's RSS reaches my analysis without opening the trace.

## 4. Implementation decisions

**The sampler runs before the poll phase.** `MonitorLoop.run` calls
`RssSampler.tick` ahead of `EventsMonitor.tick`, with the same stamping
instant, and hands it the `live` set of the previous tick's `PollReport`. The
loop keeps that set between ticks. The first tick has none, so it samples
nothing. `RssSampler.tick` keeps its signature.

ADR-0013's rule holds: when the sampler runs, the previous tick's poll is the
most recent one. A process that died since then fails its `psutil` read and
yields no sample, as today. gcmon reads a pid reissued in that window as the
old process, the risk spec 0052 names for attachments. The window grows from
one poll phase to about one tick.

When the poll after a sample fails for that process, the sample still widens
the process's span to this tick's instant. `psutil` saw the pid alive at that
instant, so the span stays true to what gcmon observed.

**`PerfettoExporter` keeps the last sample per process.** `add_rss_sample`
stores the value in a dict keyed on `Process` and enqueues the counter as
today. `add_event` reads the entry and passes it to
`convert_item_to_trace_format` as a keyword parameter, `rss: int | None`.
`add_process_retired` drops the entry. The three methods run on the loop's
thread, and the dict takes no lock.

**The converter writes `RSS` into the pause's args** when `rss` is not `None`,
and on no sub-phase. `PausePhase.args` stays a function of the record. Merging
context into the args dict after conversion measured slower than taking it as
a parameter.

**The run rows need nothing.** The pause copy on `Gen0..2` reuses the
annotations the process rows encoded, so it carries `rss` too.

**`gcmon extract` writes it as the last column.** `COLUMNS` gains `RSS` after
the pause fields, and `record_rows` reads it off the pause's args like the
rest. A pause without one leaves an empty cell.

**JSONL and `combine` are unchanged.** `JsonlExporter` discards RSS samples,
so no record carries one, and `convert_to_trace_format` passes no `rss`.

**Docs.** `docs/formats.md` names `rss` among the pause's args, and how old it
can be when `--rss-interval` exceeds `--rate`. `docs/rss.md` says the sample
is read before the poll and appears on the pauses.

**Rejected: keeping the order and attaching the previous tick's sample.** The
arg would need no change to the loop, but every pause would carry a value one
tick older than its own tick's, and the sample would stay backdated by the
poll phase.

**Rejected: a timestamp arg beside the value.** With `--rss-interval` equal to
`--rate` the value is at most one tick old, and the counter row already gives
each sample's time.

## 5. Seams and testing decisions

- **Seam:** the Perfetto integration suite for the arg, read through the trace
  processor (ADR-0014); `MonitorLoop`'s unit tests with a recording exporter
  for the call order; and `gcmon extract` run in-process for the column. A
  trace shows the order only through the values the arg carries.
- **New seam needed:** none.
- **What makes a good test here:** the `rss` arg a pause slice carries, read
  through `extract_arg`; for the loop, the order of calls on the exporter
  within a tick.
- **Prior art:** the RSS cases in
  `tests/exporters/perfetto_integration/test_row_contents.py`;
  `TestRssSamplerInLoop` in `tests/monitoring/test_monitor_loop.py`; and
  `test_every_record_reads_back_as_one_row` in
  `tests/cli/analyze/test_extract_cmd.py`.
- **Cases:**
  1. Two samples of one process with a pause read after each: each pause
     carries the sample before it.
  2. A pause read before the process's first sample carries no `rss`, and its
     other args are unchanged.
  3. Two processes: each pause carries its own process's sample.
  4. A pause's copy on its `Gen` run row carries the same `rss`.
  5. No sub-phase carries `rss`.
  6. Without `--rss`, no pause carries `rss`.
  7. Within a tick, the loop samples RSS before it polls, with the same
     instant, on the live set the previous tick reported. The first tick
     samples nothing.
  8. `gcmon extract` writes each record's `rss` in its last column, and an
     empty cell for a record read before the first sample, in both layouts.

## 6. Out of scope

- **A layout without counter rows.** Dropping the `rss` counter is that
  layout's decision. Spec 0075's `merged` keeps its `RSS` group as written.
- **Restoring the counter from the args.** Pauses carry only the samples whose
  tick read a pause, so a counter rebuilt from them is coarser than the one
  recorded. The layout that drops the counter says whether it accepts that.
- **RSS on loss windows or marks.** No reader has asked for it there, and each
  would pay the arg's bytes.
- **RSS in JSONL.** `--rss` is Perfetto-only (ADR-0013), and adding it to the
  JSONL schema is a separate decision.
