# CLI Usage

`gcmon` requires one of three subcommands: `monitor`, `run` and `combine`.

## What you'll see

gcmon stays quiet by default: it writes the trace to a file and exits when the
target ends or you press `Ctrl+C`. `-v` follows progress:

```bash
$ gcmon monitor 12345 -v
[INFO] monitoring PID 12345 (perfetto trace → gcmon.pftrace)
[INFO] collected 42 GC events so far
...
[INFO] stopping (Ctrl+C)
[INFO] wrote 42 events to gcmon.pftrace
```

Open the output in [Perfetto UI](https://ui.perfetto.dev), whose SQL panel
queries the trace directly; see
[Trace Analysis with Perfetto SQL](perfetto-sql.md).

## monitor

Monitor a running process by PID.

```bash
# Until interrupted, Perfetto format
gcmon monitor 12345

gcmon monitor 12345 -o gc_trace.pftrace
gcmon monitor 12345 -d 30 -v

# 100 polls a second
gcmon monitor 12345 --output trace.pftrace --rate 0.01
```

## run

Run a Python script or module with GC monitoring enabled.

**Important:** everything after `-s`/`--script` or `-m`/`--module` reaches the
target verbatim, so gcmon's own options go first.

```bash
gcmon run -s my_script.py

# A module, as `python -m` takes it
gcmon run --stats=full --table-format md -m test test_gc -v

# `--iterations 1000 --verbose` belong to benchmark.py, not to gcmon
gcmon run -s benchmark.py --iterations 1000 --verbose

gcmon run --format jsonl -o trace.jsonl --stats=total -m http.server 8000
```

Exactly one of `-s`/`--script` or `-m`/`--module`.

## Options for `monitor` and `run`

| Option | Applies to | Description | Default |
|--------|------------|-------------|---------|
| `pid` (required) | `monitor` | Process ID to monitor | - |
| `-s, --script <path>` | `run` | Python script path to run | - |
| `-m, --module <name>` | `run` | Module name to run (like `python -m`) | - |
| `-o, --output` | both | Output file path for trace data | `gcmon.pftrace`, or `gcmon.jsonl` when `GCMON_FORMAT=jsonl` |
| `-r, --rate` | both | Seconds between poll starts, as a plain decimal, `0.001` or more | `0.1` |
| `-d, --duration` | both | Monitoring duration in seconds | Until interrupted / script exits |
| `-v, --verbose` | both | Enable verbose output (`-v` for INFO, `-vv` for DEBUG) | `0` |
| `--format` | both | Output format: `perfetto`, `jsonl` or `stdout` (see [Output formats](formats.md)) | `perfetto` |
| `--flush-threshold` | both | Number of events to buffer before flushing | `100` |
| `--stats <view>` | both | Show a statistics table at end of monitoring. The value is required: `total`, `full`, or one of `no`/`off`/`false`/`0` (see [`--stats`](#--stats)) | No table |
| `--table-format` | both | Table format: `plain` or `markdown`/`md` | `plain` |
| `--rss` | both | Track the target's Resident Set Size. `perfetto` only, and needs the `[cmdline]` extra (see [RSS Tracking](rss.md)) | `False` |
| `--rss-interval` | both | RSS sampling interval in seconds | `1.0` |

### `--stats`

The value is required, and it is one of these words:

| Value | Prints |
|-------|--------|
| `total` | the run-wide `Total` block, `Read Time` and the footer |
| `full` | that, plus one block per interpreter |
| `no`, `off`, `false`, `0` | no table |

Bare `--stats` is a parse error, and so is any word outside the table.
[Statistics](statistics.md) reads the two views.

`GCMON_STATS` takes the same words. Blank reads as unset, and anything else
stops the run at startup.

## Environment Variables

Each variable below sets a default for its flag, and a flag on the command
line beats it. A value a variable cannot read falls back to the default. The
exception is `GCMON_STATS`, which stops the run.

| Variable | Equivalent flag | Description | Default |
|----------|----------------|-------------|---------|
| `GCMON_OUTPUT` | `-o, --output` | Output file path for trace data | `gcmon.pftrace`, `gcmon.jsonl` for `--format jsonl` |
| `GCMON_RATE` | `-r, --rate` | Seconds between poll starts, as a plain decimal, `0.001` or more | `0.1` |
| `GCMON_DURATION` | `-d, --duration` | Monitoring duration in seconds | Until interrupted / script exits |
| `GCMON_VERBOSE` | `-v, --verbose` | Verbose level (integer or truthy value) | `0` |
| `GCMON_FORMAT` | `--format` | Output format: `perfetto`, `jsonl`, or `stdout`. Any other value stops the run | `perfetto` |
| `GCMON_FLUSH_THRESHOLD` | `--flush-threshold` | Number of events to buffer before flushing | `100` |
| `GCMON_STATS` | `--stats` | Statistics table view, in the words [`--stats`](#--stats) takes. Blank reads as unset; any other value stops the run | No table |
| `GCMON_TABLE_FORMAT` | `--table-format` | Table format: `plain`, `md`, or `markdown` | `plain` |
| `GCMON_RSS` | `--rss` | Enable RSS tracking (`1`, `true`, `yes`, `on`) | `False` |
| `GCMON_RSS_INTERVAL` | `--rss-interval` | RSS sampling interval in seconds | `1.0` |

## combine

Combine JSONL captures into one trace, or into one JSONL capture.

```bash
gcmon combine trace1.jsonl trace2.jsonl -o combined.pftrace

# `-n` starts every process at t=0
gcmon combine trace1.jsonl trace2.jsonl -o combined.pftrace -n

gcmon combine trace1.jsonl --output-format jsonl -o combined.jsonl
```

| Option | Description | Default |
|--------|-------------|---------|
| `inputs` (required) | One or more input JSONL captures | - |
| `-o, --output` (required) | Output file path for the combined trace | - |
| `--output-format` | Output format: `perfetto` or `jsonl` | `perfetto` |
| `-n, --normalize` | Normalize timestamps per PID so each process timeline starts at 0 | `False` |

## --version

```bash
$ gcmon --version
0.5.0
```

The version of the gcmon you are running, read from the installed
distribution's metadata. `gcmon.__version__` gives a Python process the same
string. Quote it in a bug report.

An editable install whose metadata predates the last `pyproject.toml` bump
reports the older number, as `pip show gcmon` does. Reinstall to move it.
