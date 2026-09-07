# 0036: Take every record kind through one exporter method

- **Status:** Not started
- **Kind:** feature (cleanup)
- **Effort:** M
- **Origin:** code structure review of `src/gcmon`, 2026-08-15. **Supersedes**
  0029 ([RETIRED.md](RETIRED.md)), whose buffering duplication this removes by
  construction; section 4 carries forward every constraint 0029 established.
- **Respects:**
  [ADR-0008](../docs/adr/0008-buffered-exporter-and-encoder-protocol.md)
  (exporter buffers, encoder serializes),
  [ADR-0011](../docs/adr/0011-process-lifetime-and-ordering.md) (liveness
  arrives batched, once per tick),
  [ADR-0013](../docs/adr/0013-rss-sampling.md) (RSS on a sentinel track),
  [ADR-0015](../docs/adr/0015-gc-loss-spans-on-their-own-track.md)

## 1. Problem statement

An operator running `gcmon run -s app.py --format jsonl --rss` is told "RSS
tracking is not supported for --format jsonl; RSS samples will be discarded."
That warning is true, but it does not come from the exporter that discards
them; it comes from `RSS_CAPABLE_FORMATS`, a tuple of format-name strings
hand-maintained in the CLI layer, three modules away from the code whose
capability it describes. Add an exporter and forget the tuple and the operator
gets the wrong answer in one of two directions: a warning about a format that
now works, or silence while their samples are dropped.

Underneath it is the reason the tuple exists. `EventsExporter` has grown one
method per record kind (GC record, instant, loss window, RSS sample, liveness)
and three of the five are no-op implementations in the base class, each
carrying a `# noqa: B027` to quiet the linter that objects to exactly this.
Every new record kind widens the interface for every exporter, and an exporter
opts out by inheriting a method that does nothing. Nothing about that is
visible at the seam.

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
5. As an operator with an existing JSONL pipeline, I want the file format to
   stay exactly as it is, so that my parsers and my archived captures keep
   working. *(carried from 0029)*
6. As an operator running `gcmon combine` over JSONL I recorded last month, I
   want it to read back unchanged, so that this is not a migration. *(carried
   from 0029)*
7. As a maintainer, I want `close()` to mean closed on every exporter, so that
   a double-close in a teardown path is not a per-class question. *(carried
   from 0029)*
8. As someone piping `--format stdout` into a log aggregator, I want each line
   flushed as it is today, so that a long-running monitor is not silent
   between flush thresholds. *(carried from 0029)*
9. As a maintainer writing a test exporter, I want to implement two methods
   rather than six, so that a fake stays cheap to write and cannot drift from
   the real interface.
10. As a maintainer of `--format chrome+perfetto`, I want the fan-out exporter
    to forward one call rather than one per record kind, so that a new kind
    cannot be forwarded to one sub-exporter and not the other.

## 4. Implementation decisions

**4.1: The interface becomes three methods, not one.** The obvious collapse is
a single `add(pid, record)` over a tagged union, and it is wrong for liveness.
`add_process_liveness` takes a *set* of pids and one timestamp, deliberately:
ADR-0011 has the monitor report the whole live set once per tick, and
`PerfettoExporter` takes `_io_lock` once for the batch. So:

```python
type ExportRecord = TGCStatsInfo | TInstantMsg | TLossMsg | RssSample


class EventsExporter(ABC):
    @abstractmethod
    def add(self, pid: int, record: ExportRecord) -> None: ...
    @abstractmethod
    def close(self) -> None: ...

    def add_process_liveness(self, pids: Set[int], ts_ns: int) -> None:  # noqa: B027
        """Batched per ADR-0011. One no-op, and it is documented as one."""
```

Five methods become two plus one batched outlier, and the three `# noqa: B027`
no-ops become one whose reason for existing is a decision rather than an
omission.

**Rejected: N per-pid calls for liveness.** It preserves a one-method
interface and costs ADR-0011's batching: a lock acquisition per pid per tick
instead of one.

**Rejected: `add(record)` with every record carrying its own pid.**
`TGCStatsInfo` may be a `_remote_debugging` object, which has no pid; the
monitor supplies it. Making the exporter's unit an envelope means an
allocation per record, and GC records outnumber every other kind by orders of
magnitude.

**4.2: RSS becomes a record.** `add_rss_sample(pid, rss_bytes, ts_ns)` becomes
an `RssSample` struct passed to `add`. The sentinel track and the counter it
produces are unchanged (ADR-0013).

