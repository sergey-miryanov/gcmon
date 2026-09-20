# ADR-0028: Draw every process a row of its own, under a pid gcmon counts

- **Status:** Accepted
- **Date:** 2026-09-02
- **Amended by:** [ADR-0027](0027-group-every-row-an-interpreter-owns.md)
- **Modules:** exporters

## Context

A pid belongs to the operating system, which hands the same one out again, and
a `Process` names which holder of it a record came from
([ADR-0025](0025-create-every-process-in-one-place.md)).
[ADR-0010](0010-process-identity-cmdline-and-start-marker.md) gives a process
a track and a `Lifetime` slice that keeps it visible, and
[ADR-0011](0011-process-lifetime-and-ordering.md) draws one span per process
on the shared `Processes` row. Two processes that held one pid still have to
come out as two rows, each with its own start stamp, command line and
counters.

The trace processor keys process identity on `ProcessDescriptor.pid`, not on
the track uuid, so descriptors sharing a pid do not reliably draw a row each:
two descriptors on one pid split, and a third does not. Measured against the
trace processor the suite pins.

A span on the shared row says when gcmon watched a process and nothing about
how much of it gcmon read (ADR-0011), so a reader needs somewhere to find
that.

## Decision

**The Perfetto process track is split per process, each row stamped and ranked
from its own first observation.**

**A row is written under a pid gcmon counts from 1, not the operating
system's.** The `ProcessDescriptor` is the only place it reaches the trace:
gcmon writes no `ThreadDescriptor`, and everything an interpreter owns is a
plain custom track under that process
([ADR-0027](0027-group-every-row-an-interpreter-owns.md)). The trace processor
still builds one nameless thread per process out of that descriptor, with a
`tid` equal to the pid, and `thread.is_main_thread` marks it. No query of
gcmon's reads it. The operating system's pid rides as an annotation on both of
a process's spans (ADR-0011).

**Every process draws a full set of rows of its own**: process track, a group
per interpreter holding that interpreter's rows, and a `Lifetime` slice, named
`Process <pid>` and `Process <pid>#N` to match its `Processes` span.
`start_timestamp_ns` stamps a row where its process started, and a counter's
shared y axis stops at its own process's rows
([ADR-0005](0005-counter-y-axis-share-key.md)).

**A process with a span and no descriptor gets one at close.** Finalization
walks every process the accumulator holds, so a process gcmon polled and read
no collections from
([ADR-0029](0029-report-liveness-and-fold-it-into-the-span.md)) draws its own
row: a `ProcessDescriptor` stamped and ranked from its first observation, its
command line, and a `Lifetime` slice with nothing under it.

**Both rows draw the pair gcmon observed.** A process's own row carries one
`Lifetime` slice
([ADR-0010](0010-process-identity-cmdline-and-start-marker.md)) and the
workload's `Instant` marks, which nest without closing anything; its span on
the shared row has a track to itself (ADR-0011). Neither row has to give up an
end to draw, and both carry the command line, the pid and the epoch. The rows
differ in what they answer: this one is about one process, the shared one
about the run.

**How much of the process gcmon read is counted in the convert pass.** The
`Lifetime` slice says `sampled_count` against `lost_count`, and the exporter's
entry points are where those arrive: `add_event` takes one record and
`add_loss_event` one poll interval. Neither holds the encoder lock, and the
accumulators are read under it, so counting there means a second acquisition
per record on the hot path. Counting in the convert pass costs nothing, since
its caller already holds the lock, and it also covers `gcmon combine`, which
builds a trace from a capture without an exporter.

The pass sees events rather than records, so it counts the two that stand for
one thing each: the `GC Pause` slice, which every record produces one of, and
a slice on a `LossTrack`, which is one interval. Every other event is a phase
or a counter of a record already counted.

`sampled_count` cannot be summed off the `GC Loss` slices
([ADR-0015](0015-gc-loss-spans-on-their-own-track.md)). `EventsMonitor`
reports an interval only when it lost something, so the `observed_count`
riding there covers lossy intervals alone; a process that lost nothing has no
slice to sum and would read as one gcmon never sampled.

