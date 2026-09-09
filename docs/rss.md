# RSS Tracking

RSS (Resident Set Size) tracking samples the physical memory usage of each
monitored process and emits it as a process-level counter track.

Supported by the `perfetto` format. The `jsonl` and `stdout` formats discard
RSS samples; `--rss` logs a warning when combined with them.

## How to Use

```bash
# Enable RSS tracking with Perfetto output (default 1s interval)
gcmon monitor 12345 --format perfetto -o trace.pftrace --rss

# Custom sampling interval
gcmon monitor 12345 --format perfetto --rss --rss-interval 0.5
```

## The `[cmdline]` extra

RSS tracking requires [psutil](https://github.com/giampaolo/psutil), which
ships with the `[cmdline]` extra:

```bash
pip install gcmon[cmdline]
```

When this extra is installed:
- gcmon reads the command line of each monitored process and the Perfetto
  trace records it, where it labels the process track and is queryable from
  SQL; see [Process command lines](formats.md#process-command-lines).
- RSS tracking (`--rss`) can sample Resident Set Size via
  `psutil.Process(pid).memory_info().rss`.

Without this extra, the `cmdline` field is omitted and `--rss` is silently
ignored (an info log is emitted at startup). All other trace data is
unaffected.

## How It Works

- RSS sampling runs inside the GC poll loop, so its effective rate is capped
  by `--rate`. If `--rss-interval` is shorter than `--rate`, a warning is
  logged and RSS is sampled at the poll rate. Samples are evenly spaced,
  because the poll loop holds its schedule.
- Only PIDs that returned a successful GC poll status are sampled; no stale
  data for dead processes.
- The counter track belongs to the process, so it is parented directly to the
  process track, outside the `GC Metrics` group.
- The `rss` counter track displays in the Perfetto UI with the name `"rss"`
  and the value in bytes.
- Graceful degradation: if `psutil` is not installed, `--rss` is ignored
  without crashing.

## SQL Query Example

See
[Example: Querying RSS Values](perfetto-sql.md#example-querying-rss-values).