**4.3: Capability is asked of the exporter.** Each exporter declares the
record kinds it handles; `EventsExporter.add`'s base behaviour for an
unhandled kind stays what it is today: drop it, silently, because raising from
a monitoring callback is the worse failure. The RSS warning moves out of
`get_monitoring_options` and into `run_monitoring_loop`, where the exporter
has been constructed and can answer for itself, and `RSS_CAPABLE_FORMATS` is
deleted. The operator still sees the warning before monitoring starts; it
moves a few lines later in the log, after the "Output:" / "Format:" preamble.

**4.4: The JSONL buffering collapses rather than being extracted.** This is
why 0029 is superseded and not merely reordered: its three byte-identical
lock/append/threshold/flush blocks exist *because* there are three `add_*`
methods. One `add` means one block, with the record-shaping done by a `match`
on the kind. 0029's extraction of a generic holder is no longer needed.

**Carried from 0029, unchanged: the JSONL record shape does not change, and
JSONL does not move onto `TraceEvent`.** `PerfettoExporter` buffers
`TraceEvent`; `JsonlExporter` buffers `to_mapping(record)`, the raw record
fields. Those are not two encodings of one thing. The JSONL schema is public,
documented per-field in [docs/formats.md](../docs/formats.md#jsonl-output),
and read back by `jsonl_io.read_jsonl` to drive `gcmon combine`. Routing JSONL
through the `TraceEvent` model would rewrite every line of every JSONL file
gcmon has produced and break the combine reader in the same commit. Rejected
there, rejected here.

**4.5: `close()` gets a `_closed` guard on the JSONL path.** Carried from
0029: `JsonlExporter.close` has none, so unlike every other exporter it
accepts records after close and buffers them where nothing will flush them.
ADR-0008 already settled that a record after close is dropped silently; this
makes JSONL agree.

**4.6: `StdoutExporter` keeps its `_open_writer` override.** Carried
from 0029. Three lines, and the one thing that genuinely differs between a
file and an already-open stream.

## 5. Seams and testing decisions

- **Seam:** the on-disk file, through
  `tests/exporters/test_jsonl_exporter.py`, `test_chrome_trace_exporter.py`
  and `test_perfetto_exporter.py`, plus the JSONL leg of
  `tests/test_convert_cmd.py`. That is the highest seam available and the
  correct one: the contract this must not break is the file, not the class
  structure. The RSS warning is observed at
  `tests/monitoring/test_monitoring_base.py`.
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
- **Prior art:** `tests/exporters/test_combined_exporter.py` for the fan-out
  assertions; `tests/test_convert_cmd.py` for the JSONL round-trip; the
  chrome↔perfetto content-equivalence test in
  `tests/test_convert_cmd_perfetto.py`; `MockExporter` in `tests/helpers.py`,
  which is the existing test adapter and which shrinks with the interface.
- **Cases:**
  1. Every record kind reaches every exporter that handles it, and the file is
     byte-identical to today's for a fixed input on all five formats.
  2. GC records, loss windows and instant events all reach the JSONL file when
     the buffer never hits the flush threshold and `close()` is what drains
     it, the path each of the three duplicated blocks owns today. *(carried
     from 0029)*
  3. `close()` twice writes the file once; an `add` after `close()` does not
     resurrect the file.
  4. An exporter that does not handle RSS drops the sample and does not raise,
     and `run_monitoring_loop` warns exactly once for that format.
  5. Regression guard: `--format chrome+perfetto` writes the same two files,
     and the Perfetto integration suite passes with no track moved and no
     field number changed.

## 6. Out of scope

- Any change to the JSONL schema, including the `ts` unit. Both are public and
  documented.
- Making an unhandled record kind raise instead of dropping. ADR-0008 settled
  that deliberately and this spec does not reopen it.
- Compression, rotation, or line-buffering policy for `--format stdout`.
  *(carried from 0029)*
- The `EventEncoder` `Protocol` → `ABC` question. ADR-0008 chose the protocol
  deliberately.
- Batching anything else the way liveness is batched. Liveness is batched
  because ADR-0011 made it a per-tick observation; nothing else is.

## 7. Further notes

0029 is retired with no file of its own; [RETIRED.md](RETIRED.md) carries its
row. Section 4.4 above holds the argument for why JSONL must not move onto
`TraceEvent`, which is the single most important constraint on this work and
which a reader should have in front of them before touching `JsonlExporter`.
