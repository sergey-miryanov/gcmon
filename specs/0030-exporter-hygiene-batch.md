# 0030: Close three small correctness hazards in the exporter package

- **Status:** Not started
- **Kind:** feature (cleanup)
- **Effort:** S
- **Origin:** post-v0.2.0 code review (old spec 18, REQ-2, 7, 9, 10, 11, 12,
  14)
- **Respects:**
  - [ADR-0001](../docs/adr/0001-hand-rolled-perfetto-protobuf-encoder.md):
    wire constants mirror the `.proto`; policy does not.
  - [ADR-0005](../docs/adr/0005-counter-y-axis-share-key.md)
  - [ADR-0029](../docs/adr/0029-report-liveness-and-fold-it-into-the-span.md):
    `_io_lock` serializes encoder state.

## 1. Problem statement

Three independent one-file changes, batched because each is too small to
schedule alone and all three live in the same package. Two more went with the
Chrome format ([0055](RETIRED.md)): the `getattr` probe was in its encoder,
and the duplicated format validation had nothing left to disagree about once
`combine` took one input. A third, 4.3, went with the code it was about. None
is operator-visible today; each is a way for a future change to go wrong
quietly. They are listed in section 4 in the order they should land, cheapest
first. Take them together or drop any one; nothing here depends on anything
else here.

## 2. Solution

No operator-visible change. Every item here is internal.

## 3. User stories

1. As a maintainer adding a counter metric, I want one place to add it and a
   compiler that objects if I misspell it, so that a typo does not silently
   rank the track at 0.
2. As a maintainer touching `PerfettoTrackState`, I want its threading
   contract written down, so that I do not add a call from outside `_io_lock`
   and corrupt uuid allocation.
3. As anyone reading `build_track_event`, I want its parameters not to shadow
   builtins, so that `type()` means `type()` inside the function.

## 4. Implementation decisions

**4.1: `_COUNTER_RANKS` becomes an enum.** The dict in `perfetto_format` maps
eleven metric names to `sibling_order_rank` values. A misspelled key silently
falls through `_COUNTER_RANKS.get(metric, 0)` to rank 0, which puts the track
at the top of the group and looks deliberate. Replace it with a
`CounterMetric(IntEnum)` whose member names are the metric names uppercased
and whose values are the current ranks, **preserved exactly**, including
`rss = 1`, which shifted every rank below it when RSS sampling landed
([ADR-0013](../docs/adr/0013-rss-sampling.md)). Lookup becomes a helper that
catches `KeyError` and returns 0, keeping today's tolerance for an unknown
metric. It stays in `perfetto_format` beside `_TOPLEVEL_COUNTER_METRICS` and
does **not** move to `perfetto_proto`: that module mirrors Perfetto's `.proto`
field numbers (ADR-0001) and ranks are our own layout policy.

**4.2: `PerfettoTrackState` states its threading contract.** It is not
internally thread-safe and does not need to be: every call reaches it through
`ProtobufEventEncoder.write_events` under `PerfettoExporter._io_lock`, and
`PerfettoExporter.add_process_liveness` takes that same lock explicitly for
this reason (ADR-0029). Add that to the class docstring. No locking, no
behaviour change.

`PerfettoExporter` holds `_io_lock` across the whole of a flush and has no
second lock: one lock guards the buffer and the encoder together, so a flush
reads the buffer and writes what it read in one critical section. ADR-0008
carries that, and `_flush_locked` is the one step that empties the buffer into
the encoder, so both call sites show the section they sit in, so 4.2 documents
`PerfettoTrackState` alone.

**4.3: dropped, 2026-08-26.** It asked `BufferedTraceExporter._build_meta` to
state the atomicity of its check-and-emit. There is no such method: no
producer decides what a batch's descriptors are any more
([ADR-0024](../docs/adr/0024-an-event-names-the-track-it-is-drawn-on.md)), and
no such class either
([ADR-0008](../docs/adr/0008-buffered-exporter-and-encoder-protocol.md)). The
guarantee it wanted written down is now `PerfettoTrackState`'s, which is what
4.2 covers. `TestMetaDedupRaceClosed` still stands and says in its own
docstring that the race closed by deletion. The number is kept rather than
reused, so 4.4 stays 4.4.

**4.4: `build_track_event` stops shadowing `type`.** Rename its first
parameter to `event_type`. It is re-exported from `perfetto_format` and called
from `perfetto_format`, `perfetto_builders` and `perfetto_process_lifetime`,
and it is called by name in the tests, so grep `src/` and `tests/` together.
Mechanical, but it is a keyword-argument rename and will fail loudly rather
than silently if a call site is missed.

**Also considered and deliberately not batched here:** making
`combine._normalize_trace_timestamps` return a new list instead of mutating in
place. The mutation is currently harmless (its only caller passes a list it
just built from `convert_to_trace_format`, so nothing else holds a reference)
and a non-mutating rewrite means constructing fresh structs for every event in
a combine run. It becomes worth doing the moment a caller shares the list;
until then the honest fix is a docstring saying it mutates, which 4.2's
reasoning already argues for. Fold it into 4.2.

**Not adopted at all:** importing `psutil` at the top of the module that reads
a command line, and making it a hard dependency. Graceful degradation without
`psutil` is a documented, tested property: the `[cmdline]` extra in
[docs/rss.md](../docs/rss.md), the fallback in
`gcmon.monitoring.process_registry`, and the same pattern in `rss_sampler`.
The lazy import is what makes it work.

## 5. Seams and testing decisions

- **Seam:** `tests/exporters/`, at each module's public function. Nothing here
  changes what a trace means, so the trace-processor seam has nothing to
  observe; the existing integration suite serves as the regression guard
  rather than the assertion.
- **New seam needed:** none.
- **What makes a good test here:** for 4.1, assert the enum's values against
  the eleven current ranks *by name*, so the test fails if a member is
  renumbered. A test that reads the ranks out of the enum and compares them to
  itself proves nothing. 4.2 and 4.4 are covered by the existing suite
  continuing to pass; do not add tests that assert a docstring exists.
- **Prior art:** `tests/exporters/test_perfetto_format.py` for the rank
  assertions.
- **Cases:**
  1. Each of the eleven metrics keeps its exact current rank; an unrecognized
     metric still ranks 0.
  2. Regression guard: the full Perfetto integration suite passes unchanged:
     no track moves, no field number changes.

## 6. Out of scope

- Anything that changes trace bytes. Every item here is internal.
- `psutil` as a hard dependency (see section 4, not adopted).
- Splitting `perfetto_format`, which is large enough to deserve it but not as
  part of a hygiene pass.
