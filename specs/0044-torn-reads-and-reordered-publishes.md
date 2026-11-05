# 0044: Keep monitoring through a contended read

- **Status:** Landed
- **Kind:** bug (availability)
- **Effort:** S
- **Origin:** [#251](https://github.com/sergey-miryanov/gcmon/issues/251), a
  CodSpeed failure on CPython 3.15.1
- **Respects:**
  - [ADR-0020](../docs/adr/0020-attach-to-a-process-once.md): a failed read
    lets go of the attachment, and the reader owns `_remote_debugging`'s error
    vocabulary. Both held. Amended: the consequence that forbade a retry
    keeping the old attachment, and the sentence that gave cursors and
    attachment one lifetime.
  - [ADR-0017](../docs/adr/0017-monitor-owns-the-pid-lifecycle.md): cursors,
    policy and process share a lifetime and one prune. A contended poll prunes
    nothing.
  - [ADR-0015](../docs/adr/0015-gc-loss-spans-on-their-own-track.md): the
    retained cursors turn a skipped poll into an ordinary loss window.
    Amended: the trust bullets that named the two hazards.
  - [ADR-0025](../docs/adr/0025-create-every-process-in-one-place.md): a
    process retires when it departs, and a contended poll is not a departure.
  - [ADR-0029](../docs/adr/0029-report-liveness-and-fold-it-into-the-span.md):
    liveness is stamped from reads that returned, so a contended poll reports
    none.
  - [0024](0024-cpython-report-remote-readable-gc-stats.md): the upstream
    report. Section 4's rows for 3.3 and 3.5 changed with ADR-0015.

## 1. Problem

`gcmon run` could kill a healthy workload. A read that landed on a collection
ended the run: gcmon terminated the target it had started, wrote a shortened
trace and exited 0, and nothing at default verbosity named the cause.
`gcmon monitor` stopped early the same way and left its target running.

In a process tree the run survived and the trace was wrong. A live process was
retired and came back as a new epoch, so `--stats` reported two processes. The
records stamped between its last good read and the contended one reached no
slice and no loss window.

## 2. Evidence

**Upstream.** gh-155811 added `update_seq` to `struct gc_stats`, first
released in v3.15.1, the version gcmon requires. `add_stats` in `Python/gc.c`
and `gc_collect_main` in `Python/gc_free_threading.c` hold it odd across every
ring write. `get_gc_stats_from_interpreter_state` in
`Modules/_remote_debugging/gc_stats.c` brackets its copy with two reads of it,
tries twice with no pause, and then raises:

```c
PyErr_SetString(PyExc_RuntimeError,
                "GC stats changed while being read; retry later");
```

The error aborts `iterate_interpreters`, so one contended interpreter discards
the records of every interpreter in that read.

**The path through gcmon, before this change.** `RemoteEventsReader.read`
translated the error into `TargetUnavailable`, and `EventsMonitor._poll`
answered `PollStatus.INVALID_PROCESS`. `StartupTimeoutPolicy.wait` read that
as death once it had seen the pid alive. `EventsMonitor.tick` then called
`_forget`.

With no other pid holding the run open, `MonitorLoop.run` broke.
`run_monitoring_loop` waited two seconds for the subprocess, read a
`returncode` of `None` as 0, and `ChildProcessRunner.__exit__` terminated the
target.

With another pid holding it open, `_forget` had dropped the cursors, so the
next read seeded them and opened no gap. `_ingest` then found the records
dated inside the retired epoch, took them for re-reads and dropped them,
though that epoch never exported them.

**The two hazards.** The same counter closes the two in 0024 sections 3.3 and
3.5. `add_stats` is the only writer of the ring, so a snapshot that passes the
check holds no half-written record and no hole. gcmon has neither left to
handle.

## 3. Scope

**Affected.** `gcmon monitor` and `gcmon run`, on every `--format` and every
platform, on GIL and free-threaded builds.

**Not affected.** `gcmon combine`, which reads a capture. A target that has
exited, which raises a different error and stays `TargetUnavailable`.

**Why the suite did not catch it.** Test doubles for the reader raised
`TargetUnavailable`, and no double raised this error. The tests reading a real
target make few reads, and only CodSpeed's simulation mode was seen losing the
race.

## 4. What landed

1. **Name the outcome.** `events_reader` has `ReadContended` beside
   `TargetUnavailable`, and not a subclass of it: a handler of
   `TargetUnavailable` would take it for a missing target.
2. **Retry in the reader.** `RemoteEventsReader.read` recognises the error by
   its message, since it is a bare `RuntimeError`. It makes up to three more
   reads on the same attachment, 1 ms apart. CPython's two attempts are
   microseconds apart and can land inside one collection. A read that returns
   keeps the attachment. Neither figure is measured; a wrong one only changes
   how often step 3 runs.
3. **Skip the poll when the retries run out.** The reader drops the
   attachment, as for any failed read, and raises `ReadContended`. `_poll`
   catches it and answers a new `PollStatus.CONTENDED`, logged at debug level
   once per pid and reason as `TargetUnavailable` is. `tick` does not ask the
   policy, sets `keep_running` and prunes nothing. The next read that returns
   diffs against the retained cursors, so a ring that wrapped meanwhile opens
   a loss window.
4. **Take the race out of the benchmarks.** `test_bench_events_reader` reads a
   target that fills its rings, signals, and stops collecting, so a retry
   cannot land in the instruction count. The held-read benchmark's baseline
   moved with the target. The cause is unconfirmed: every row a read returns
   now holds a record, and a local run moved far less than CodSpeed did.
5. **Check it live.** On 3.15.1, Windows, under `gcmon run`, against a target
   collecting as fast as it can: the process kept one epoch, and the records
   one poll returned for a ring were consecutive on `collections`.
   `gcmon monitor` was not run. No poll was seen to run out of retries and no
   read was seen to be contended, so a real contended read has not exercised
   steps 2 and 3.
6. **Delete what pinned the old shapes**, after step 5:
   `test_a_hole_inside_a_run_goes_unnoticed` and `TestATornRead`. The ADR-0015
   trust bullets and 0024 section 4 now say the target's seqcount provides the
   contiguity gcmon relies on.

## 5. Seams and testing decisions

- **Seam:** `EventsMonitor.tick`, through `FakeEventsReader` and the real
  `StartupTimeoutPolicy`, for what a contended poll does to a run. The reader
  through `SpyMonitor` for retries and attachment lifetime.
- **New seam needed:** none.
- **What makes a good test here:** the report, the exported records, the loss
  windows, the retired processes and the attach count. Not how many retries
  fired.
- **Prior art:** `_drive` in `tests/monitoring/test_monitor.py`, and
  `SpyMonitor` in `tests/monitoring/test_events_reader.py`.
- **Cases:**
  1. A single process with one contended poll: the run keeps going.
  2. A child with one contended poll: one epoch, nothing retired, and the
     records stamped across the skipped tick are exported under it.
  3. A ring that wraps across the skipped tick: a loss window with the right
     counts.
  4. A contended poll followed by a dead target: the ordinary end of a run.
  5. The reader, contended and then answering: one attach, and the records
     returned.
  6. The reader, contended on every attempt: `ReadContended`, and the next
     read attaches again.
  7. The regression guard: any other `RuntimeError` stays `TargetUnavailable`
     with no retry.
  8. The message the reader matches is present in `_remote_debugging`'s
     binary, where the module is a file, so a rewording fails CI.
  9. The benchmarks' target: its rings are full and two reads of them are
     equal, since the default run deselects the benchmarks.
- **Real targets:** the probe loops in `running_target` and
  `_collecting_target` catch `ReadContended` too, and so does
  `_read_a_record_stamped_after`.

## 6. Out of scope

- **A dedicated exception type in CPython.** It would replace the message
  match, and it is an upstream request gcmon does not need to wait for.
- **`monitor._is_complete` and `RingAccumulator.unseen`.** The first still
  drops empty slots, and the second is how one poll is diffed against the
  last. `TestAMidWriteSlot` stays with them, though the sequence counter
  removes the shape it builds.
- **Repairing a hole inside one poll's records.** The floor removes the shape.
- **Reporting how many polls were contended.** A skipped poll costs at worst a
  loss window, which the trace and `--stats` already show.
- **Other failures read as death.** `StartupTimeoutPolicy` treats every
  `TargetUnavailable` after the first good read as final, and this was the
  only transient one known.
- **The other three findings in 0024.** None of them ends a run.
- **A recycled pid behind a stale attachment**, which
  [0052](0052-a-recycled-pid-can-be-read-through-a-stale-attachment.md) owns.

## 7. Further notes

**Alternatives.**

- **Retry alone.** Any bound can run out, and running out would still end the
  run.
- **Skip alone.** Correct, and the next tick is a natural retry. A ring holds
  few slots, so on a fast target a skipped poll costs records that a few
  milliseconds of retry keeps.
- **Keep the attachment when the retries run out.** A stale attachment reading
  unrelated memory can fail the sequence check on every read, and the pid
  would stay contended forever. Attaching again ends that.
- **Ask the wait policy.** Both policies would need a new answer for a poll
  that says nothing about the process, and `tick` can state that once.

`Read Time` carries the retries, as it carries the attach.
