# 0036: Take every record kind through one exporter method

- **Status:** Not started
- **Kind:** feature (cleanup)
- **Effort:** M
- **Origin:** code structure review of `src/gcmon`, 2026-08-15. **Supersedes**
  0029 ([RETIRED.md](RETIRED.md)), whose buffering duplication this removes by
  construction; section 4 carries forward every constraint 0029 established.
- **Respects:**
  - [ADR-0008](../docs/adr/0008-buffered-exporter-and-encoder-protocol.md):
    the exporter buffers and the encoder serializes, and a record after
    `close()` is dropped: sections 4.4 and 4.5.
  - [ADR-0029](../docs/adr/0029-report-liveness-and-fold-it-into-the-span.md):
    liveness arrives batched, once per tick: 4.1.
  - [ADR-0013](../docs/adr/0013-rss-sampling.md): RSS on the process track
    rather than a thread's: 4.2.
  - [ADR-0015](../docs/adr/0015-gc-loss-spans-on-their-own-track.md): a loss
    window is drawn on its own track, which `convert_loss_to_trace_format`
    decides and 4.1 does not touch.
  - [ADR-0025](../docs/adr/0025-create-every-process-in-one-place.md): the
    monitor hands the exporter a `Process`, never a bare pid: 4.1.

## 1. Problem statement

An operator running `gcmon run -s app.py --format jsonl --rss` is told "RSS
tracking is not supported for --format jsonl; RSS samples will be discarded."
That warning is true, but it does not come from the exporter that discards
them; it comes from `RSS_CAPABLE_FORMATS`, a tuple of format-name strings
hand-maintained in `gcmon.cli.monitor.monitoring_options`, a package away from
the exporters whose capability it describes. The comment above it names the
method it tracks, `EventsExporter.add_rss_sample`, which is the coupling
written down rather than removed. Add an exporter and forget the tuple and the
operator gets the wrong answer in one of two directions: a warning about a
format that now works, or silence while their samples are dropped.

Underneath it is the reason the tuple exists. `EventsExporter` has grown a
method per record kind and a method per process-lifecycle notification, eight
in all, and five of the eight are no-op implementations in the base class,
each carrying a `# noqa: B027` to quiet the linter that objects to exactly
this. Every new record kind widens the interface for every exporter, and an
exporter opts out by inheriting a method that does nothing. Nothing about that
is visible at the seam.

## 2. Solution

For an operator, output is byte-identical on every format, and the RSS warning
says the same sentence; it just becomes true by construction, asked of the
exporter that will or will not handle the samples. For a maintainer, adding a
record kind means adding a branch to the exporters that care, and the ones
that do not are unchanged rather than silently widened.

## 3. User stories

1. As an operator using `--format jsonl --rss`, I want the warning about
   discarded samples to come from the exporter that discards them, so that it
   cannot be stale.
2. As an operator using a format that gains RSS support later, I want the
   warning to stop appearing without anyone remembering to edit a list, so
   that gcmon does not lie about its own capabilities.
3. As a maintainer adding a record kind, I want to add it to the exporters
   that handle it, so that the interface does not grow a method every exporter
   must know to ignore.
4. As a maintainer adding a record kind, I want the JSONL buffering written
   once, so that a record type cannot end up flushed on one path and dropped
   on another. *(carried from 0029)*
5. As a maintainer, I want this refactor to change no output byte on any of
   the three formats, so that a golden-file comparison is a valid test of it
   and `jsonl_io.read_jsonl` is not part of the change. *(carried from 0029)*
6. As a maintainer, I want `close()` to mean closed on every exporter, so that
   a double-close in a teardown path is not a per-class question. *(carried
   from 0029)*
7. As someone piping `--format stdout` into a log aggregator, I want each line
   flushed as it is today, so that a long-running monitor is not silent
   between flush thresholds. *(carried from 0029)*
8. As a maintainer writing a test exporter that only collects records, I want
   to implement two methods rather than four, so that a fake stays cheap to
   write and cannot drift from the real interface.

## 4. Implementation decisions

**4.1: Four record methods become one; the three lifecycle methods stay.** The
obvious collapse is a single `add` over every call the interface takes, and it
is wrong for the process-lifecycle three. `add_process_cmdline`,
`add_process_retired` and `add_process_liveness` never reach the buffer:
`PerfettoExporter` takes `_io_lock` and calls the encoder directly for all
three, where every record kind goes through `_enqueue`. `add_process_liveness`
also takes a *set* of processes and one timestamp, deliberately: ADR-0029 has
the monitor report the whole live set once per tick, and one lock acquisition
covers the batch. So:

