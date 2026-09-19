# ADR-0004: Emit `heap_size` and `rss` as single top-level counters, outside the `GC Metrics` group

- **Status:** Superseded by
  [ADR-0024](0024-an-event-names-the-track-it-is-drawn-on.md)
- **Date:** 2026-06-27
- **Amended by:** [ADR-0027](0027-group-every-row-an-interpreter-owns.md)
- **Modules:** exporters

> The single-arg display-name rule and the top-level metric set holding both
> `heap_size` and `rss` are gone: the converter writes every display name, and
> a counter a `ProcessTrack` owns parents to the process track by
> construction. The body below is what the record still decides.

## What still holds

- **`heap_size` is its own counter event**, out of the per-generation payload,
  which carries `collected`, `candidates`, `duration` and `uncollectable`, the
  last only when non-zero. It has no per-generation meaning, and carried on
  that payload it drew one track per generation, each plotting the same
  process-wide number sampled at whichever generation happened to collect. One
  continuous series per `(pid, iid)` instead, latest value wins, which is the
  semantics for a process-wide gauge.
- **`rss` is one series per pid** and has no per-generation meaning either
  ([ADR-0013](0013-rss-sampling.md)). It parents to the OS-scoped process
  track, so the trace processor drops its `sibling_order_rank` and its
  position is a UI heuristic ([ADR-0003](0003-gc-metrics-group-track.md)).
- **`heap_size` is drawn outside the `GC Metrics` group.** Inside it, with
  `sibling_order_rank = 0`, the rank is honored and the metric renders first,
  but the group is collapsible, so the heap size stays hidden until someone
  expands it. Consolidating the metric was meant to make it easy to read. It
  sits on its interpreter's group instead, a plain custom track that honors
  the rank it carries there
  ([ADR-0027](0027-group-every-row-an-interpreter-owns.md)).
- **`heap_size` stays on the `GC Pause(N)` slice's args**, so it remains
  queryable per-pause from the slice `args` table.
