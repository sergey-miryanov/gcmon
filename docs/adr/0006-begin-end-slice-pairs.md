# ADR-0006: Represent durations as Begin/End pairs in both backends

- **Status:** Superseded by
  [ADR-0024](0024-an-event-names-the-track-it-is-drawn-on.md)
- **Date:** 2026-06-14

> Superseded on 2026-08-26. Both halves of the argument below are gone: the
> two backends this reconciled became one
> ([ADR-0021](0021-write-one-trace-format.md)), and the pair carrying
> independent metadata at each end -- the thing a complete event could not
> express -- was never built, since an end carried only a track. A span is one
> `Slice` carrying both its ends now, and the encoder derives the pair the
> wire format still needs. What survives is the consequence this record ends
> on: duration is a property of the pair on the wire, and nothing downstream
> had to change.

## Context

The Chrome Trace Event format offers two ways to express a span. A "complete"
event (`ph: "X"`) carries a start timestamp and a duration in one record. A
begin/end pair (`ph: "B"` / `ph: "E"`) carries two records with two
timestamps.

gcmon's Chrome backend emitted `ph: "X"` with `ts` + `dur`. The Perfetto
backend emitted `TYPE_SLICE_BEGIN` / `TYPE_SLICE_END` pairs from the *same*
`TGCStatsInfo` fields, because Perfetto has no complete-event primitive.

The result was an impedance mismatch sitting between two backends that were
otherwise computing the same thing. Each had its own copy of the GC sub-phase
discovery logic (the `has_*` guards for mark-alive, deduce-unreachable,
handle-weakrefs, and the rest), the same name and category strings, and its
own arithmetic: one converting a pair of timestamps into `ts + dur`, the other
keeping them apart. Sharing that logic was impossible while the two sides
disagreed on the shape of a span.

## Decision

The Chrome backend emits begin/end pairs. `ph: "X"` is not produced.

- The event model's span types and their factories are begin/end pairs; the
  complete-event types they replaced are gone.
- The converter emits a begin/end pair per span rather than a single complete
  event.
- The Chrome trace reader parsed `ph: "B"` and `ph: "E"`; `ph: "X"` parsing
  was removed. That reader is gone with the format
  ([ADR-0021](0021-write-one-trace-format.md)).
- Timestamp normalization covers `"B"`, `"E"`, `"C"` and `"I"` events.

Begin/end is the shared primitive. Perfetto's model wins because it is the one
that cannot be expressed in terms of the other: a complete event is derivable
from a pair, but a pair carrying independent metadata at each end is not
derivable from a complete event.

## Consequences

- This was the enabling step for
  [ADR-0007](0007-shared-trace-converter-pipeline.md): with both backends
  agreeing on the primitive, the sub-phase discovery logic and the naming
  strings collapsed into one shared converter.
- A trace has roughly twice as many event records for the same spans. The
  files are larger. The Perfetto UI renders begin/end pairs natively, and
  nothing downstream had to change.
- A truncated or interrupted trace can end with an unmatched begin. The viewer
  tolerates this; a complete event could never be half-written.
- Duration is a property of the pair, not of a record. Nothing converts one.

## Alternatives considered

- **Make Perfetto synthesize complete events.** Not possible; the format has
  no such primitive.
- **Keep both shapes and translate at the boundary.** Rejected: that relocates
  the impedance mismatch without removing it, and it keeps the duplicated
  sub-phase logic that cost the most.

## Implementation

- `src/gcmon/model/trace_event.py` holds the begin and end event types and
  their factories.
- `src/gcmon/exporters/trace_converter.py` emits the pairs.
- `src/gcmon/analysis/combine.py` normalizes timestamps across `"B"`, `"E"`,
  `"C"` and `"I"`.