```python
type ExportRecord = TItem | RssSample

class EventsExporter(ABC):
    @abstractmethod
    def add(self, process: Process, record: ExportRecord) -> None: ...
    @abstractmethod
    def close(self) -> None: ...

    # Not records. Each reaches the encoder directly, under `_io_lock`.
    def add_process_cmdline(...) -> None:  # noqa: B027
    def add_process_retired(...) -> None:  # noqa: B027
    def add_process_liveness(...) -> None:  # noqa: B027
```

Eight methods become five, and five `# noqa: B027` no-ops become three, each
of the three now a documented decision rather than an omission.

`TItem` already exists in `model.protocol` as
`TGCStatsInfo | TInstantMsg | TLossMsg`, and `to_mapping` already takes it.
`ExportRecord` belongs in `exporters.exporter`: `RssSample` is a concrete
struct in `model.data`, and `model.protocol` is what `model.data` imports, so
the union cannot live beside `TItem` without inverting that import.

Dispatch inside `add` is a guard chain, not a `match` on types. The three
record types are `Protocol` classes matched structurally by `is_gc_stats`,
`is_instant` and `is_loss`, which is how `to_mapping` already dispatches.
`is_gc_stats` goes first: GC records outnumber every other kind by orders of
magnitude.

Three producers call the interface, and all three move: `EventsMonitor`
(`add_event`, `add_loss_event`, and all three lifecycle methods),
`ControlServer` (`add_instant_event`) and `RssSampler` (`add_rss_sample`).

**Rejected: N per-pid calls for liveness.** It preserves a one-method
interface and costs ADR-0029's batching: a lock acquisition per process per
tick instead of one.

**Rejected: folding the lifecycle three into `add` as records.** They take a
different lock from the record path and produce nothing drawn at a timestamp.
One `add` covering both would take `_io_lock` or the state lock depending on
its argument, which is the seam this spec is trying to make legible.

**Rejected: `add(record)` with every record carrying its own process.**
`TGCStatsInfo` may be a `_remote_debugging` object, which knows no pid and
nothing of `pid_epoch`; ADR-0025 has `ProcessRegistry` assign that and the
monitor supply the `Process`. Making the exporter's unit an envelope means an
allocation per record.

**4.2: RSS becomes a record.** `add_rss_sample(process, rss_bytes, ts_ns)`
becomes an `RssSample` struct passed to `add`, declared in `model.data` beside
the other concrete records. `RssSampler._sample` is the one caller.
`PerfettoExporter`'s branch builds the same
`Counter(ProcessTrack(process), "rss", "rss", ts_ns, rss_bytes)` it builds
today, so the row it lands on and the counter itself are unchanged (ADR-0013).

**4.3: Capability is asked of the exporter.** Each exporter declares the
record kinds it handles; `add`'s behaviour for an unhandled kind stays what
the inherited no-op does today: drop it, silently, because raising from a
monitoring callback is the worse failure. The RSS warning moves out of
`get_monitoring_options` and into `run_monitoring_loop`, where the exporter
has been constructed and can answer for itself, and `RSS_CAPABLE_FORMATS` is
deleted. The operator still sees the warning before monitoring starts, but no
longer beside the "RSS tracking: enabled" line that prompts it: the whole
preamble is logged while the options are read, and the exporter does not exist
until the loop runs. It lands after "Self PID:" and before "Monitoring PID:".

**4.4: The JSONL buffering collapses rather than being extracted.** This is
why 0029 is superseded and not merely reordered: its three byte-identical
lock/append/threshold/flush blocks exist *because* there are three `add_*`
methods on `JsonlExporter`. Each of the three builds `{"pid": process.pid}`,
updates it from `to_mapping(item)`, and runs the same twelve lines. One `add`
means one block, and `to_mapping` is already the record-shaping for the three
kinds it accepts, so what the collapsed body adds is the drop for `RssSample`.
0029's extraction of a generic holder is no longer needed.

