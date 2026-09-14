# 0020: Record the target's Python version and GC thresholds in the trace

- **Status:** Not started
- **Kind:** feature (enhancement)
- **Effort:** M
- **Origin:** carried over from the pre-2026-08 spec set (old spec 20),
  rewritten
- **Respects:**
  - [ADR-0010](../docs/adr/0010-process-identity-cmdline-and-start-marker.md):
    cmdline as a debug annotation on the process slice.
  - [ADR-0011](../docs/adr/0011-process-lifetime-and-ordering.md): the
    `Processes` track and its slices.
  - [ADR-0021](../docs/adr/0021-write-one-trace-format.md): `--format` takes
    `perfetto`, `jsonl` and `stdout`, and a Perfetto-only feature is allowed
    to be Perfetto-only.

## 1. Problem statement

A trace from three weeks ago says a generation-0 collection ran 4,000 times.
Whether that is alarming depends entirely on the thresholds the process was
running with (700 is not 70) and on which Python built it. Neither is in the
file. Today both are logged to stderr at startup and lost with the terminal
scrollback, so the moment a trace outlives the session that produced it, the
numbers in it stop being interpretable. Comparing two traces from different
builds is worse: nothing in either file says they came from different builds.

## 2. Solution

Click the `Lifetime` bar on the process's own row in the Perfetto UI, and its
Args panel shows the Python version and the GC thresholds alongside the
command line that is already there. They travel with the file, so a trace
opened months later still says what it was measuring, and two traces can be
compared on the strength of what is in them rather than on someone's memory of
how they were produced.

Where the values cannot be known, they are absent rather than guessed: an
attach to a process that was not started by gcmon shows the command line it
shows today and no metadata, and the trace is otherwise identical.

## 3. User stories

1. As a developer profiling my own script with `gcmon run`, I want the trace
   to record which Python ran it, so that a trace I archive stays
   interpretable after I upgrade.
2. As an operator comparing GC behaviour across two builds, I want each trace
   to carry its own version, so that I do not have to trust a filename.
3. As an operator reading a collection count, I want the thresholds that
   produced it in the same panel, so that I can tell a tuned process from a
   default one.
4. As someone attaching to a production process that gcmon did not start, I
   want the trace to be valid and unchanged when metadata cannot be obtained,
   so that this feature never costs me a capture.
5. As someone running without the `[cmdline]` extra, I want the same graceful
   absence, so that an optional dependency stays optional.
6. As a maintainer, I want a value to be either correct or absent, so that
   nobody reads a threshold off a trace that was actually the monitoring
   process's threshold.
7. As a user of `--format jsonl` or `--format stdout`, I want my output
   byte-identical, so that a Perfetto-only feature stays Perfetto-only.

## 4. Implementation decisions

**Both values are emitted as string debug annotations on the
`TYPE_SLICE_BEGIN` packet of the `Lifetime` slice, on the process's own row**,
the same packet and the same mechanism as `cmdline` under ADR-0010, built in
`perfetto_process_lifetime` via `_build_debug_annotation_string`. Process
metadata has one home ([0067](RETIRED.md)), so a reader clicking a process's
row reads everything gcmon knows about it. No new track, no new packet, no
change to `_emit_process_descriptor`, and nothing added to the `Processes`
track's slices.

| Metadata | Annotation | Type | Example |
|---|---|---|---|
| Python version | `python_version` | string | `3.15.0b3 (tags/v3.15.0b3:cf16a33fad1) [MSC v.1943 64 bit (AMD64)]` |
| GC thresholds | `gc_thresholds` | string, JSON object | `{"0": 700, "1": 10, "2": 10}` |

`PerfettoTrackState` stores both per process, with getters and setters
following the ones already there for the descriptor and the command line. It
is not internally thread-safe and does not need to be; see
[0030](0030-exporter-hygiene-batch.md) section 4.2 for why.

**Each value has exactly one trustworthy source, and they are different
sources.** This is the decision the original spec left open, and getting it
wrong produces annotations that are confidently false:

- **`python_version` under `gcmon run`** is exact. `ChildProcessRunner` spawns
  the child with `sys.executable`, so the monitoring process's `sys.version`
  *is* the child's version. Inject it as `GCMON_PYTHON_VERSION` in the child's
  environment at spawn; the runner already builds a merged environment for the
  child, so this is one more key.
