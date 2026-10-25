# CLI Usage

`gcmon` requires one of three subcommands: `monitor`, `run` and `combine`.

## What you'll see

gcmon stays quiet by default: it writes the trace to a file and exits when the
target ends or you press `Ctrl+C`. `-v` follows progress:

```bash
$ gcmon monitor 12345 -v
[gcmon] INFO: Output: gcmon.pftrace
[gcmon] INFO: Format: perfetto
[gcmon] INFO: Rate: 0.1s
[gcmon] INFO: Duration: until interrupted (Ctrl+C)
[gcmon] INFO: Self PID: 38752
[gcmon] INFO: Running server on <control address>
[gcmon] INFO: Monitoring PID: 12345
...
[gcmon] INFO: Monitoring complete.
[gcmon] INFO: Total events: 27
[gcmon] INFO: Ticks: 28 of 30 scheduled
[gcmon] INFO: Trace saved to: gcmon.pftrace
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
| `--layout` | both | Perfetto layout: `full` draws the run rows beside a row per process, `process` the rows per process alone. With another format it warns and is ignored | `full` |
| `--flush-threshold` | both | Number of events to buffer before flushing | `100` |
| `--stats <view>` | both | Show a statistics table at end of monitoring. The value is required: `total`, `full`, or one of `no`/`off`/`false`/`0` (see [`--stats`](#--stats)) | No table |
| `--table-format` | both | Table format: `plain` or `markdown`/`md` | `plain` |
| `--control-name` | both | Name the control plane, so a target can build its address (see [`--control-name`](#--control-name)) | The OS picks one |
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

### `--control-name`

The monitor listens on a control plane, and a `ControlClient` inside the
target sends to it ([Programmatic Control](control-plane.md)). Naming it fixes
the address:

| Platform | Address for `--control-name my-app` |
|---|---|
| Windows | `\\.\pipe\gcmon-my-app` |
| Linux, macOS | `/tmp/gcmon-my-app` |

Unset, `multiprocessing` picks a random address that a target cannot
reconstruct. `gcmon run` needs no name: it starts the target and passes the
address down in `GCMON_CONTROL_ADDRESS`.

## Environment Variables

Each variable below sets a default for its flag, and a flag on the command
line beats it. A value a variable cannot read falls back to the default. The
exceptions are `GCMON_FORMAT`, `GCMON_LAYOUT`, `GCMON_RATE` and `GCMON_STATS`,
which stop the run.

| Variable | Equivalent flag | Description | Default |
|----------|----------------|-------------|---------|
| `GCMON_OUTPUT` | `-o, --output` | Output file path for trace data | `gcmon.pftrace`, `gcmon.jsonl` for `--format jsonl` |
| `GCMON_RATE` | `-r, --rate` | Seconds between poll starts, as a plain decimal, `0.001` or more | `0.1` |
| `GCMON_DURATION` | `-d, --duration` | Monitoring duration in seconds | Until interrupted / script exits |
| `GCMON_VERBOSE` | `-v, --verbose` | Verbose level (integer or truthy value) | `0` |
| `GCMON_FORMAT` | `--format` | Output format: `perfetto`, `jsonl`, or `stdout`. Any other value stops the run | `perfetto` |
| `GCMON_LAYOUT` | `--layout` | Perfetto layout: `full` or `process`. Blank reads as unset; any other value stops the run | `full` |
| `GCMON_FLUSH_THRESHOLD` | `--flush-threshold` | Number of events to buffer before flushing | `100` |
| `GCMON_STATS` | `--stats` | Statistics table view, in the words [`--stats`](#--stats) takes. Blank reads as unset; any other value stops the run | No table |
| `GCMON_TABLE_FORMAT` | `--table-format` | Table format: `plain`, `md`, or `markdown` | `plain` |
| `GCMON_CONTROL_NAME` | `--control-name` | Name the control plane | The OS picks one |
| `GCMON_RSS` | `--rss` | Enable RSS tracking (`1`, `true`, `yes`, `on`) | `False` |
| `GCMON_RSS_INTERVAL` | `--rss-interval` | RSS sampling interval in seconds | `1.0` |

## combine

Combine JSONL captures into one trace, or into one JSONL capture.

```bash
gcmon combine trace1.jsonl trace2.jsonl -o combined.pftrace

# `-n` starts each input file's timeline at t=0
gcmon combine trace1.jsonl trace2.jsonl -o combined.pftrace -n

gcmon combine trace1.jsonl --output-format jsonl -o combined.jsonl
```

| Option | Description | Default |
|--------|-------------|---------|
| `inputs` (required) | One or more input JSONL captures | - |
| `-o, --output` (required) | Output file path for the combined trace | - |
| `--output-format` | Output format: `perfetto` or `jsonl` | `perfetto` |
| `-n, --normalize` | Normalize timestamps so each timeline starts at 0. The scope differs by output: per input file for `perfetto`, per PID across the whole merge for `jsonl` | `False` |

## extract

Write a trace of pyperformance as a CSV table, one row per record, naming the
benchmark each record's process ran from pyperformance's results file. A
`.meta.json` beside the table says where it came from. It needs the `analysis`
extra.

```bash
pip install gcmon[analysis]

# Reads run.json and writes run.csv and run.meta.json, all beside the trace
gcmon extract run.pftrace

gcmon extract run.pftrace -o records.csv --results suite.json --overwrite \
    --benchmark async_tree_io_tg --benchmark xml_etree_parse \
    --meta gcmon_version=0.9.1
```

| Option | Description | Default |
|--------|-------------|---------|
| `trace` (required) | A trace gcmon wrote | - |
| `-o, --output` | Output CSV path | The trace's path with `.csv` in place of its suffix |
| `--results` | pyperformance's results file for the run, plain or `.gz`. A missing one stops the command | The trace's path with `.json` in place of its suffix |
| `--benchmark NAME` | Keep only the rows of the benchmark the results file calls *NAME*; repeat for more. A name the results file lacks stops the command | Every process's rows |
| `--drop COLUMN[,COLUMN...]` | Leave these columns out of the table; repeat for more. Not with `--keep`. An unknown column, or dropping them all, stops the command | Every column |
| `--keep COLUMN[,COLUMN...]` | Write only these columns; repeat for more. The columns keep the table's order whatever order they are given in. Not with `--drop`. An unknown column stops the command | Every column |
| `--meta KEY=VALUE` | Add *KEY* to the `.meta.json`; repeat for more. A key gcmon derives stops the command | - |
| `--overwrite` | Replace an existing output and its `.meta.json`. Without it, either one existing stops the command, and both are left as they were | `False` |

The first run downloads Perfetto's v58.2 trace processor and keeps it for
later runs. [Records table](formats.md#records-table) describes the columns
and the `.meta.json`.

## --version

```bash
$ gcmon --version
0.7.0
```

The version of the gcmon you are running, read from the installed
distribution's metadata. `gcmon.__version__` gives a Python process the same
string. Quote it in a bug report.

An editable install whose metadata predates the last `pyproject.toml` bump
reports the older number, as `pip show gcmon` does. Reinstall to move it.
