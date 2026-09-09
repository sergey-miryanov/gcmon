# ADR-0025: Create every process in one place, and carry it instead of a pid

- **Status:** Accepted
- **Date:** 2026-08-31, amended:
  - 2026-09-02: a record became filed under the process the read came back
    from, and one dated inside a retired life dropped, see
    [ADR-0011](0011-process-lifetime-and-ordering.md)

## Context

A pid belongs to the operating system, which hands the same one out again, and
gcmon monitors a tree of short-lived workers. A record naming a pid does not
say which process produced it.

gcmon already had that answer in one place.
[ADR-0016](0016-the-ring-is-the-statistics-unit.md) put an epoch on everything
a run keeps, counting the processes that have held the pid, and the `--stats`
table prints `12345:0#2` for the second. Nothing else carried it: the exporter
protocol, the `Track` an event names
([ADR-0024](0024-an-event-names-the-track-it-is-drawn-on.md)) and the trace
itself all carried a number two processes shared.

Counting the epoch a second time, at the encoder, would close that with
nothing new plumbed through. `add_process_liveness` names the live pids once a
tick, and a pid missing from one report and back in a later one opens a second
epoch. A gap in those reports is not a departure. The control server
suppresses a pid and the monitor stops polling it, which drops the pid out of
every report until it is re-enabled
([ADR-0011](0011-process-lifetime-and-ordering.md)), and the encoder counts
one process twice. The control server's own path has no reports to read: it
takes a pid and a timestamp off the wire and needs the process that held that
pid then, which may have retired before the message arrived. The epoch would
also be counted twice over one set of evidence, at the encoder and on
`StreamingStats`, with nothing but a test to keep the two in step.

## Decision

**A `Process` is `(pid, pid_epoch)` and holds nothing else.** Every field is
part of identity, and msgspec generates the comparisons and the hash in C. The
encoder keys a dict on a `Process` several times per event; a field carrying
what gcmon read would put both back in Python. What was read stays with the
registry that read it, and a caller holding the pair reaches the rings and
tracks filed under it.

**Only the monitor calls `ProcessRegistry.create`.** The call comes on a read
that returned, not when a listing names the pid and not on the attempt.
Evidence naming a pid gcmon never read has no process to belong to, and a pid
it cannot read produces none. A process per attempt spends an epoch and a
`psutil` command-line read on every poll of a pid that never becomes readable,
for as long as it stays in the tree, and a pid the operating system hands on
afterwards opens at whatever number those attempts reached.

**Below the registry a pid is an `int`; above it, a `Process`.** The reader,
the child listing and the `psutil` calls take the number the operating system
gave; `Track`, the statistics keys and the trace take the process. The
registry is the one place the two meet.

**A control-plane instant is filed under `at(pid, ts)`; a record is filed
under the process it was read from.** A control message carries the time it
arrived when its sender named none, which can be after the pid retired, and
gcmon has nothing else to place it by: a timestamp inside a closed life
belongs to the process that lived it. Past the last retirement, `at` answers
with the process running now, or with the last to leave. Before the first
discovery, it answers with that first process.

A record is different, because gcmon has it only because a read returned it
and that read named a process. A pid pruned from the tree loses its read
cursor ([ADR-0017](0017-monitor-owns-the-pid-lifecycle.md)), so whatever
claims it next re-reads records dated inside its predecessor's life. Those go
nowhere. They went out under the predecessor already, and filing them under
the successor would back-date its span to before it existed. Dating them back
into the closed life instead would widen a span gcmon had stopped observing a
tick earlier, and leave every retired process open to evidence arriving for it
later ([ADR-0011](0011-process-lifetime-and-ordering.md)). The monitor counts
what it drops and says so at debug level.

**The control plane holds a read-only view, not the registry.** A
`ProcessLookup` protocol carrying `at` and nothing else lives in `model`,
which every layer may import. `control` cannot import `monitoring`: the layer
table in `tests/architecture/test_layering.py` forbids that edge.

**A new process reaches the exporter before anything can resolve it.**
`ProcessRegistry.create` takes the exporter's command-line sink and calls it
under the lock. A control message naming that pid waits in `at`, and the
exporter it reaches already knows what the process is running. Handing the
command line on after `create` returned leaves a window: a flush inside it
describes the process without one, and a descriptor is emitted once per
process, which fixes that answer in the trace.

**One lock, taken on every access.** The monitor writes and the control server
reads from its own thread. One write per process against one read per control
message leaves nothing to contend over. The command-line read runs outside it:
it reads another process, and a control message about a third would otherwise
wait behind it.

## Consequences

- The registry assigns every epoch. The table and the trace read the same one,
  and nothing cross-checks them.
