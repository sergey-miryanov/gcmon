# ADR-0010: Duplicate the process cmdline per consumer, and force the process track to render

- **Status:** Accepted
- **Date:** 2026-06-08
- **Amended by:** [ADR-0011](0011-process-lifetime-and-ordering.md),
  [ADR-0025](0025-create-every-process-in-one-place.md)
- **Modules:** exporters, monitoring

## Context

gcmon discovers child PIDs at runtime and monitors them alongside the main
process, so a trace routinely contains several processes. `Process 4821` is
not enough to tell them apart, so you need the command line.

Perfetto's `ProcessDescriptor` has a `cmdline` field (field 2, repeated
string) for this. Two problems stood in the way.

**The trace processor does not surface it.** `ProcessDescriptor.cmdline` is
not exposed via the SQL `process.cmdline` column, which always returns `None`.
Writing it correctly makes the data visible in the UI but unqueryable from
SQL, which is how the trace-processor tests and any user analysis read a
trace.

**The process track was often invisible.** The Perfetto UI renders a track's
`description` only when the track has at least one event on it. The process
track is OS-scoped, and nothing but an instant was drawn on it then: every
slice and every counter went on a child track. A trace with no instant events
for a pid therefore had an empty process track, and the UI hid its
description.

## Decision

**Write the cmdline for each consumer, on purpose.**

- `ProcessDescriptor.cmdline` (field 2, repeated string), protobuf-correct and
  visible in the UI.
- `TrackDescriptor.description` (field 14, string), the space-joined cmdline,
  on the process track. This one *is* surfaced in the `args` table and joins
  to `process` via `track`, so it is queryable:

  ```sql
  SELECT p.pid, a.string_value AS description
  FROM process p
  JOIN process_track pt ON pt.upid = p.upid
  JOIN track t ON t.id = pt.id
  LEFT JOIN args a ON a.arg_set_id = t.source_arg_set_id AND a.flat_key = 'description'
  ```

**Draw a `Lifetime` slice on the process track itself**, one
`TYPE_SLICE_BEGIN` / `TYPE_SLICE_END` pair per process spanning the interval
gcmon observed it ([ADR-0011](0011-process-lifetime-and-ordering.md)). The
track therefore always holds an event, so it and its description always
render, and the row says how long gcmon watched the process rather than only
that it existed.

**A process descriptor and a `Lifetime` slice belong to a process, not to a
pid.** A pid handed on names two processes, each with its own command line to
render ([ADR-0011](0011-process-lifetime-and-ordering.md)).

**A command line is read once per process, where the monitor creates it.**
Reading it at the first flush instead cost two things: a process that exited
between the poll and the flush had none left to read, and a read filed under
the pid put the first process's program on every later process that held it.
The monitor discovers a process while it is running. `create` hands the read
back with the process and the monitor forwards it to the exporter, because a
`Process` holds its identity and nothing else
([ADR-0025](0025-create-every-process-in-one-place.md)).

**The read degrades silently.** It imports `psutil` lazily. If it is not
installed, or the process is gone or inaccessible, nothing is read, a warning
says so and the trace stays valid. A process gcmon never polled has no command
line, and that is the only way to have none.

## Consequences

- You can identify processes in the UI and query them from SQL.
- The cmdline is stored more than once, and the `Lifetime` and `Processes`
  slices repeat it again as an annotation. Accepted: the consumers differ (UI
  rendering versus the SQL `args` table), and neither reads the other's copy.
- **The process track is never empty.** Every process draws one `Lifetime`
  slice on it, and the workload's own marks land on the same row, nested
  inside the slice unless one shares its start timestamp. The slice is
  Perfetto-only, so a consumer enumerating a process's slices reads it among
  them.
- `psutil` stays an optional dependency (the `cmdline` extra). gcmon works
  without it, minus the cmdline.
- **A `combine` run writes no command line.** Offline conversion
  ([ADR-0021](0021-write-one-trace-format.md)) creates no process, so nothing
  is read.
- `description` joins the arguments with spaces and no shell quoting,
  favouring readability over round-trippability. The structured form is in
  `ProcessDescriptor.cmdline`.

## Alternatives considered

- **`ProcessDescriptor.cmdline` alone.** Rejected: not queryable from SQL, so
  the trace-processor tests could not assert on it and you could not analyse
  by it.
- **`TrackDescriptor.description` alone.** Rejected: it abandons the
  protobuf-correct field, and a future trace-processor version that does
  surface `cmdline` would find it empty.
- **Collect the cmdline in the exporter**, on the grounds that it is trace
  metadata only the Perfetto format needs, so the JSONL and stdout paths carry
  no `psutil` cost. Rejected: the exporter learns of a process on the first
  flush that mentions it, which is the wrong moment on both counts above;
  offline it asks the local machine about a historical pid, which answers
  about an unrelated process once the pid has been reissued; and the saving is
  one `psutil` call per process on a path that already reads every process
  once a tick. The Perfetto-only part is the emission, not the collection.
- **Make `psutil` a hard dependency.** Rejected: gcmon is installed next to
  the process it monitors, and graceful degradation costs one `try`/`except`.
