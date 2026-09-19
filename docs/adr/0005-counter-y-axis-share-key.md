# ADR-0005: Use the metric name itself as `CounterDescriptor.y_axis_share_key`

- **Status:** Accepted
- **Date:** 2026-06-28
- **Amended by:** [ADR-0027](0027-group-every-row-an-interpreter-owns.md),
  [ADR-0028](0028-draw-every-process-a-row-of-its-own.md)
- **Modules:** exporters

## Context

`G0 collected`, `G1 collected` and `G2 collected` are three separate Perfetto
counter tracks plotting the same quantity for different generations. By
default each gets its own auto-scaled Y-axis, so a spike on G0 and a spike on
G1 look the same size even when they differ by two orders of magnitude.
Comparing generations means mentally re-scaling.

Perfetto solves this with `CounterDescriptor.y_axis_share_key` (field 7,
optional string): counter tracks that share a key **and** share a parent track
are rendered on one Y-axis range. gcmon was already emitting a
`CounterDescriptor` at `TrackDescriptor` field 8 for every counter track, but
it was always the empty submessage.

All per-generation counter tracks already share a parent, the
per-`(process, iid)` `GC Metrics` group from
[ADR-0003](0003-gc-metrics-group-track.md), so the "same parent" half of the
requirement holds and sharing is scoped to a single interpreter.

## Decision

**The share key is the metric name, verbatim.** `G0 collected`, `G1 collected`
and `G2 collected` all get `y_axis_share_key = "collected"`; the
per-generation `candidates`, `duration` and `uncollectable` tracks get their
own metric names.

There is **no lookup table**, on purpose. The grouped-counter emission path
sets `y_axis_share_key` to the metric name it already has, so any metric added
to the counter payload in future gets correct Y-axis sharing with no code
change. This is the whole point of keying on the name.

Two normalizations guard the edges:

- An empty share key is treated as an absent one: no field is emitted, and the
  `CounterDescriptor` stays the empty submessage. This defends against
  silently disabling sharing for a future metric with an empty name.
- A track that is not a counter ignores any share key passed to it; field 8 is
  not emitted at all.

When a share key is set, the `CounterDescriptor` submessage contains **only**
field 7. No other `CounterDescriptor` field (`type`, `categories`, `unit`,
`unit_multiplier`, `is_incremental`, `unit_name`) is written.

**`heap_size` and `rss` get no share key.** Neither is drawn inside the
`GC Metrics` group: `heap_size` sits in its interpreter's group
([ADR-0027](0027-group-every-row-an-interpreter-owns.md)) and `rss` on the
process track, where the `ProcessTrack` that owns it puts it
([ADR-0024](0024-an-event-names-the-track-it-is-drawn-on.md)), so neither has
a peer to share an axis with. A key there would be a no-op, and omitting it
keeps the wire format minimal.

## Consequences

- Generation-to-generation magnitude comparison is readable without
  re-scaling.
- Y-axis sharing and sibling ordering are independent features; both are
  preserved.
- Sharing crosses neither interpreters nor processes, because each
  `(process, iid)` has its own `GC Metrics` group and Perfetto requires a
  shared parent, and two processes that held one pid count as two
  ([ADR-0028](0028-draw-every-process-a-row-of-its-own.md)). That is the
  documented scope of the feature.
- Older trace processors ignore the unknown field, so no write-time version
  gate is needed.
- **The SQL-level tests read the key from the stdlib table the UI builds its
  TrackEvent rows from**, `_track_event_tracks_ordered_groups` in
  `viz.summary.track_event`, because `counter_track` has no `y_axis_share_key`
  column. The wire-level tests hold the bytes.

## Alternatives considered

- **A lookup table mapping metric to share key.** Rejected: it duplicates the
  metric name and needs an edit whenever someone adds a metric. Forget the
  edit and that metric silently loses its shared axis.
- **A share key on `heap_size` / `rss` for forward-compatibility.** Rejected:
  no peers, so it is bytes on the wire that do nothing.
- **Setting `unit` / `unit_name` at the same time.** Deferred to a separate
  change; a wire-level test locks the current minimal submessage, so the scope
  creep would be caught.
