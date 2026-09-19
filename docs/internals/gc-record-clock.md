# The clock a GC record is stamped from

Which clock CPython reads for a record's `ts_start` and `ts_stop`, where
`duration` comes from, and why a number gcmon reads off its own clock sits on
the same timeline. [ADR-0009](../adr/0009-nanoseconds-canonical-time-unit.md),
[ADR-0015](../adr/0015-gc-loss-spans-on-their-own-track.md) and
[ADR-0023](../adr/0023-the-pyperf-hook-annotates-and-does-not-drive.md) are
the decisions this backs.

Everything here is read at CPython 3.15.0b4, in `Python/gc.c`,
`Python/gc_free_threading.c`, `Python/pytime.c` and `Modules/timemodule.c`.

| | Reads | Through |
|---|---|---|
| A record's `ts_start` and `ts_stop` | the monotonic clock | `PyTime_PerfCounterRaw` |
| `time.monotonic_ns()` | the monotonic clock | `PyTime_Monotonic` |
| `time.perf_counter_ns()` | the monotonic clock | `PyTime_PerfCounter` |
| `time.time_ns()` | the wall clock | not used for a record |

The rest of this page is where each row comes from.

## What stamps a record

`gc_collect_main` reads the clock twice, into `ts_start` before the collection
and into `ts_stop` after it, and both reads are `PyTime_PerfCounterRaw`. The
free-threaded collector in `gc_free_threading.c` makes the same two calls.

Both fields are `PyTime_t` in `struct gc_generation_stats`, which is integer
nanoseconds.

## The performance counter is the monotonic clock

`pytime.c` defines one in terms of the other:

```c
int
PyTime_PerfCounterRaw(PyTime_t *result)
{
    return PyTime_MonotonicRaw(result);
}
```

`PyTime_PerfCounter` returns `PyTime_Monotonic` the same way.
`PyTime_Monotonic` and `PyTime_MonotonicRaw` both call
`py_get_monotonic_clock`. The `Raw` form differs in one thing: it raises no
exception, so it is safe to call without an attached thread state.

`time.monotonic_ns()` is `PyTime_Monotonic` and `time.perf_counter_ns()` is
`PyTime_PerfCounter`, so all of them read one clock.

## What the monotonic clock is, per platform

| Platform | Source |
|---|---|
| Windows | `QueryPerformanceCounter` |
| macOS | `mach_absolute_time` |
| Linux and the other POSIX platforms | `clock_gettime(CLOCK_MONOTONIC)`, or `CLOCK_HIGHRES` where it is defined |

Each is a system-wide counter, and `py_get_monotonic_clock` scales the ticks
to nanoseconds without subtracting an origin of its own. Two processes on one
machine therefore read comparable numbers. That is what lets gcmon place an
instant it read itself, a loss window's edge or an RSS sample, beside the
timestamps the target wrote, and what lets a workload's mark sit among the
records.

## Where `duration` comes from

`duration` is a `double` of seconds, computed from the same two reads:

```c
stats.duration = PyTime_AsSecondsDouble(stats.ts_stop - stats.ts_start);
```

`add_stats` then adds it to the running total it copied from the previous
record, which is the cumulative `duration` gcmon reads. The total and the
timestamps share a clock by construction, since one is the difference of the
other two.

## What a failed clock read leaves

The collector discards the return value of both reads, with the comment "don't
interrupt the GC if reading the clock fails". On failure `PyTime_MonotonicRaw`
writes `0` into the field, so the record is published with a zero in it and
nothing marks it.
