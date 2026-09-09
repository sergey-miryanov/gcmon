# gcmon - zero-overhead GC monitoring for Python

[![PyPI](https://img.shields.io/pypi/v/gcmon.svg)](https://pypi.org/project/gcmon/)
[![CI](https://github.com/sergey-miryanov/gcmon/actions/workflows/ci.yml/badge.svg)](https://github.com/sergey-miryanov/gcmon/actions/workflows/ci.yml)
[![Python Version](https://img.shields.io/badge/python-3.15+-blue.svg)](https://pypi.org/project/gcmon/)
[![codecov](https://codecov.io/gh/sergey-miryanov/gcmon/branch/main/graph/badge.svg?token=ZHH7R72OC0)](https://codecov.io/gh/sergey-miryanov/gcmon)
[![CodSpeed](https://img.shields.io/endpoint?url=https://codspeed.io/badge.json)](https://app.codspeed.io/sergey-miryanov/gcmon?utm_source=badge)
[![PyPI Downloads](https://static.pepy.tech/personalized-badge/gcmon?period=total&units=INTERNATIONAL_SYSTEM&left_color=BLACK&right_color=GREEN&left_text=downloads)](https://pepy.tech/projects/gcmon)
[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/sergey-miryanov/gcmon)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

gcmon watches a running Python process's garbage collector from **outside**
the process: no code changes, no callbacks, no in-process overhead. Export to
Perfetto or JSONL; query with PerfettoSQL.

> **Requires CPython 3.15+** for the monitored process and the `gcmon`
> process, built from the same source. See [Limitations](#limitations) for
> details.

## Why gcmon?

Python's garbage collector can introduce unpredictable pauses in applications.
The standard library provides `gc.get_stats()` for aggregate collection
counters and `gc.callbacks` for per-event hooks, but both run inside the
target process: callbacks add execution overhead that distorts timing, while
`gc.get_stats()` only exposes cumulative counters with no per-pause
resolution. Neither can monitor a process without modifying its code.

Most monitoring tools report the **GC collection count**, how often the
collector ran. What hurts a latency-sensitive service is **GC pause time**,
how long each collection held it up, and reporting that requires a source
inside CPython's own GC bookkeeping. See
[Alternatives Comparison](#alternatives-comparison) for what each tool
reports.

gcmon reads GC statistics directly from a target process's memory using
platform-specific memory access APIs. The target process is never paused (GC
statistics are written to a ring buffer and read as a whole), so there is zero
in-process overhead and no code changes required.

Use it to profile GC pause times, compare live-object count and RSS trends, or
integrate GC metrics into benchmarks.

## Features

- **Real-time GC monitoring** - Track garbage collection events in running
  Python processes without in-process overhead
- **Multiple export formats** - Perfetto binary protobuf, JSONL file, and
  JSONL to stdout
  ([formats](https://github.com/sergey-miryanov/gcmon/blob/main/docs/formats.md))
- **CLI** - Monitor processes or run scripts with GC monitoring
  ([usage](https://github.com/sergey-miryanov/gcmon/blob/main/docs/cli.md))
- **RSS tracking** - Track Resident Set Size of monitored processes in a
  Perfetto trace
  ([details](https://github.com/sergey-miryanov/gcmon/blob/main/docs/rss.md))
- **Pyperf hook integration** - Seamlessly integrate with pyperf benchmarks
  ([pyperf hook](https://github.com/sergey-miryanov/gcmon/blob/main/docs/pyperf.md))

## When to Use

**Use gcmon when you want to:**

- Profile GC pause times in production or staging without modifying
  application code
- Measure GC impact on latency-sensitive services (APIs, real-time systems)
- Correlate GC activity with benchmark results via the pyperf hook
- Track live object count trends over time across running processes
- Debug intermittent latency spikes suspected to be GC-related

**Use something else when you need to:**

- Find which code paths trigger collections: use
  [`profiling.sampling`](https://docs.python.org/3.15/library/profiling.sampling.html),
  or [`austin`](https://github.com/P403n1x87/austin) with `-g` on interpreters
  older than 3.15 (statistical, no per-pause timing or heap data)
- In-process GC callbacks (e.g., triggering actions on collection): use
  [`gc.callbacks`](https://docs.python.org/3/library/gc.html#gc.callbacks)
- Cumulative collection counters without per-pause detail: use
  [`gc.get_stats()`](https://docs.python.org/3/library/gc.html#gc.get_stats)
- Monitor across different Python builds: gcmon requires the exact same binary
  (see [Limitations](#limitations))

## Alternatives Comparison

| Tool | GC Pause Time | Code Changes | Overhead | Best Use Case |
|---|---|---|---|---|
| gcmon¹ | Yes (exact) | None | Zero in-process | Production GC monitoring |
| [`profiling.sampling`](https://docs.python.org/3.15/library/profiling.sampling.html)², [`austin`](https://github.com/P403n1x87/austin) | Partial³ | None | Near-zero in-process | Which code triggers GC |
| `gc.callbacks` | Yes (exact) | High (custom code) | Moderate (Python call) | Custom metrics pipelines |
| `gc.get_stats()` | No (cumulative only) | Minimal | Minimal | Basic counters |
| APM agents (Datadog, New Relic, Dynatrace) | Varies⁴ | Agent required | Moderate | Distributed tracing |
| [OpenTelemetry runtime metrics](https://opentelemetry-python-contrib.readthedocs.io/en/latest/instrumentation/system_metrics/system_metrics.html) | No (counts only⁵) | Wrapper or SDK | Low | Fleet-wide GC counters |

¹ Requires CPython 3.15+ on both sides, built from the same source. See
[Limitations](#limitations).

² Stdlib from CPython 3.15 on; austin covers older interpreters.

³ Both mark the samples taken during a collection (`<GC>` frames, austin's
`-g`), which gives GC as a share of samples and the stacks behind it, but no
per-pause durations and no heap data.

⁴ Datadog and New Relic ship theirs off by default: Datadog reports
per-generation collection counts, New Relic per-generation pause time via
`gc.callbacks`. Dynatrace collects GC activity per generation.

⁵ Reports collection counts (`cpython.gc.collections` and friends), not
durations. Platforms that bundle OTel, Odigos among them, forward the same
counters. eBPF sensors such as Groundcover's watch kernel events, not
CPython's GC phases.

> Exact GC pause time has only two sources: `gc.callbacks` inside the process,
> whether your own or an agent's, and `_remote_debugging` reading CPython's
> ring buffer from outside it. Everything else samples or counts. gcmon builds
> on the latter.

### Decision Guide

**GC pauses**: *My service stalls and I suspect the collector.* → Run
**gcmon** against the PID for exact per-pause timings.

**GC origin**: *I know collections are costly, but not what triggers them.* →
Sample the process with
[`profiling.sampling`](https://docs.python.org/3.15/library/profiling.sampling.html)
and read its `<GC>` frames.

## How It Works

gcmon runs **outside** the target process. It reads GC statistics directly
from the process's memory using platform-specific memory access APIs
(available in CPython 3.15+).
[How gcmon reads a process](https://github.com/sergey-miryanov/gcmon/blob/main/docs/monitoring.md)
covers the polling loop, what it misses, and what it recovers.

For the pyperf hook integration, gcmon uses an **external process model**:

1. You start the monitor yourself, over the whole suite: `gcmon run`
2. That process reads the benchmark process's memory directly
3. The hook marks where each benchmark ran, in the trace the monitor writes

This provides zero in-process overhead during benchmarks, crash isolation
(gcmon crashes don't affect the target), and clean separation of concerns.

## Limitations

### Same Python version and build

The monitoring and monitored processes must use the **exact same Python
version and build**. `gcmon` reads GC statistics directly from the target
process's in-memory data structures, and the layout of these structures varies
between Python versions and build configurations (fields, offsets, sizes).
Mismatched binaries are rejected by the Python runtime to prevent undefined
behavior or crashes.

In practice, run both processes from the same virtualenv, container image, or
`pyenv`/`uv` environment so they share a single Python binary.

### Sub-step breakdown requires a custom build

The per-phase GC breakdown visible in the screenshot below (Mark Alive, Fill
increment, Deduce Unreachable, and the fields that accompany it) is only
produced when the monitored process runs a CPython build with enhanced GC
instrumentation. Standard CPython builds give you the top-level GC Pause
slices and counter data only. See
[Output formats](https://github.com/sergey-miryanov/gcmon/blob/main/docs/formats.md)
for which fields need which build.

### Not every GC run is read

CPython writes one record per finished GC run into a small fixed ring buffer,
so a target whose collector runs more often than gcmon polls loses records
before any poll reads them. A GC-heavy workload at default settings can sit
there for most of a run.

gcmon reconstructs what it missed from CPython's cumulative counters, so
**`Count` and `Sum` in the `--stats` table cover every run**, read or not, and
the `Cov` column reports what share gcmon read. **Percentiles are not
corrected and read high.** The trace draws each blind interval on a `GC Loss`
track. See
[How gcmon reads a process](https://github.com/sergey-miryanov/gcmon/blob/main/docs/monitoring.md)
for the mechanism and
[Statistics](https://github.com/sergey-miryanov/gcmon/blob/main/docs/statistics.md)
for how to read a low-coverage table.

### No call-stack attribution

gcmon reports when each GC run happened, how long it took, and how large the
heap was, plus a per-phase breakdown on a custom CPython build with enhanced
GC instrumentation (see [above](#sub-step-breakdown-requires-a-custom-build)).
It cannot tell you which code triggered the run, because the GC records carry
no stack information. A sampler answers that question, so the two pair well:
see [Alternatives Comparison](#alternatives-comparison).

### No OS-level memory pressure

gcmon reports the collector's view of the heap, plus RSS samples when `--rss`
is enabled. Neither is a measure of OS-level memory pressure. Use `psutil`,
Prometheus node exporters, or eBPF tooling for that.

## Requirements

- **Python**: CPython 3.15 or newer is required for both the monitoring and
  the monitored process.
- **Operating systems**: Linux, macOS, and Windows are supported (the test
  matrix runs on `ubuntu-latest`, `macos-latest`, and `windows-latest`).
- **Process access**: gcmon reads another process's memory using
  platform-specific APIs. On Linux and Windows no extra setup is needed. On
  **macOS**, the calling process must be authorized to read the target process
  memory.

## Installation

```bash
pip install gcmon

# With optional extras
pip install gcmon[stats]      # High-accuracy statistics (see docs/statistics.md)
pip install gcmon[cmdline]    # Process command line and RSS tracking (see docs/rss.md)
pip install gcmon[stats,cmdline]  # Both extras
```

`[stats]` installs [DDSketch](https://github.com/DataDog/sketches-py) for
high-accuracy, memory-efficient percentiles; see
[Statistics](https://github.com/sergey-miryanov/gcmon/blob/main/docs/statistics.md).
`[cmdline]` installs [psutil](https://github.com/giampaolo/psutil), which
populates process command lines in Perfetto traces and enables `--rss`; see
[RSS Tracking](https://github.com/sergey-miryanov/gcmon/blob/main/docs/rss.md).
Each extra degrades gracefully when absent; no other trace data is affected.

## Quick Start

```bash
# Monitor a running process by PID (default Perfetto format)
gcmon monitor 12345

# Run a Python script with GC monitoring
gcmon run -s my_script.py

# Monitor with custom output and statistics output
gcmon monitor 12345 -o trace.pftrace --stats=total

# Perfetto binary output with RSS tracking
gcmon monitor 12345 --format perfetto -o trace.pftrace --rss

# Combine multiple JSONL captures (e.g. different runs or builds) into one trace
gcmon combine trace1.jsonl trace2.jsonl -o combined.pftrace -n
```

### Example: Perfetto Trace Output

<img src="https://raw.githubusercontent.com/sergey-miryanov/gcmon/main/docs/images/chrome-trace-example.png" alt="Perfetto Trace Example" width="800">

*GC monitoring data visualized in Perfetto UI:*
- *GC Pause slices with sub-step breakdown, and per-gen `G{gen}` counter
  tracks*
- *A shared `heap_size` counter and a `Processes` lifetime track*
- *An `rss` counter track per PID (when `--rss` is enabled)*

See
[Output formats](https://github.com/sergey-miryanov/gcmon/blob/main/docs/formats.md)
for the full track inventory and the JSONL event schema.

## See Also

The tools weighed in [Alternatives Comparison](#alternatives-comparison), and
the viewer gcmon writes for:

- [`profiling.sampling`](https://docs.python.org/3.15/library/profiling.sampling.html):
  stdlib statistical profiler, Tachyon (out-of-process, `<GC>` frames but no
  per-pause timing)
- [`austin`](https://github.com/P403n1x87/austin): sampling CPU/memory
  profiler (out-of-process, `-g` tags GC samples on interpreters older than
  3.15)
- [`gc.callbacks`](https://docs.python.org/3/library/gc.html#gc.callbacks):
  in-process hook, the other exact source of pause time
- [`gc.get_stats()`](https://docs.python.org/3/library/gc.html#gc.get_stats):
  cumulative per-generation counters, no per-pause detail
- [OpenTelemetry runtime metrics](https://opentelemetry-python-contrib.readthedocs.io/en/latest/instrumentation/system_metrics/system_metrics.html):
  fleet-wide GC collection counts
- [Perfetto UI](https://ui.perfetto.dev): the trace viewer used by gcmon's
  Perfetto exporter

## Project documentation

- [gcmon documentation](https://github.com/sergey-miryanov/gcmon/blob/main/docs/README.md):
  CLI reference, output formats, statistics, RSS tracking, the pyperf hook,
  programmatic control, Perfetto SQL, architecture decision records, and the
  release process

## License

MIT License - see [LICENSE](LICENSE) for details.

## Contributing

Bug reports and pull requests are welcome at
[GitHub](https://github.com/sergey-miryanov/gcmon/issues). The
[contributing guide](https://github.com/sergey-miryanov/gcmon/blob/main/CONTRIBUTING.md)
covers setting up a working copy, running the tests and the checkers, and what
a pull request needs. Please read the
[AI policy](https://github.com/sergey-miryanov/gcmon/blob/main/.github/AI_POLICY.md)
first.
