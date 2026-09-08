# ADR-0024: An event names the track it is drawn on

- **Status:** Accepted
- **Date:** 2026-08-26, amended:
  - 2026-08-28: `old_work` joined the top-level set
  - 2026-08-31: a track named a process rather than a pid, see
    [ADR-0025](0025-create-every-process-in-one-place.md)
- **Supersedes:** [ADR-0004](0004-toplevel-shared-counters.md),
  [ADR-0006](0006-begin-end-slice-pairs.md)

## Context

`TraceEvent` came from the Chrome Trace Event format, and kept its vocabulary
after [ADR-0021](0021-write-one-trace-format.md) removed the format that
needed it: a `ph` discriminator, and a `(pid, tid)` pair in which a row
belonging to no interpreter took a tid no interpreter would claim. The encoder
is the only consumer left.

Three things followed from the Chrome shape:

- `ProcessMeta` and `ThreadMeta` existed so a producer could tell the encoder
  which rows to draw. Two producers implemented that, and the encoder never
  read the names they carried.
- A counter event carried a dict of metrics, and the encoder concatenated a
  display name from it, with a special case to avoid `heap_size heap_size`.
- Sentinel integers identified loss and RSS, so the encoder had to test the
  tid to find out what kind of row it was writing.

## Decision

**A `Track` names a row, and every event carries one.**
`ProcessTrack(process)`, `InterpreterTrack(process, iid)` and
`LossTrack(process, iid)`. The `(pid, tid)` pair and the sentinels go.
`LossTrack` and `InterpreterTrack` carry the same two fields and name
different rows. The first field is a `Process`
([ADR-0025](0025-create-every-process-in-one-place.md)), so a trace can draw two
processes that shared a pid apart. A capture read back offline carries no
epoch, so `combine` builds every pid a first process.

**The encoder derives every other row from those.** Ahead of the first packet
naming a track it emits the pid's process descriptor, whichever kind of track
it is, then the track's own where it has one. No event names a counter's row,
the `GC Metrics` group holding it or the `Processes` track; the encoder
allocates all three. `ProcessMeta` and `ThreadMeta` go, with both
implementations.

**The meta dedup race closes by deletion rather than by relocation.** The race
[ADR-0008](0008-buffered-exporter-and-encoder-protocol.md) records was two
producers racing on a check-and-add under `BufferedTraceExporter._lock`. With
no producers, dedup lives only in `PerfettoTrackState`, reached through
`write_events` and `record_process_liveness`, both already under `_io_lock`.

**A counter carries one metric, its value and a written display name.** The
converter writes the display name, `G0 collected` or `Thread 0 heap_size`,
where the encoder used to concatenate one. `metric` is the other field, and
still does the grouping: it drives the sibling rank and the shared y axis
([ADR-0005](0005-counter-y-axis-share-key.md)), so `G0 collected` and
`G1 collected` keep one scale.

**The converter qualifies a top-level counter with the interpreter that owns
it.** `Thread 0 heap_size` and `Thread 0 old_work`, interpreter 0 included.
Which metrics are top-level is a policy in `perfetto_format.py`, not a
property of the metric.

**A slice is one event, and the encoder expands it.**
`Slice(track, name, cat, ts_start, ts_stop, args)` replaces `SliceBegin` and
`SliceEnd`, which only ever went out as a pair. Perfetto has no complete-slice
event, so the pair survives on the wire.

**Nesting needs no reconstruction in gcmon.** Perfetto builds the stack: it
sorts by timestamp, breaks ties by position in the sequence, and closes a
slice on a `SLICE_END`. gcmon already emits a span as an adjacent pair on the
`Processes` track ([ADR-0011](0011-process-lifetime-and-ordering.md)), where a
fuzz suite checks it against the real trace processor.

## Consequences

- A trace an operator opens is unchanged, except that a `heap_size` counter
  track is named `Thread {iid} heap_size` where it was `heap_size`. A
  PerfettoSQL query matching `name = 'heap_size'` stops matching.
- A JSONL capture carries no `tid`, and one written before this change still
  reads: nothing read the field, and `from_mapping` rebuilds a record from its
  own fields.
- Adding a kind of row means a member on `Track` and a branch in the
  descriptor derivation; adding a kind of event means a member on
  `TraceEvent`, and the type checker finds every place that has to change.
- gcmon can no longer represent an unpaired end, or a pair whose ends cross.
  Both were possible.
- A record puts half as many slice events in the buffer: up to nine where
  there were up to eighteen.
- Packet order changes: a pid's thread descriptors arrive at each track's
  first slice rather than up front, and a pause's `SLICE_END` goes out
  directly after its own `SLICE_BEGIN`. The trace a reader gets is the same
  one, since the trace processor sorts.

## Alternatives considered

- **Delete the intermediate and emit Perfetto packets from the converter.** It
  costs the oracle in `tests/test_convert_cmd_perfetto.py`, which needs both
  halves to exist, and the encoder's unit-test seam. It also puts track state
  under the exporter's IO lock on every record rather than once per flush,
  since a converter emitting packets has to allocate uuids as it goes. The
  fact that would settle it differently: a second encoder never arriving *and*
  the oracle being retired.
- **Name what an event is about rather than the row it is drawn on.** A
  `LossTrack` would collapse into `InterpreterTrack` plus a flag, and the flag
  is the sentinel again.
- **Qualify `heap_size` only when a process has more than one interpreter.**
  Rejected as unimplementable rather than undesirable: gcmon is a streaming
  writer and does not know at descriptor time whether a sibling will appear.

## Implementation

- `src/gcmon/model/trace_event.py` holds the three track structs, the `Track`
  and `TraceEvent` unions, and `Slice` / `Instant` / `Counter`.
- `src/gcmon/exporters/perfetto_format.py` derives a track's descriptors,
  expands a `Slice` into its pair, and holds the top-level metric set.
- `src/gcmon/exporters/perfetto_track_state.py` keys its uuid tables on a
  `Track`.
- `src/gcmon/exporters/trace_converter.py` writes every display name.
- `src/gcmon/exporters/perfetto_process_lifetime.py` folds a slice into its
  pid's `Processes` span at both ends.
- Tests: `TestATrackIsDescribedOffTheEventsOnIt` in
  `tests/exporters/test_perfetto_format.py`; `TestTwoInterpretersHeapSizes` in
  `tests/exporters/test_perfetto_exporter_integration.py`;
  `TestMetaDedupRaceClosed` in
  `tests/exporters/test_exporter_thread_safety.py`; and
  `TestASliceExpandsIntoAPair` and `TestTheTraceProcessorBuildsTheNesting` in
  `tests/exporters/test_perfetto_slice_expansion.py`, the second asking the
  real trace processor about the ties the expansion rests on.
