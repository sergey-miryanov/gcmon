# ADR-0002: Allocate track UUIDs sequentially and parent every track explicitly

- **Status:** Accepted
- **Date:** 2026-06-08
- **Amended by:** [ADR-0027](0027-group-every-row-an-interpreter-owns.md)
- **Modules:** exporters

## Context

Perfetto identifies every track by a 64-bit `uuid`, and
`TrackDescriptor.parent_uuid` builds the tree the UI renders. gcmon emits a
process track per monitored process, a group per interpreter holding that
interpreter's rows ([ADR-0027](0027-group-every-row-an-interpreter-owns.md)),
counter tracks, and the shared `Processes` lifetime track (see
[ADR-0011](0011-process-lifetime-and-ordering.md)).

An early design derived UUIDs arithmetically from the identifiers (process as
`pid | (1 << 60)`, thread as `(pid << 20) | iid | (1 << 60)`, counters from a
`3 << 60` base), on the theory that bit 60 marked "default" (OS-scoped) tracks
and that deterministic UUIDs were collision-free by construction. The scheme
was fragile: the bit-packing had to be re-derived for every new track kind,
`(pid << 20) | iid` collides for large pids, and it put an enormous varint in
every packet.

The descriptor layout was wrong in four further places, all presenting the
same way, with the Perfetto UI rendering gcmon traces in the wrong order while
the Chrome trace import looked right: `ProcessDescriptor` written at field 6
(`ChromeProcessDescriptor`) instead of field 3, thread UUIDs setting bit 61
instead of bit 60, process and thread descriptors carrying no ordering fields,
and counter tracks parented to the *thread* track, which pushed the thread
down below its own counters.

## Decision

**UUIDs are allocated sequentially from a per-trace counter starting at 1**,
lazily, on first use of each track. The state object maps identity (a
`Process`, a `Track`, a `Track` and a counter's display name) to the allocated
UUID, so the same track reuses its UUID across flushes. Collision-freedom
comes from the counter, not from bit arithmetic.

**`uuid = 0` is reserved.** Perfetto reads it as the root descriptor, and
gcmon writes one there to carry the `process_ordering` hint. Nothing parents
to it, and the allocator starts at 1, so no track takes the number instead.

**Descriptor layout:**

- Process: `ProcessDescriptor` at field **3**, with
  `child_ordering = EXPLICIT` so its children can be ordered.
- Interpreter groups: the interpreter list parents to the process track and an
  interpreter's own group to that, both with `child_ordering = EXPLICIT`.
  Every row an interpreter owns parents to its group and is ranked inside it
  ([ADR-0027](0027-group-every-row-an-interpreter-owns.md)). No track gcmon
  writes carries a `ThreadDescriptor`.
- Counters: a per-generation counter parents to the `GC Metrics` group
  ([ADR-0003](0003-gc-metrics-group-track.md)), `heap_size` to its
  interpreter's group (ADR-0027), and a counter a `ProcessTrack` owns to the
  process track ([ADR-0024](0024-an-event-names-the-track-it-is-drawn-on.md)).
- Track descriptors carry **no timestamp** on their containing `TracePacket`.
  Descriptors are time-independent.

**"Parented to the trace root" means the `parent_uuid` field is absent on the
wire** (`parent_uuid=None`, which the encoder skips), never `parent_uuid=0`.

## Consequences

- Adding a new track kind needs no bit-layout design: ask the state object for
  a UUID.
- UUIDs are not stable across runs or reproducible from a pid. Nothing depends
  on that; identity is carried by the descriptor's `pid` and `name` and by the
  parent chain, which is what the trace processor keys on.
- UUIDs stay small, so their varints stay short.
- The `1 << 60` bit-marking is gone. Perfetto's "default track" recognition
  comes from the presence of the `ProcessDescriptor` / `ThreadDescriptor`
  sub-message, not from the UUID value. That is what the field-6 and bit-61
  errors above turned on.

## Alternatives considered

- **Arithmetic UUIDs derived from pid/iid** (`pid | 1<<60` etc.). Rejected:
  deterministic but fragile. It requires a new bit-range per track kind,
  `(pid << 20) | iid` collides for large pids, and it encodes a large varint
  into every packet for no benefit.
