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

**`ProtobufEventEncoder` owns byte production** through three methods:
`open(path)`, `write_events(events)`, `close()`. It is a class of its own
because `combine` drives it with no exporter around it.

The exporter constructs its encoder, and its public constructor signature is
unchanged.

**No protocol is declared over the encoder.** One format leaves one
implementation ([ADR-0021](0021-write-one-trace-format.md)), and both callers,
`PerfettoExporter` and `combine`, name `ProtobufEventEncoder`. What the split
defends is the encoder being a separate class that runs with no exporter, no
buffer and no lock around it, and a class boundary needs no declaration.

**One lock guards the buffer and every touch of encoder state.** It is held
across deciding what to write and writing it, so encoder state is touched in
the order the calls arrived in.

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

- A second output format is a second encoder class with the same three
  methods. No lifecycle, locking or dedup code to copy.
  [ADR-0021](0021-write-one-trace-format.md)'s
  `combine --output-format perfetto` reuses `ProtobufEventEncoder` directly,
  outside any exporter.
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
  seen-pid set and the atomic check-and-emit, so what is left to merge is a
  buffer, a lock and four one-line calls.
- **A second lock for the buffer alone.** Rejected: it leaves a window between
  taking events out of the buffer and writing them. A retirement reaching the
  encoder inside that window draws a process's span from an accumulator those
  events have not reached, leaving it short at whichever end they would have
  moved ([ADR-0011](0011-process-lifetime-and-ordering.md)). The one lock
  costs an append the wait for a write already in progress.
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