- **`gc_thresholds` is only knowable from inside the target**, and only as of
  a moment. The monitoring process's own `gc.get_threshold()` is not the
  child's: gcmon may have tuned its own, and the child may call
  `gc.set_threshold` at any point after start. Take it from the control plane:
  the target already talks to gcmon through `ControlClient`, so a process that
  opted in reports its own thresholds, and one that did not gets no
  annotation. Record the observation time in the value
  (`{"0": 700, "1": 10, "2": 10, "observed_ts": …}`) so a reader can see it is
  a sample rather than an invariant.
- **Under `gcmon <pid>` attach**, neither is knowable without cooperation. If
  the target was started by gcmon or carries the hook, the env var and the
  control plane supply them as above. Otherwise both are omitted. Do not infer
  a version from the executable path or the cmdline: a wrong version in the
  Args panel is worse than an empty one, because it will be believed.

**Rejected: sourcing thresholds from the monitoring process at spawn time.**
The original spec chose this "for v1" on the grounds that the child's startup
thresholds are the CPython defaults. If they are the defaults, the annotation
carries no information; if they are not, it is wrong.

**Rejected: a new `EventsExporter.add_process_metadata` method.** The
control-plane path can reuse the existing message routing; a new ABC method
obliges every exporter to acknowledge a Perfetto-only concern. Revisit only if
the control-plane message shape does not fit.

**Degradation is silent and total.** No `psutil`, no control connection, an
unreadable environment, a target that never reports: the annotation is
omitted, and nothing else about the trace changes. This matches how `cmdline`
already behaves.

## 5. Seams and testing decisions

- **Seam:** the trace processor, via
  `tests/exporters/test_perfetto_exporter_integration.py`. Debug annotations
  surface in the `args` table keyed `debug.python_version` /
  `debug.gc_thresholds`, exactly as `debug.cmdline` does today, the highest
  seam available, and the only one that proves the annotation is attached to
  the right slice rather than merely present in the byte stream.
- **New seam needed:** none for emission. The env-var injection needs an
  assertion at `ChildProcessRunner`'s environment-building step, which
  `tests/monitoring/test_child_process_runner.py` already reaches.
- **What makes a good test here:** query the annotation *through its slice*:
  join `args` to the `Lifetime` slice on a known pid's process track and
  assert the value. A test that greps the trace bytes for the string would
  pass on an annotation attached to the wrong slice, or to a packet the UI
  never renders. Assert the negative too: `--format jsonl` and
  `--format stdout` output stays byte-identical.
- **Prior art:** the `debug.cmdline` assertions in
  `tests/exporters/test_perfetto_exporter_integration.py`, which are this
  feature's exact shape, one annotation earlier.
- **Cases:**
  1. `gcmon run` produces a trace whose process slice carries
     `python_version`, and it matches the interpreter that ran the child.
  2. A target reporting thresholds through the control plane produces
     `gc_thresholds` on its own slice; two pids get their own values, not each
     other's.
  3. Neither source available: the trace is valid, the slice carries `cmdline`
     as before, and neither annotation is present.
  4. Regression guard: JSONL and stdout output byte-identical; the `Processes`
     track's slice count, spans and ordering unchanged.

## 6. Out of scope

- Metadata in JSONL or stdout. Perfetto-only, like `cmdline` (ADR-0010).
- Tracking threshold *changes* over a run. One observation, annotated with
  when it was taken. A `gc.set_threshold` mid-run is a real event worth
  drawing, and it is its own spec.
- Arbitrary user metadata (`--metadata key=value`). The annotation mechanism
  would extend to it; the CLI surface, precedence and escaping are a separate
  design.
- Updating `examples/perfetto_dump.py` to decode the new annotations.

## 7. Further notes

**To confirm when this is picked up:** whether `_remote_debugging` exposes the
target's version hex or GC thresholds directly. If it does, the attach case
stops needing cooperation and section 4's source table gets simpler. This is a
factual question about the extension module, not a preference, and it should
be answered before the control-plane path is built. See
[0024](0024-cpython-report-remote-readable-gc-stats.md), which reviews what
that API does and does not expose.
