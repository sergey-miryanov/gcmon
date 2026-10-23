# 0073: Convert a merged trace to the process layout

- **Status:** Not started
- **Kind:** feature (enhancement)
- **Effort:** M
- **Origin:** design session 2026-10-03, split out of
  [spec 0072](0072-draw-a-run-on-rows-by-kind.md)
- **Respects:**
  - [ADR-0021](../docs/adr/0021-write-one-trace-format.md): Perfetto is not an
    input, and a JSONL capture is the only thing that converts. Amended when
    this lands: a merged trace converts too.
  - [ADR-0026](../docs/adr/0026-two-subsystems-over-a-shared-base.md): the
    reader is analysis-subsystem code, and `perfetto` arrives with an
    `analysis` extra, which this spec declares.
  - [ADR-0007](../docs/adr/0007-shared-trace-converter-pipeline.md): the
    records read back go through the shared converter, so the process layout
    comes from the code that draws it live.
  - [ADR-0029](../docs/adr/0029-report-liveness-and-fold-it-into-the-span.md):
    a span is `[min, max]` over observations, so its two ends go back in as
    liveness observations.
  - [ADR-0011](../docs/adr/0011-process-lifetime-and-ordering.md): a live run
    ranks a process when it reaches it. The conversion describes every process
    in one batch, so its ranks follow first observation alone.
  - [ADR-0013](../docs/adr/0013-rss-sampling.md): RSS never reaches a capture,
    so the merged trace's `RSS` group is the only source for it.

## 1. Problem statement

An operator who recorded with `--layout merged` holds every number the process
layout would have drawn, and no way to see the process layout's view of them:
a row per process, and the counter charts, `heap_size` among them. Converting
from a JSONL capture works only if they kept one, and a capture never holds
RSS. Today the answer is to run the workload again.

## 2. Solution

`gcmon convert <trace> -o <out>` reads a merged trace and writes the same run
in the process layout: a row per process with its interpreter groups, the
pause and loss rows, the counters, the `Lifetime` slices and `rss`. Handed a
trace already in the process layout, it refuses and names the layout.

The output differs from what a live process-layout run would have written in
one way: process rows are numbered and ranked by first observation across the
whole trace, where a live run numbers them in the order it reached them.

## 3. User stories

1. As an operator who recorded with `--layout merged`, I want the process
   layout from that trace, so that I can read one process's counters without
   running the workload again.
2. As an operator, I want RSS in the converted trace, so that converting loses
   nothing a live run would have drawn.
3. As an operator who passes a process-layout trace, I want a refusal naming
   the layout, so that I do not mistake a copy for a conversion.
4. As an operator handed a trace with no pause in it, I want a refusal, so
   that I do not get an empty trace back.
5. As an operator whose run was killed, I want the converted trace to show
   what a killed process-layout run would, so that the conversion invents no
   span gcmon never wrote.
6. As a maintainer, I want the conversion checked against a live trace of the
   same events, so that a field the fold drops fails a test.

## 4. Implementation decisions

**`convert` reads through the trace processor**, as analysis-subsystem code
(ADR-0026), with queries of its own. They return the root rows; each slice
with its track's name, its `parent_id` and its args; and each `RSS` counter's
samples under its track's name. A track identifies nothing on a merged row,
since one track there holds slices of several processes.

**A trace is merged when its root holds `All GC Pauses`.** A trace holding a
process row with `pid > 0` is in the process layout, and `convert` refuses it
by name. Anything else holds no pause, and `convert` refuses it as such.

**The fold rebuilds records, not events.** A pause slice and the sub-phase
slices whose `parent_id` names it become one GC record, built with
`from_mapping` as `read_jsonl` builds one. The pause's args give the fields,
`generation` renamed to `gen`, and the pause's own bounds give `ts_start` and
`ts_stop`, as each sub-phase's bounds give the two fields its `bounds` reads.
A chained sub-phase shares a field with its neighbour, so either one supplies
it. A loss window becomes a loss record, its `genN` groups giving each
generation's counts and `lost_collections` its `lost_from`. A mark becomes an
instant record. Rejected: emitting the process layout's events straight from
the slices, which restates the converter's rules in a second place.

A sub-phase the merged trace does not draw had zero width, and the converter
draws nothing for one and merges none of its args into the pause, so the
rebuilt record leaves it out and the output matches.

**The records go through `convert_to_trace_format`**, into a process-layout
`ProtobufEventEncoder`, so the counters, the groups and the `Lifetime` counts
come from the code that draws them live (ADR-0007). It takes its items keyed
by `Process` instead of by pid, so two processes that held one pid stay apart;
`combine` passes `Process(pid, 1)`, the process it builds today. Going through
it puts each interpreter's touching loss windows back in time order
(`_loss_in_time_order`), which the merged trace's lanes do not keep and which
ADR-0015 needs.

**Processes come from the slices.** The `pid` and `pid_epoch` on a pause, a
loss window or a mark give its `Process`. A span, where the input holds one,
gives that process's command line to `record_process_cmdline` and its two ends
to `record_process_liveness`, so the span accumulator reproduces it
(ADR-0029). Every process is described in one batch.

**A killed run converts to a killed run.** `convert` retires every process the
input holds a span for, so each one's row, span and `Lifetime` slice go out at
the next flush, and it finishes without `close()`'s finalization. A process
the input holds no span for, one still running when the run died, keeps its
row and slices and gets no span and no `Lifetime` slice, as in a killed
process-layout trace.

**RSS comes from the `RSS` group.** Each counter is named for its process's
span, and each sample becomes the
`Counter(ProcessTrack(process), "rss", "rss", ts, value)` that
`PerfettoExporter.add_rss_sample` builds live.

**The command is `convert`**, beside `combine` and `report`. It writes
Perfetto and takes no `--layout`, since its one direction is merged to
process.

## 5. Seams and testing decisions

- **Seam:** a round trip through the trace processor. The same events go
  through a live process-layout exporter, and through a merged one followed by
  `convert`; both traces are read back and compared.
- **New seam needed:** the reader's rows and the fold from them to records,
  the fold testable without a trace processor.
- **What makes a good test here:** what the trace processor reads from both
  traces, not their bytes, with row pids and ranks left out of the comparison.
- **Prior art:** spec 0075 case 2, and `test_combine_loss_round_trip.py`.
- **Cases:**
  1. Every slice, argument, counter sample, `Lifetime` count and RSS sample of
     the live process-layout trace appears in the converted one, row pids and
     ranks aside.
  2. A record with a zero-width sub-phase converts to the same pause and args.
  3. A chained sub-phase whose neighbour had zero width keeps its start.
  4. One interpreter's touching loss windows, read back on different lanes,
     convert into a sequence.
  5. A process seen only through liveness gets its row and its span back.
  6. A process with pauses and no span, as a killed run leaves one, gets its
     row and its pauses, and no span or `Lifetime` slice.
  7. A process-layout trace is refused naming its layout; a trace with no
     `All GC Pauses` row is refused.
  8. `combine`'s output is unchanged by `convert_to_trace_format` taking
     `Process` keys.

## 6. Out of scope

- **Process layout to merged.** A process-layout reader and the merged encoder
  would do it; whether gcmon offers it is a separate decision.
- **`combine` reading a tracefile.** ADR-0021 keeps it on JSONL.
- **Matching a live run's row pids and ranks.** A live run numbers processes
  in the order it reached them, and the trace does not record that order.

## 7. Further notes

- Constrained after spec 0075, which writes the input.