**Carried from 0029, unchanged: the JSONL record shape does not change, and
JSONL does not move onto `TraceEvent`.** `PerfettoExporter` buffers
`TraceEvent`; `JsonlExporter` buffers `to_mapping(record)`, the raw record
fields. Those are not two encodings of one thing, and the vocabulary already
separates them: one entry read out of the target's ring is a record, one thing
written into a trace is an event ([CONVENTIONS.md](CONVENTIONS.md), rule 4).
An event names the track it is drawn on
([ADR-0024](../docs/adr/0024-an-event-names-the-track-it-is-drawn-on.md)), so
routing JSONL through the `TraceEvent` model makes the record format a
serialization of a drawing: `jsonl_io.read_jsonl` would reconstruct records
out of events to drive `gcmon combine`, and
[docs/formats.md](../docs/formats.md#jsonl-output) would document a trace
where it documents a ring. Rejected there, rejected here.

**4.5: `close()` becomes a state on the JSONL path.** Carried from 0029, and
the hazard is one step worse than 0029 described. `JsonlExporter` keeps no
closed flag: `close()` drains the buffer and returns, so a record added after
it lands in a fresh buffer, and `_flush` reopens `_output_path` in append
mode. That record reaches the file if `flush_threshold` more follow it, and is
lost if they do not. `PerfettoExporter` has `_closed` and drops. ADR-0008
settled that a record after close is dropped silently; give `JsonlExporter`
the same guard so JSONL agrees.

**4.6: `StdoutExporter` keeps its `_open_writer` override.** Carried from
0029: three lines, plus a `close()` that flushes the stream after the base
drains it, and that is the whole of what differs between a file and an
already-open stream.

## 5. Seams and testing decisions

- **Seam:** the on-disk file, through `tests/exporters/test_jsonl_exporter.py`
  and `test_perfetto_exporter.py`, the stream through
  `test_stdout_exporter.py`, plus the JSONL leg of
  `tests/cli/analyze/test_convert_cmd.py`. That is the highest seam available
  and the correct one: the contract this must not break is the file, not the
  class structure. The RSS warning moves seam with the code it lives in: three
  assertions in `tests/cli/monitor/test_monitoring_options.py` move to
  `tests/cli/monitor/test_loop_runner.py`, and that file's import of
  `RSS_CAPABLE_FORMATS` goes with the tuple.
- **New seam needed:** none. Do **not** assert on `__mro__`, on which class
  holds the buffer, or on the method count; that pins the implementation this
  spec exists to make free to change. *(carried from 0029)*
- **What makes a good test here:** a golden-file comparison. Capture the JSONL
  a fixed set of records produces today and assert the rewrite reproduces it
  byte for byte, including loss records and instant events. "The file has
  three lines and parses as JSON" would pass on output with every field name
  wrong. For the Perfetto leg, assert what the trace *means* through the trace
  processor, since a round-trip through our own constant is equally happy with
  a right and a wrong field number
  ([ADR-0014](../docs/adr/0014-perfetto-integration-test-strategy.md)).
- **Prior art:** `tests/cli/analyze/test_convert_cmd.py` for the JSONL
  round-trip; `tests/cli/analyze/test_convert_cmd_perfetto.py` for what a
  trace means read back through the trace processor. Four fakes subclass
  `EventsExporter` and all four shrink with the interface: `MockExporter` in
  `tests/helpers.py` from six methods to four, and `Recorder` in
  `tests/exporters/loss_row.py`, `LossRecorder` in
  `tests/monitoring/test_monitor_loss.py` and `Recorder` in
  `tests/monitoring/test_loss_replay.py` from four to two.
- **Cases:**
  1. Every record kind reaches every exporter that handles it, and the output
     is byte-identical to today's for a fixed input on all three formats: the
     file for `perfetto` and `jsonl`, the stream for `stdout`.
  2. GC records, loss windows and instant events all reach the JSONL file when
     the buffer never hits the flush threshold and `close()` is what drains
     it, the path each of the three duplicated blocks owns today. *(carried
     from 0029)*
  3. `close()` twice writes the file once; an `add` after `close()` does not
     reopen it, on the JSONL path and the stdout path alike.
  4. An exporter that does not handle RSS drops the sample and does not raise,
     and `run_monitoring_loop` warns exactly once for that format.
  5. Regression guard: the Perfetto integration suite passes with no track
     moved and no field number changed, and cmdline, retirement and liveness
     each still reach the encoder once per notification.

## 6. Out of scope

- Any change to the JSONL schema, including the `ts` unit. What proves this
  refactor broke nothing is byte-identical output, and a schema change takes
  that proof away. It belongs to its own spec, which moves
  `jsonl_io.read_jsonl`, the fixtures and
  [docs/formats.md](../docs/formats.md#jsonl-output) in the same commit.
- Giving JSONL an RSS line of its own. Nothing outside this repo reads the
  format, so the barrier is not compatibility; it is that this is a feature,
  and it would change the schema the item above keeps fixed. Story 2 is
  written for the day someone does it.
- Making an unhandled record kind raise instead of dropping. ADR-0008 settled
  that deliberately and this spec does not reopen it.
- Compression, rotation, or line-buffering policy for `--format stdout`.
  *(carried from 0029)*
- The `EventEncoder` `Protocol` to `ABC` question. ADR-0008 chose the protocol
  deliberately.
- Batching anything else the way liveness is batched. Liveness is batched
  because ADR-0029 made it a per-tick observation; nothing else is.

## 7. Further notes

0029 is retired with no file of its own; [RETIRED.md](RETIRED.md) carries its
row. Section 4.4 above holds the argument for why JSONL must not move onto
`TraceEvent`, which is the single most important constraint on this work and
which a reader should have in front of them before touching `JsonlExporter`.
ADR-0008's own list of alternatives reaches the same verdict from the other
side, leaving "fold `JsonlExporter` / `StdoutExporter` into the same base" not
done because they consume raw records rather than `TraceEvent`.