**A retired process's row and its span on the shared row both go out at the
next flush.** Once gcmon lets go of a pid the process's span is final: a
record read afterwards is filed under whatever holds the pid now (ADR-0025),
and liveness and RSS both work off the tick's live set. Neither slice needs
anything but that one span, since no span is measured against another
(ADR-0011), so both are drawn as soon as the events queued ahead of them have
reached the accumulator. A process still held when the run stops is drawn at
close instead.

The Perfetto UI hides a row holding no events, so a `Lifetime` slice that
never reached the file takes its whole row with it, its interpreters' rows and
all. A process already retired keeps its row and its place on the minimap; one
still running keeps neither.

The exception is the control plane, which files an instant by timestamp and
can still name a retired process (ADR-0025). One arriving after the row was
drawn lands on it outside the slice. Accepted: the alternative is holding
every row back for a message that may never come.

## Consequences

- **`process.pid` is never the operating system's**, so a query joining it
  against a pid from `ps`, from a log or from a trace recorded elsewhere
  matches nothing rather than matching some of the processes that held that
  pid.
- **A zero-GC process draws a full row**, since the monitor reads its command
  line when it creates the process rather than on the encoder's write
  ([ADR-0010](0010-process-identity-cmdline-and-start-marker.md)) and
  finalization gives it a descriptor off its span alone. Only the pause rows,
  the loss rows and the counters are missing, because it produced nothing to
  draw on them.

## Alternatives considered

- **Sharing one process track across every process that held a pid**, on the
  grounds that two descriptors on one pid might collapse to a single `upid`.
  Rejected: the shared row interleaves two processes' events, steps its
  counters between them with nothing marking where, and carries a start stamp
  that predates the successor.
- **Deriving the epoch from gaps in the liveness reports**, which reach the
  encoder already and would have split the spans with nothing new plumbed
  through. Rejected in [ADR-0025](0025-create-every-process-in-one-place.md):
  a pid the control server suppresses produces the same gap as a pid that
  died.
- **Resolving the epoch inside the encoder**, asking a `ProcessLookup` which
  process held the pid at a record's timestamp. Rejected: the monitor already
  decided that when it created the process (ADR-0025), so a second answer
  computed downstream can only disagree with the first, and a boundary
  timestamp is where it would.
- **Keeping one process track and annotating each counter with the epoch.**
  Rejected: an annotation on a counter is not a row, so the UI still draws one
  line stepping between two processes. It also leaves the start stamp, the
  lifetime slice and the command line wrong.
- **Writing the operating system's pid on every descriptor**, leaving
  `process.pid` a trace-wide identifier a reader joins on and correlates
  against other tools. Rejected: two descriptors on one pid split and a third
  does not, so the scheme costs a row per process past the second. The join it
  buys is replaced by the `pid` annotation on both of a process's spans and by
  the row's name.
- **Leaving the operating system's pid on the first process to hold it**, and
  counting only its successors, keeping `process.pid` joinable for the rows a
  pid was never reused for. Rejected: a column true for most rows and false
  for the rest is a worse rule than one never true, because a query on it
  comes back with a subset that looks like an answer.
- **Offsetting a process by its epoch**, `pid + (pid_epoch - 1) * space`, so
  `process.pid` decodes to the operating system's pid and the epoch with no
  annotation. Rejected: `ProcessDescriptor.pid` is an `int32`, so whatever
  *space* reserves for the pid comes out of the epoch. At Linux's `1 << 22`
  ceiling it allows 512 processes per pid and still misses a Windows pid,
  which reaches `1 << 26`; sized for Windows it allows 32, which an ordinary
  run reaches. The case it misses needs a fold and a warning, machinery for a
  convenience the annotation already provides.
- **Fixing the command line alone**, the one field that was wrong rather than
  merged: the `#2` span and the track above it named different programs.
  Rejected: there is no correct value to write into a field two processes
  share, and the pause row, the counters, the start stamp and the lifetime
  slice stay merged behind it.
