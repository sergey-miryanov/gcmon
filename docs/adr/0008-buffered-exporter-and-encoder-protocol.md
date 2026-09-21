# ADR-0008: Split the exporter's lifecycle from the encoder that writes the bytes

- **Status:** Accepted
- **Date:** 2026-06-14
- **Amended by:** [ADR-0021](0021-write-one-trace-format.md),
  [ADR-0024](0024-an-event-names-the-track-it-is-drawn-on.md)
- **Modules:** exporters

## Context

`TraceExporter` (Chrome) and `PerfettoExporter` each independently implemented
the same lifecycle: a two-lock model (one for state, one for I/O),
`flush_threshold`-based buffering, the `add_event` / `add_instant_event` /
`close` sequence, and per-pid/iid deduplication of `ProcessMeta` /
`ThreadMeta`.

Only the byte production differed: the Chrome side's `[\n … \n]\n` bracket
dance versus the Perfetto side's `"wb"`-then-`"ab"` file-mode toggle. The two
exporters duplicated everything around it.

Both copies also carried the same bug. The meta-dedup did its "have I seen
this pid?" check and its emit in separate critical sections, so two threads
adding events for a brand-new pid could both pass the check and both emit a
`process_name` event, putting a duplicate process descriptor in the output.

## Decision

**`PerfettoExporter` owns the lifecycle:** the lock, the buffer and flush
threshold, and `add_event` / `add_instant_event` / `close`.

**`ProtobufEventEncoder` owns byte production** through `open(path)`,
`write_events(events)` and `close()`. It is a class of its own because
`combine` drives it with no exporter around it.

The exporter constructs its encoder, and its public constructor signature is
unchanged.

**No protocol is declared over the encoder.** One format leaves one
implementation ([ADR-0021](0021-write-one-trace-format.md)), and both callers,
`PerfettoExporter` and `combine`, name `ProtobufEventEncoder`. What the split
defends is the encoder being a separate class that runs with no exporter, no
buffer and no lock around it, and a class boundary needs no declaration.

**One lock guards the buffer and every touch of encoder state.** A flush holds
it across both deciding what to write and writing it.

**Meta building is atomic.** The check and the emit happen inside a single
critical section under that lock, which is what closes the race between two
threads reaching a brand-new pid.

The split settles further questions:

- **One place holds the seen-pid set**, `PerfettoTrackState` in the encoder
  ([ADR-0024](0024-an-event-names-the-track-it-is-drawn-on.md)). Exactly one
  `ProcessMeta` per pid reaches the wire, a cmdline is registered at most
  once, and nothing needs a double-checked-locking dance around that
  registration.
- **The encoder records a command line rather than reading one.** The monitor
  reads it once, where it creates the process
  ([ADR-0010](0010-process-identity-cmdline-and-start-marker.md),
  [ADR-0025](0025-create-every-process-in-one-place.md)). A slow
  `psutil.Process(pid).cmdline()` call inside the encoder would run either
  under the I/O lock, serializing one call per pid, or outside every lock and
  back into double-checked locking.
- **A run with nothing in it writes no file at all.**

## Consequences

- A second trace format is a second encoder class with those same methods. The
  lifecycle and the locking stay with the exporter, so neither gets copied.
  The dedup is the encoder's own since
  [ADR-0024](0024-an-event-names-the-track-it-is-drawn-on.md), so a new
  encoder writes that part itself.
- Output bytes were unchanged by the refactor, verified by the existing
  structural tests, which decode the output and assert on each meaningful
  field.
- `close()` is idempotent.
- **`add_event` after `close()` silently drops the event.** This matches the
  pre-refactor behaviour, and the alternative is raising from a monitoring
  callback during shutdown.
- A cmdline provider failure costs the descriptor its cmdline and nothing
  else. A `psutil` error never costs you the trace.

## Alternatives considered

- **A buffering base class the exporters share.** Rejected: with one exporter
  ([ADR-0021](0021-write-one-trace-format.md)) the base has a fan-out of one.
  Meta building sits in the encoder
  ([ADR-0024](0024-an-event-names-the-track-it-is-drawn-on.md)), with the
  seen-pid set and the atomic check-and-emit, so what is left to merge is the
  buffer and the lock around it.
- **A second lock for the buffer alone.** Rejected: a flush would empty the
  buffer under one lock and write under the other, with a gap between the two.
  A retirement can land in that gap and draw the process's `Lifetime` bar from
  the span accumulator. The events still in flight have not reached the
  accumulator, so the bar comes out short at whichever end those events would
  have moved ([ADR-0029](0029-report-liveness-and-fold-it-into-the-span.md)).
  The price of one lock is that an append waits for a write already running.
- **A `Protocol` declared over the encoder.** Rejected: with one format there
  is one implementation and nothing typed against the protocol.
- **A common base class with abstract encode methods instead of a separate
  encoder object.** Rejected: composition lets the encoder run without an
  exporter, which is what `combine` needs.
- **Keep cmdline registration outside the lock.** Rejected: it required the
  double-checked locking that atomic meta building makes unnecessary, and the
  call now happens once per pid, so the serialization is not worth the
  complexity.
- **Fold `JsonlExporter` / `StdoutExporter` into the same lifecycle.**
  Rejected: they consume raw `TGCStatsInfo`, not `TraceEvent`, so the data
  shapes differ.
