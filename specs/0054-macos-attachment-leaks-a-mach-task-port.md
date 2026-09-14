# 0054: Release the Mach task port a dropped attachment held

- **Status:** Not started
- **Kind:** bug (availability)
- **Effort:** S
- **Origin:** reading `Python/remote_debug.h` at 3.15.0b4 while writing
  [Remote reads, per platform](../docs/internals/remote-reads.md), 2026-08-20.
  Unverified on hardware: this was found in the source, not in a run
- **Respects:**
  - [ADR-0020](../docs/adr/0020-attach-to-a-process-once.md): an attachment is
    dropped on every failed read, so the re-attach cadence is what turns a
    per-attachment leak into growth.

## 1. Problem

An operator runs `gcmon monitor` on macOS against a long-lived process tree
whose workers come and go. Every worker gcmon attaches to costs gcmon one Mach
port name that is never given back, and every failed read costs another,
because a failed read is followed by a fresh attach. Nothing in gcmon's output
says so. What an operator would see at the end of that growth has not been
established: the plausible failure is `task_for_pid` refusing to attach once
the port name space fills, which would read as a target gcmon cannot attach to
rather than as a leak in gcmon.

## 2. Evidence

`proc_handle_t` holds a `mach_port_t task` on macOS, taken by `pid_to_task`,
which calls `task_for_pid`. `task_for_pid` hands over a send right that its
caller owns and has to release with `mach_port_deallocate`.

`_Py_RemoteDebug_CleanupProcHandle` is the only thing that releases what a
handle took, and it has two platform branches:

```c
#ifdef MS_WINDOWS
    if (handle->hProcess != NULL) { CloseHandle(handle->hProcess); ... }
#elif defined(__linux__)
    if (handle->memfd != -1) { close(handle->memfd); ... }
#endif
    handle->pid = 0;
    _Py_RemoteDebug_FreePageCache(handle);
```

There is no `__APPLE__` arm. On macOS the cleanup zeroes the pid and frees the
page cache, and the port send right stays in the caller's name space.

`GCMonitor_dealloc` reaches that cleanup through `cleanup_runtime_offsets`, so
dropping the last reference to a `GCMonitor` is what releases a handle on the
two platforms that release anything. `gcmon.monitoring.events_reader` drops
that reference on every failed read and on every prune, which is what makes
the count grow rather than sit at one per live pid.

**Attaching once made this better, not worse.** Before
[ADR-0020](../docs/adr/0020-attach-to-a-process-once.md) gcmon built and
discarded a handle on every poll of every pid, so the leak grew with polls. It
now grows with attachments, which is one per pid plus one per failed read.
Neither is bounded.

## 3. Scope

**Affected:**

- macOS, every subcommand that monitors a live process, every `--format`.
  Worse the wider the fan-out and the shorter the worker lifetimes, since both
  raise the number of attachments a run makes.

**Not affected:**

- **Windows and Linux.** `CloseHandle` and `close` run on the paths that drop
  an attachment.
- **`gcmon combine`** and every offline path: no attachments.
- **What gcmon reports.** Records, traces and statistics are unchanged; this
  is gcmon's own resource use.

**Why the suite does not catch it.** Nothing counts what an attachment costs
the monitor process. `tests/monitoring/test_events_reader.py` asserts how many
times gcmon attaches, which is the opposite measure: a run that leaks a port
per attach passes every one of those assertions.

## 4. Proposed change

1. **Report it upstream**, the way
   [0024](0024-cpython-report-remote-readable-gc-stats.md) reports the ring.
   The fix is an `__APPLE__` arm on `_Py_RemoteDebug_CleanupProcHandle`
   calling `mach_port_deallocate(mach_task_self(), handle->task)`, and it
   belongs with the code that took the right.
2. **Confirm it on hardware first.** Count the monitor process's port names
   across a run that attaches many times, and show the count tracking
   attachments rather than live pids.
3. **Decide whether gcmon does anything meanwhile.** The honest options are to
   wait for the fix, or to bound the damage by not re-attaching pids gcmon has
   already given up on. Neither is settled, and the measurement in step 2 is
   what settles it: a leak that takes days to matter argues for waiting.

**Rejected: gcmon releasing the port itself.** It has no handle to release.
`GCMonitor` exposes the pid and the records, not the port, and reaching past
that would put a `ctypes` Mach call in a package whose one rule about
`_remote_debugging` is that it stays behind `gcmon.monitoring.events_reader`.

## 5. Seams and testing decisions

- **Seam:** `gcmon.monitoring.events_reader.RemoteEventsReader`, driven
  directly. The leak is per attachment, and that class is where attachments
  are made and dropped.
- **New seam needed:** a way to read the process's own Mach port count, which
  nothing in the repo does. `mach_port_names(mach_task_self(), ...)` through
  `ctypes` in the test, not in the package.
- **What makes a good test here:** assert that the count after N
  attach-and-drop cycles is the count before, within a small margin for the
  interpreter's own churn. Asserting an absolute number pins the platform
  rather than the defect.
- **Prior art:**
  `tests/monitoring/test_events_reader.py::TestAttachOncePerPid` for the
  attach-and-drop cycle to build on; it counts attaches where this counts what
  they cost.
- **Cases:**
  1. N attach-and-drop cycles against one live target leave the port count
     where they found it. Fails today.
  2. A run that attaches once and reads many times does not grow the count,
     which is the control: without it a fix that leaked per read would pass
     case 1.
  3. Skipped off macOS, by platform rather than by marker, since the resource
     does not exist elsewhere.

## 6. Out of scope

- **The Linux `memfd` and Windows handle paths.** Both are released, and this
  changes neither.
- **ADR-0020's lifetime.** Attaching once and dropping on every failed read is
  what it is; this spec is about what a drop costs, not when one happens.
- **Any other resource CPython's remote debugging holds.** The page cache is
  freed on every platform, and nothing else in `proc_handle_t` owns a kernel
  object.

## 7. Further notes

This came out of reading, not out of a run, and it should not be sized until
somebody has counted ports on a Mac. The upstream report is worth filing
either way, since the missing arm is plain in the source and does not depend
on how fast the growth is.
