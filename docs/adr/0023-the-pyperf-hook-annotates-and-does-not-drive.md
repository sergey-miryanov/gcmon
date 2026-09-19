# ADR-0023: Mark the benchmark from the pyperf hook, and drive nothing

- **Status:** Accepted
- **Date:** 2026-08-24
- **Amended by:** [ADR-0024](0024-an-event-names-the-track-it-is-drawn-on.md)
- **Modules:** model, pyperf

## Context

`--hook=gcmon` used to run gcmon for you. pyperf builds a hook inside each
measurement phase and a phase runs twice per worker process, once for warmups
and once for values, so each build spawned a `gcmon monitor`, attached it to
the worker, opened a control pipe and wrote a temporary capture of its own. A
sixty-benchmark suite at `-p 5` was on the order of six hundred monitors, and
each one reopened the same output path for writing, so all but the last were
overwritten.

Around the benchmark the hook started and stopped the monitor through the
control plane. Stopping suppresses gcmon's polling of that pid and nothing
else: the target keeps collecting, the records made during the gap are still
in the ring when polling resumes, and the cumulative counters a capture is
reconstructed from span the gap whole. The numbers the hook published were the
pause over a window wider than the benchmark by every gap in it, and
`docs/pyperf.md` told the operator they covered the monitored window.

The operator wanted one thing the hook did not produce: a way to tell a
benchmark's own activity from the interpreter starting up, importing,
calibrating, and pyperf's bookkeeping between values.

## Decision

- **The hook annotates a trace it did not start.** It spawns no process,
  writes no file, computes no statistics, and adds no key to pyperf's
  metadata. It reaches the monitor through `GCMON_CONTROL_ADDRESS`.
- **A benchmark's extent is two instants.** The hook writes a begin and an end
  mark per measured region, named under a prefix reserved to gcmon:
  `gcmon:<benchmark>:<n>:<i>:begin` and `:end`.
- **The hook captures a mark in the region and sends it after.** It reads a
  clock at each end and holds the pair; `ControlClient.instant_msg` takes the
  captured timestamp, and the send happens at teardown, the first moment
  pyperf names the benchmark.
- **A region carries two numbers.** `<n>` counts across the worker process and
  `<i>` within the hook.
- **The hook refuses to run without a monitor.** The constructor connects and
  raises pyperf's `HookError`, caught by its loader to print one message and
  exit 1.
- **One module writes the grammar and reads it.** It sits in `model`, below
  both the hook that writes a mark and anything that would read one.

## Consequences

A run costs one monitor however many benchmarks and processes it has, and one
trace holds every worker's marks alongside its GC activity.

You decide the region after the run, not during it. A benchmark cannot be
re-run to change your mind about where its boundaries were, and the marks mean
you do not have to.

Marks reach the exporter out of order with respect to records, by seconds
rather than milliseconds. ADR-0011 and ADR-0029 cover that: the trace
processor sorts by timestamp, and a freshly discovered child's first event can
predate gcmon polling it.

One module writing and reading the grammar means a round trip agrees with
itself on a changed separator, so the grammar is pinned as a literal string
instead.

pyperf's metadata holds nothing to trend across this change, and nothing in
tree reads the marks: until a reader exists they are for the Perfetto UI.

A mark and a GC record have to come from one clock, the one
`time.monotonic_ns` reads
([The clock a GC record is stamped from](../internals/gc-record-clock.md)). If
CPython ever stamps a record from another, every mark is misplaced and nothing
downstream catches it.

## Alternatives considered

**Repair the gate.** Making start/stop scope a region means polling at both
edges, subtracting the counters across the gap, and dropping whatever the ring
holds on resume; the observed span becomes a set of intervals. A repaired gate
still destroys the gap's records while counting their cost, and marking keeps
them. Gating saves only gcmon's own polling cost, and gcmon is external, so
the benchmark never paid it.

**Number a region once.** A worker builds one hook per measurement phase and
every one is handed the same benchmark name, so a count scoped to the hook
would put one mark name on two different regions. A count scoped to the
process is unique, but it runs straight through both phases and cannot say
where one ended.

**Warn and carry on.** The hook could log that it found no monitor and let the
run proceed. A control client with no address never connects, so every send
goes nowhere: the suite finishes having recorded nothing, and the operator
finds out when they open the trace.

**Monitor from inside the worker.** The remote debugging interface accepts the
caller's own pid, so an in-process hook is possible: it would read its own
ring and need no second process. A free-threaded build sizes the ring at one
record per generation, so keeping up means a thread polling hard enough that
every read takes the GIL inside the process being benchmarked. gcmon reads
from outside for that reason, and `--rate` spends the monitor's time rather
than the target's.

**Carry the fields as annotations.** An instant could take a payload of keys,
and a reader would join the `args` table instead of parsing a string. The
encoder already writes `debug_annotations` for the GC pause slices, and an
`Instant` has carried args since
[ADR-0024](0024-an-event-names-the-track-it-is-drawn-on.md), so what is left
is a payload reaching every exporter's `add_instant_event` and surviving the
JSONL round trip `combine` reads. That touches the exporter protocol and the
capture format, not the hook, and gets a spec of its own.

**Draw a region as a slice.** A slice would pair the two ends in the model,
where a reader now matches two names. It needs a track to sit on, and where a
benchmark's track belongs in the hierarchy is a larger decision than this
record makes.

**Recover the benchmark name from the worker's command line.** A regex over
`sys.argv` would name the region without waiting for teardown, at the price of
a second copy of the sanitizer, in another language, with nothing keeping the
two in step.
