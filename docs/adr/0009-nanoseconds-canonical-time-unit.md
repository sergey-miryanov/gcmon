# ADR-0009: Store `TraceEvent.ts` in nanoseconds; convert at the encoder

- **Status:** Accepted
- **Date:** 2026-06-25
- **Amended by:** [ADR-0024](0024-an-event-names-the-track-it-is-drawn-on.md)
- **Modules:** exporters, model, support

## Context

`TGCStatsInfo` records timestamps in nanoseconds, on the clock
`time.monotonic_ns()` reads
([The clock a GC record is stamped from](../internals/gc-record-clock.md)).
The `TraceEvent` model stored microseconds, so the converter divided each
timestamp field down on the way in.

That was fine while Chrome was the only backend: the Chrome Trace Event format
is specified in microseconds, so the model matched the output.

It broke when Perfetto arrived. `TracePacket.timestamp` is a **nanosecond**
field, and `ProtobufEventEncoder` wrote microseconds straight into it,
compressing the whole timeline of every `.pftrace` by a factor of 1000. The
failure was invisible because the compression was uniform: relative shape
looked right, and the chrome↔perfetto equivalence test compared
`chrome_dur // 1000 == perfetto_dur`, encoding the discrepancy as if it were
expected.

One model unit shared across backends with different wire units has no correct
answer. The question is where the conversion belongs.

## Decision

`TraceEvent.ts` is nanoseconds, and so are the two ends a `Slice` carries.
`TGCStatsInfo` values flow through the converter unchanged.

**Conversion happens at the encoder, once, per format.**
`ProtobufEventEncoder` writes `event.ts` directly, since
`TracePacket.timestamp` is nanoseconds. An encoder for a format in another
unit converts as it serializes, which the Chrome encoder did until the format
went ([ADR-0021](0021-write-one-trace-format.md)).

The in-memory model uses the source's unit, and each encoder owns the unit its
own wire format demands. If you are asking which unit a timestamp is in:
nanoseconds, unless you are looking at bytes on disk.

## Consequences

- Perfetto traces have correct timelines.
- **There is no migration path for existing `.pftrace` files.** Traces
  captured before this change are 1000× compressed and cannot be corrected
  after the fact, because the original precision is not recoverable from the
  file. Re-capture.
- Nothing in gcmon converts a timestamp: the one encoder writes nanoseconds.

## Alternatives considered

- **Keep microseconds internally and multiply by 1000 in the Perfetto
  encoder.** Rejected: it would restore the precision `TGCStatsInfo` already
  carries only by inventing zeros in the low three digits, and it keeps the
  model's unit different from every source it reads.
- **Store both units on the event.** Rejected: two fields that must agree will
  disagree eventually, and it doubles the struct.
- **A nanosecond mode for the Chrome output.** Out of scope. The Chrome Trace
  Event format is a public spec with microsecond timestamps; deviating would
  break the viewers.
