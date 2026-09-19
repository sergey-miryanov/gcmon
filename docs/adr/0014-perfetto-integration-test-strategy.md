# ADR-0014: Validate traces against the real trace processor; deselect slow suites by marker

- **Status:** Accepted
- **Date:** 2026-06-12
- **Amended by:** [ADR-0026](0026-two-subsystems-over-a-shared-base.md)
- **Modules:** tests

## Context

[ADR-0001](0001-hand-rolled-perfetto-protobuf-encoder.md) explains why gcmon
hand-rolls its Perfetto encoder, and what that costs: field-number drift and
message-layout mistakes fail *silently*. The trace still parses; it renders
wrong. Three such bugs shipped, and each time someone found them by opening a
trace in the UI.

Unit tests could not have caught them. A round-trip test reads a value back
through the same constant it wrote with, so it is equally happy with a correct
and an incorrect field number. Only the trace processor can settle whether a
trace means what you think it means, and it is the same binary the Perfetto UI
runs in the browser.

Separately, `ControlClient` is the child-side IPC surface used by each
monitored process. Its send and close paths are locked, and the *server* side
had a thread-safety suite, but the client side had no regression guard.

## Decision

**Traces are validated by loading them into the real trace processor and
asserting on the SQL tables** it exposes (`slice`, `args`, `track`,
`counter_track`, `process`, `thread`). The `perfetto` package is a **dev-group
dependency**, so it never enters the runtime tree, and it is used only on the
read side, via `perfetto.trace_processor.TraceProcessor`. gcmon's own encoder
remains hand-rolled per ADR-0001.

**Trace-processor tests run in the default suite.** They sit behind no marker
and are not skipped when the package is missing: `perfetto` is a required dev
dependency, so a developer running `pytest` has it, and the tests import it at
module level. The first run downloads the trace-processor binary; later runs
use the cache.

**Only the optional suites are gated by marker**, registered in
`pyproject.toml` and deselected by the default `addopts`. The commands and the
CI jobs are in [testing](../testing.md); each marker exists for its own
reason.

- `stress` is probabilistic, so it earns repetition rather than a place in a
  suite that has to pass on every run.
- `benchmark` measures where the rest of the suite asserts.
- `fuzz` earns a marker for cost, not flakiness. Seeds are fixed, so a failure
  reproduces and repeating a trial re-runs the same trace. The trace processor
  starts once per trial, which is seconds rather than the milliseconds the
  default suite budgets for.
- `architecture` is deselected for the opposite reason to the other three
  ([ADR-0026](0026-two-subsystems-over-a-shared-base.md)). It costs nothing
  and answers a question about structure rather than behaviour, which the rest
  of the suite cannot fail on.

**The trace is asserted against the events it was built from.** While gcmon
wrote two formats, the suites were parametrized over Chrome JSON and Perfetto
binary, and each format's reading was the other's oracle. With one format left
([ADR-0021](0021-write-one-trace-format.md)) the oracle is the
`list[TraceEvent]` the trace was built from: the trace processor reads the
`.pftrace` back through a decoder gcmon did not write, and slice names,
nanosecond durations and arg values are compared against the events. That
catches a wrong field number, which expectations written from gcmon's own
constants cannot.

**Trace processor instances are per-test, not session-scoped.** An instance
loads one trace and cannot easily be reused for another. Per-test instances
are sub-second on a warm binary cache and keep tests isolated.

**Test data is synthetic and minimal.** One `GCStatsInfo` with each known
argument populated exercises the whole structure and each field-number code
path. Real captured batches are slower and add noise without reaching new
code.

## Consequences

- CI catches field-number and schema-drift bugs on each run, rather than
  leaving them for a user to hit.
- The cost is that `pytest` needs the trace-processor binary. An earlier
  design put these tests behind an `integration` marker with
  `pytest.importorskip`, so they were skipped by default, which meant they
  seldom ran at all. Tests that never run catch nothing, and the download is a
  smaller price.
- A wire-format regression test and a trace-processor test both cover the same
  behaviour, on purpose: the wire test is fast, dependency-free, and asserts
  the exact byte-level invariant, while the trace-processor test is the
  end-to-end net. Neither subsumes the other.
- These tests replaced the manual "open it in ui.perfetto.dev and look"
  acceptance step. Running the trace processor covers strictly more, since
  that is what the UI runs.
- **Some behaviour is not in the core tables.** `sibling_order_rank`
  ([ADR-0011](0011-process-lifetime-and-ordering.md)) and `y_axis_share_key`
  ([ADR-0005](0005-counter-y-axis-share-key.md)) are UI rendering hints with
  no column in `track` or `counter_track`. The stdlib table the UI builds its
  TrackEvent rows from carries both, the rank as the `order_id` it sorts a
  row's children on, and the tests read them there. A process track's rank
  shows in no table, so the tests touching it are schema-validity guards.
- Stress tests are the only probabilistic tests in the suite, so they are the
  only ones whose failures can depend on how loaded the runner is.

## Alternatives considered

- **Add `perfetto` to runtime dependencies.** Rejected: ADR-0001 exists to
  keep it out of the runtime tree. Dev-group membership gives the tests what
  they need and ships nothing extra.
- **Keep the trace-processor tests behind an `integration` marker with
  `importorskip`.** Rejected: deselected by default they seldom run, and
  nothing else in the suite catches the bugs they exist for.
- **Session-scoped trace processor fixture.** Rejected: one instance holds one
  trace; sharing it means either reloading anyway or coupling every test to a
  single fixture trace.
- **Real captured GC data as test input.** Rejected: slower, noisier, and it
  exercises no code path that one fully-populated synthetic item does not.
- **Stress tests for `ControlClient`'s lazy-reconnect and failure-recovery
  contracts.** Rejected as redundant: the `ControlClient` unit tests already
  assert that a close followed by a send reconnects silently, and that
  `BrokenPipeError` clears the connection for the next call. Those assertions
  are stricter than "no exception under contention."
