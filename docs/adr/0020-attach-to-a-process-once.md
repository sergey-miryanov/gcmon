# ADR-0020: Attach to a process once, and let go the moment a read fails

- **Status:** Accepted
- **Date:** 2026-08-21
- **Modules:** monitoring

## Context

Reading a process's GC records has two halves. One is finding the target:
opening it, locating its runtime, reading and validating its debug offsets.
The other is copying the rings out. Only the second is the point, and gcmon
used to do both on every poll of every process, throwing the first half away
each time.

Attaching scans the target's loaded modules for the runtime section and
validates what it finds; a read is a handful of remote memory copies. The gap
is two orders of magnitude. gcmon therefore spent almost all of every poll
re-deriving what it had derived on the poll before, once per process per tick.

CPython 3.15 offers both shapes: a free function that attaches, reads and
detaches, and a `GCMonitor` object that attaches once and reads many times.

## Decision

**Attachment is state, so it has one owner and one prune.** It lives behind
`EventsReader` in `monitoring`, and the monitor drops it in the same pass that
drops that pid's cursors and streaming statistics.
[ADR-0017](0017-monitor-owns-the-pid-lifecycle.md)'s rule extends to it
unchanged: cursors and attachment share a lifetime, and nothing prunes either
one anywhere else. Child discovery stays outside the reader: it is stateless,
caches nothing, and answers a question about the process tree rather than
about a ring.

**`debug=True` is passed explicitly, and it selects an exception type rather
than a log level.** Under `debug=False` a dead target surfaces as
`ProcessLookupError`; under `debug=True` CPython *replaces* the live exception
with a `RuntimeError` carrying a descriptive message and demotes the original
to `__cause__`. It prints nothing. gcmon passes `True` because that is what
the free `get_gc_stats` function hardcoded, so the switch to `GCMonitor`
changes what gcmon reads and not what it catches or logs.

`debug=False` gives better signal: it is the only way to tell a target that
has died from one whose GC layout does not match gcmon's build. Nothing
consumes that distinction today, because both collapse into
`TargetUnavailable`. It becomes the right answer when something does.

**gcmon holds an attachment only while its reads keep returning, and never
remembers a failed attach.** An attachment holds a runtime address and debug
offsets derived from the process that existed when it was made, and
revalidates neither. Applied to a recycled pid it reads an unrelated process's
memory at the old address. Every field gcmon wants is an integer copied out of
memory, so the result is not a crash and not an error: it is a set of records
that are structurally valid, pass every filter gcmon has, and reach the trace.
A stale cursor makes a number wrong; a stale attachment invents the data.

## Consequences

- **Reconstructing an attachment after a failed read looks wasteful and is
  deliberate.** It costs one attach on a tick that already failed, which is
  what every tick used to cost. Do not "optimise" it into a retry that keeps
  the old attachment.

- **gcmon cannot notice a pid recycled between two successful reads.**
  [Spec 0052](../../specs/0052-a-recycled-pid-can-be-read-through-a-stale-attachment.md)
  specifies the pid-epoch machinery that would close it. Only Linux is
  exposed. [Remote reads, per platform](../internals/remote-reads.md) says why
  Windows and macOS cannot serve a read from a recycled pid at all. macOS
  depends on the drop rule above, since only a failing read notices the swap
  there. Anything leaning on that safety has to say which platform it is on.

- **`EventsMonitor` names no exception type from `_remote_debugging`.** What
  an unreadable target raises differs by platform, and again under
  `debug=True`; [Remote reads, per platform](../internals/remote-reads.md) has
  the whole table. That vocabulary stops at the reader, which translates it
  into `TargetUnavailable`. Test doubles say "unavailable" instead of
  impersonating CPython's taxonomy.

- **The translation takes `ProcessLookupError` and `PermissionError`, not
  `OSError`.** Those two are the platform's way of saying the process is gone
  or closed to gcmon. Every other `OSError` says the read itself was wrong,
  `EFAULT` and `EINVAL` out of `process_vm_readv` among them, and a wrong read
  is a gcmon defect.

- **`ValueError` is outside it for the same reason.** macOS raises it for a
  read whose arguments made no sense *while `task_info` reports the task still
  valid*: a bad address, not a dead process. A terminated macOS target raises
  `ProcessLookupError`, which is translated. So both belong on
  `PollStatus.FAIL` with a traceback, where they were before this change.
  Widening the translation to swallow either would turn a bug into a silent
  "target unavailable" and burn the startup timeout hiding it.

- **The gap the decision rests on is measured**, a held read against a fresh
  attach, so a change that reattaches per poll fails a benchmark rather than
  quietly costing an order of magnitude.
- **The first poll of each process still pays for the attach**, and
  `Read Time` carries that cost rather than excluding it. An operator sees one
  outlier per process, and the cost of reading alone after it.

## Alternatives considered

- **Default the reader argument, so existing construction sites keep
  working.** Rejected. A default builds a real reader, and a test that forgets
  to inject one attaches to whatever process happens to hold the integer it
  used as a pid; the monitor tests use small integers as pids throughout. A
  required argument fails loudly on a runner instead of silently reading a
  stranger.

- **Keep the free function and cache nothing.** The status quo. It is correct,
  and it makes gcmon's own cost proportional to the number of reads rather
  than to the number of processes.

- **Drop the attachment only when a read proves the process is gone.**
  Narrower and cheaper, and wrong for the reason above: a read can fail
  without proving anything, and gcmon must not carry offsets it can no longer
  vouch for.

- **Revalidate the attachment on each read**, comparing the target's start
  time, say. Rejected: it puts back a per-poll probe of the target, which is
  the cost this decision exists to remove.

- **Let the monitor widen its `except` clause instead of translating in the
  reader.** Two words smaller, and it leaves the monitor owning the
  platform-specific error vocabulary the seam exists to contain. Returning an
  empty result instead of raising was also rejected: it loses the cause the
  debug log prints.
