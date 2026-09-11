# ADR-0003: Parent per-generation counters to a non-OS-scoped `GC Metrics` group track

- **Status:** Accepted
- **Date:** 2026-06-27, amended:
  - 2026-09-01: the track key became per process, see
    [ADR-0011](0011-process-lifetime-and-ordering.md)
  - 2026-09-11: `GC Metrics` moved onto the interpreter group, see
    [ADR-0027](0027-group-every-row-an-interpreter-owns.md)

## Context

gcmon emits several counter tracks per generation per thread (`G0 collected`,
`G0 candidates`, `G0 duration`, `G0 uncollectable`, and the same for G1 and
G2). The list is long, and an arbitrary order makes it hard to compare one
metric across generations.

Perfetto's mechanism for this is `TrackDescriptor.child_ordering = EXPLICIT`
on the parent plus `sibling_order_rank` on each child. gcmon set both,
parenting the counter tracks directly to the process track, and the trace
processor ignored the ordering: against the build bundled with `perfetto`
0.56.0, the `track` SQL table came back flattened, with `parent_id = NULL` on
every counter row.

The cause is a rule in `track_descriptor.proto`, on the `child_ordering`
field:

> Note: for tracks where `thread` or `process` are set (i.e. process tracks
> and thread tracks), this option is *ignored*. Instead, use `thread_ordering`
> and `process_ordering` on the root track descriptor (uuid = 0) to configure
> process and thread track ordering.

The process track carries a `process` sub-message, which makes it OS-scoped,
so both the parent's `child_ordering` and the children's `sibling_order_rank`
are discarded. An earlier iteration read the flattened table as "the trace
processor does not honor `sibling_order_rank` for counter tracks" and gave the
requirement up as unachievable. **The parent's type is what breaks the
ranking, not the rank field.**

## Decision

Insert an intermediate **non-OS-scoped grouping track named `GC Metrics`**,
one per `(process, iid)`, parented to that interpreter's own group
([ADR-0027](0027-group-every-row-an-interpreter-owns.md)), carrying
`child_ordering = EXPLICIT` and no `process` / `thread` / `counter`
sub-message. Every per-generation counter track's `parent_uuid` points at this
group instead of at the process track.

Because the group is a plain custom track, the trace processor honors
`child_ordering` on it and `sibling_order_rank` on its children. Counter rows
now carry a non-NULL `parent_id` pointing at the `GC Metrics` row, and the
ranking takes effect *inside* the group.

Ranks come from a single ordered table covering each metric: `collected`,
`uncollectable` (emitted only when non-zero), `candidates`, `duration`, and
the rest. `rss` has an entry too, though a `ProcessTrack` draws it where the
rank is discarded ([ADR-0004](0004-toplevel-shared-counters.md)); `heap_size`
has none, because its interpreter's group ranks it
([ADR-0027](0027-group-every-row-an-interpreter-owns.md)). Inserting a metric
shifts the ranks below it, which is fine: only the relative order matters.

## Consequences

- Counter ordering inside the group works and is stable.
- **Accepted trade-off:** the same proto rule that breaks ranking under
  OS-scoped parents also governs rendering. Per the `parent_uuid` back-compat
  note, a track whose parent is OS-scoped "inherits the parent's
  process/thread association and will appear as a *sibling* of the parent."
  The row that pays it is the `Interpreters` group, the one custom track a
  process track parents (ADR-0027): it renders *alongside* the `Process <pid>`
  track in the UI rather than nested inside it, and the rows below it nest
  normally. The spec owner reviewed this and accepted it, since ordering
  within the group still works.
- The group is collapsible, which keeps the top-level track list short. That
  is why `heap_size` is drawn *outside* the group
  ([ADR-0004](0004-toplevel-shared-counters.md), carried forward by
  [ADR-0024](0024-an-event-names-the-track-it-is-drawn-on.md)).
- Any new per-generation metric inherits the grouping for free. What sits
  outside it is `heap_size` and whatever a `ProcessTrack` owns, `rss` among
  them (ADR-0024).

## Alternatives considered

- **`child_ordering = LEXICOGRAPHIC` with name prefixes**
  (`0_heap_size`, `1_collected`, …). Rejected: it works, but the prefix shows
  up in the track name the user reads.
- **`process_ordering` on the root descriptor (`uuid = 0`).** Not applicable
  here: it orders process tracks against each other and says nothing about
  counters. It is used, for the purpose it is meant for, in
  [ADR-0011](0011-process-lifetime-and-ordering.md).
- **Leave counters parented to the process track and accept arbitrary order.**
  Rejected; this is what the earlier iteration did, and the counter list is
  long enough that the order is worth fixing.

## Implementation

- `src/gcmon/exporters/perfetto_format.py` names the group track, emits its
  descriptor once per `(process, iid)` with a docstring recording *why* the
  group is a plain custom track, holds the rank table, and parents each
  per-generation counter to the group UUID.
- Tests: `tests/exporters/test_perfetto_format.py` covers the parenting;
  `tests/exporters/test_perfetto_exporter_integration.py` asserts the counter
  rows' `parent_id` is non-NULL and equals the group row, the assertion that
  would have caught the original bug.