- **The epoch still depends on gcmon seeing the departure.** A pid recycled
  between two ticks, with the listing never showing it gone, reads as one
  process throughout. ADR-0016 accepted that and the registry inherits it;
  [ADR-0020](0020-attach-to-a-process-once.md) records the related hazard on
  the read path.
- **gcmon drops evidence for a pid nobody created, without a warning.** The
  ordinary cause is a client naming a pid gcmon never monitored. That is the
  client's error, and the line is logged at debug.
- **The registry reads the command line and keeps nothing.** `create` hands it
  to the sink the caller passed and forgets it. The exporter holds it for the
  rest of the run.
- **Two locks are ordered by that call.** The sink runs under the registry's
  lock and takes the exporter's. Nothing running under the exporter's may take
  the registry's, and the sink may not call back into the registry. The layer
  table blocks every way of breaking that but one: `ProcessLookup` is in
  `model`, so an exporter may legally hold a reader of the registry.
  `tests/architecture/test_lock_order.py` is where that import fails.
- **The registry keeps every departure for the life of the run**, because that
  is what `at` answers from.

## Alternatives considered

- **Derive the epoch at the encoder from the liveness reports**, which cost
  nothing to plumb because the reports were already arriving. Rejected on the
  two blind spots in the Context, plus a third: a command line read once per
  pid, at the first flush, names the first process's program on every later
  span of that pid
  ([ADR-0010](0010-process-identity-cmdline-and-start-marker.md)).
- **Stamp every record with its epoch at the monitor.** Rejected: a record's
  epoch is implied by which process produced it. The number would land on the
  one path that never needs it, and the exporter protocol would widen for
  every format including the ones that ignore it. Naming the process on the
  `Track` an event already carries costs one field on a value that exists.
- **A run-wide process counter instead of a per-pid epoch.** Unique with no
  registry lookup. Rejected: it reads as an opaque number. `#2` means "the
  second process to hold this pid", which is what a reader of a recycled pid
  is asking, and what the table has printed since ADR-0016.
- **Hand the control server the registry itself.** It is one object and the
  control server only reads from it. Rejected: the layer table puts `control`
  below `monitoring`, so the import runs the wrong way, and a handle that can
  create a process is one a later change can create from.
- **Move the registry below both layers, into `model` or `support`.** It would
  make the import legal in either direction. Rejected: the layer a thing
  belongs to is the one that writes it, and only the monitor writes here.
- **A module-level registry.** Rejected: two runs in one process would share
  epochs, and every test would have to reset it.
- **Carry what gcmon read on the `Process`**, a discovery timestamp and a
  command line beside the pair. What ADR-0010 shipped. Rejected: a `Process`
  is a dict key on the write path, and a struct whose fields are not all
  identity needs `__eq__` and `__hash__` written in Python. The discovery
  timestamp had no reader at all.
- **Key the statistics on `(pid, iid, epoch)` and leave the exporters on
  pids.** What ADR-0016 shipped. Rejected: three fields where one value does,
  and the trace still could not say which process a slice belonged to.

## Implementation

- `src/gcmon/model/process.py` holds `Process` and the `ProcessLookup`
  protocol. It is in `model` because every layer above `support` names one.
- `src/gcmon/monitoring/process_registry.py` holds the registry, the lock, the
  departure history and the `psutil` read behind an injected provider. It is
  in `monitoring` because that is the layer that writes to it, and the
  provider is injected rather than defaulted so a test naming a pid the
  machine does not have reads nothing instead of whatever holds that number.
- `src/gcmon/exporters/exporter.py` carries `add_process_cmdline`, a no-op
  everywhere but the Perfetto path. `exporters` cannot import `monitoring`:
  the monitor hands it to the registry as the sink, and `PerfettoTrackState`
  keeps what arrives until the descriptor and the span are emitted.
- `src/gcmon/monitoring/monitor.py` creates as it polls and retires where it
  drops a pid's state, both halves of
  [ADR-0017](0017-monitor-owns-the-pid-lifecycle.md)'s prune.
- `src/gcmon/control/control_server.py` resolves the pid on the wire through
  the protocol and drops what resolves to nothing.
- `src/gcmon/cli/monitor/loop_runner.py` builds the one registry a run
  has, before the target starts, because the control server has to be
  listening by then.
- `tests/test_process.py` pins the struct to the pair, the ordering and the
  suffix; `tests/monitoring/test_process_registry.py` pins creation, the prune
  and what `at` answers on each side of a departure;
  `tests/architecture/test_layering.py` is where the `control`-to-`monitoring`
  edge fails, and `tests/architecture/test_lock_order.py` is where an exporter
  naming `ProcessLookup` does. Both are deselected by default.
