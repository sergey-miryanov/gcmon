# ADR-0004: Emit `heap_size` and `rss` as single top-level counters, outside the `GC Metrics` group

- **Status:** Superseded by
  [ADR-0024](0024-an-event-names-the-track-it-is-drawn-on.md)
- **Date:** 2026-06-27, amended:
  - 2026-07-13: `rss` added

> Superseded on 2026-08-26. The single-arg display-name rule and the top-level
> metric set holding both `heap_size` and `rss` are gone: the converter writes
> every display name, and a counter a `ProcessTrack` owns parents to the
> process track by construction. Three things below still hold: one
> `heap_size` series per `(pid, iid)` and one `rss` per pid, `heap_size` drawn
> outside the `GC Metrics` group, and `heap_size` staying on the
> `GC Pause(N)` slice args. It is drawn on its interpreter's own group rather
> than a level up beside the process, so the rank it carries there is honored
> ([ADR-0027](0027-group-every-row-an-interpreter-owns.md)).

## Context

`heap_size` was originally carried on the per-generation counter payload,
alongside `collected`, `candidates` and `duration`. Because the encoder
materializes one counter track per `(track name, metric)` pair, that produced
three tracks (`G0 heap_size`, `G1 heap_size`, `G2 heap_size`) all plotting the
same process-wide number, sampled at whichever generation happened to collect.
Reading heap size over time meant mentally merging three partial series.

`heap_size` has no per-generation meaning. Neither does RSS
([ADR-0013](0013-rss-sampling.md)), which arrived later with the same shape: a
process-level value with no generation and no thread affinity.

Naming is a second constraint on any fix. The encoder names a counter track
`f"{event_name} {metric}"`, so a counter event named `heap_size` carrying a
single arg keyed `heap_size` would produce the track name
`heap_size heap_size`.

## Decision

**`heap_size` is emitted as its own counter event**, named `heap_size` and
carrying a single arg of the same name, separate from the per-generation one.
It is removed from the per-generation counter payload, which now carries only
`collected`, `candidates`, `duration`, and `uncollectable` (the last only when
non-zero). All generations on a `(pid, tid)` feed the same single `heap_size`
track; latest value wins on the time axis, which is the correct semantics for
a process-wide gauge.

**Single-argument counter events use the metric as the display name.** When a
counter event carries exactly one arg, the Perfetto track name is the metric
alone (`heap_size`, not `heap_size heap_size`); the Chrome encoder blanks the
event name for the same reason, since Chrome's trace processor derives the
track name as `f"{event_name} {arg_key}"`.

**One set of metric names is the switch**, holding `heap_size` and `rss`.
Metrics in it are parented directly to the process track, outside the
collapsible `GC Metrics` group. Adding a metric to the set moves it out of the
group.

`heap_size` stays on the `GC Pause(N)` slice's args as well, so it remains
queryable per-pause from the slice `args` table.

## Consequences

- One continuous `heap_size` series per `(pid, tid)`, and one `rss` series per
  pid.
- **Accepted trade-off:** because these tracks are parented to the OS-scoped
  process track, the trace processor drops their `sibling_order_rank` (the
  rule from [ADR-0003](0003-gc-metrics-group-track.md)). Their position is a
  UI heuristic; in the Perfetto UI they render *below* the `GC Metrics` group.
  A rank for `heap_size` is retained for documentation and
  forward-compatibility but is not honored.
- **Chrome-consumer break:** the converter now emits two `C` events per item
  rather than one. Downstream Chrome-trace tooling that assumed a single
  counter event per GC pause, and read every metric from it, must read the
  consolidated event separately. No consumer in this repository made that
  assumption.
- New process-level metrics need only be added to the top-level set; the
  naming and parenting fall out.

## Alternatives considered

- **`heap_size` inside the `GC Metrics` group with `sibling_order_rank = 0`.**
  Tried in the Perfetto UI. The rank *is* honored there and `heap_size`
  renders first inside the group, but the group is collapsible, so the heap
  size stays hidden until the user expands it. Consolidating the metric was
  meant to make it easy to read. Rejected in favour of a standalone
  always-visible track; the cost is the dropped rank described above.
- **Cross-thread or cross-process consolidation** (one `heap_size` track per
  pid rather than per `(pid, tid)`). Out of scope; per-`(pid, tid)` matches
  how the interpreter reports the value.
- **`tid = 0` for the process-level RSS counter.** Rejected; see
  [ADR-0013](0013-rss-sampling.md).
- **Smoothing or aggregating samples.** Rejected; "latest value wins" is
  standard Perfetto counter semantics and keeps the exporter stateless.

## Implementation

- `src/gcmon/exporters/trace_converter.py` builds the per-generation counter
  payload without `heap_size`, and emits the consolidated `heap_size` event
  beside it.
- `src/gcmon/exporters/perfetto_format.py` holds the top-level metric set and
  parents those tracks directly to the process track.
- Tests: `tests/exporters/test_perfetto_exporter_integration.py` asserts
  exactly one `heap_size` track and zero `G{N} heap_size` tracks, and
  `tests/test_convert_cmd_perfetto.py` pins the whole set of counter track
  names a combined trace carries.
