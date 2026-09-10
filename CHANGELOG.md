# Changelog

## WIP

### Breaking changes

- A Perfetto trace holds no `thread` row of gcmon's. An interpreter is not an operating-system thread, so every row one owns is a plain custom track. The one row left in `thread` is the nameless one the trace processor builds per process, and `thread.is_main_thread` marks it
- An interpreter's pause row is named `GC Pauses`, under a group named `Interpreter {iid}`, where it was `Thread {iid}` beside the process track
- A query that joined `thread_track` joins `process_track`. Every row gcmon draws carries its process's `upid`
- An interpreter's loss row is named `GC Loss`, under its `Interpreter {iid}` group, where it was `GC Loss {iid}` beside the process track
- A `heap_size` counter track is named `heap_size`, on its `Interpreter {iid}` group, where it was `Thread {iid} heap_size` beside the process track. A query matching `name = 'heap_size'` finds it again

### Bugfixes

- A per-generation counter names the interpreter that owns it. A process running several interpreters draws a `GC Metrics` row each, where the copies used to merge into one row per process holding every interpreter's counters, identically named and unattributable

## Version 0.7.0 (2026-09-09)

### Breaking changes

- Every deep import path changed and the old ones are gone. `from gcmon import ...` still gives the same names
- Drop Chrome Trace format support
- The default output is `gcmon.pftrace`
- `GCMON_FORMAT` takes the same words as `--format`, and any other value stops the run
- A run that read no records and watched no process writes no file, where it used to write an empty `gcmon.json`
- The pyperf hook spawns no monitor and publishes no GC metrics: the suite needs `gcmon run` with `--inherit-environ=GCMON_CONTROL_ADDRESS`
- The pyperf hook's metadata keys `gc_pause_*` and `gc_heap_size_p99` are gone
- The pyperf hook's `GCMON_PYPERF_HOOK_OUTPUT` and `GCMON_PYPERF_HOOK_TEMP_DIR` environment variables are gone
- `gcmon combine` reads JSONL only: `--input-format` is gone
- A compressed `.pftrace` needs Perfetto v58 or newer to open. An older Perfetto shows an empty timeline rather than refusing the file
- A `heap_size` counter track is named `Thread {iid} heap_size`, where it was `heap_size`
- A JSONL capture carries no `tid`, on any kind of line. It was `iid` on a GC record and `-2 - iid` on a loss one
- `gcmon combine` writes no command line
- A `process` row's `pid` in a Perfetto trace is one gcmon writes per process, not the operating system's. The operating system's PID is the row's name and the `pid` annotation on the `Lifetime` bar and on the `Processes` span
- A process track carries no `Start Process` instant

### Features

- A Perfetto trace is written compressed: the same events in a smaller file, and Perfetto opens it directly
- `ControlClient.instant_msg` takes a `ts`: an instant captured in a hot path can be sent after it and still land where it happened
- The pyperf hook marks where each benchmark ran: `gcmon:`-prefixed begin and end marks per measured region
- Each process that held a reused PID gets its own slice on the `Processes` track, the second named `Process 12345#2`, matching the `--stats` block
- Every `Processes` slice carries a `pid_epoch` annotation with the number in its name, and a `clipped` annotation saying whether an overlapping process cut its drawn width short
- A reused PID draws a full set of rows per process, each with its own `upid`
- Each process track draws a `Lifetime` slice over the interval gcmon watched that process. An overlapping process clips the `Processes` slice, never the `Lifetime` one
- A `Lifetime` slice's args name the program, which process on the PID it is, how many of its interpreters collected, and how much of it gcmon read: records sampled against records lost, with the GC pause inside the lost ones
- A process gcmon polled without reading a collection draws a row of its own, where it used to reach the trace as a `Processes` slice and nothing else
- A trace from a run that was killed rather than stopped still shows a row for every process gcmon had finished with. A process still running when the kill lands has no row, and neither does the `Processes` track

### Bugfixes

- `gcmon` with no subcommand prints a usage message and exits 2, where it used to print an `AttributeError` traceback
- An instant sent close to the end of a run reaches the trace, where the last one a client sent could be dropped without a word
- A `Processes` slice names the program its own process was running. A reused PID used to put the first process's command line on every slice of that PID, and a process that exited before the first flush got none at all
- A `Processes` slice covers only the process it names, where a reused PID used to draw one slice spanning both processes and the stretch between them
- A process track names the program its own process was running and opens when that process started, where a reused PID used to carry the first process's command line and a start stamp from before the second existed
- A child that leaves the process tree and comes back draws each of its collections once, where the records it had already drawn used to be drawn a second time
- A process's span ends at the last poll that read that process, where a record arriving after it used to stretch the span further
- The process tracks sort by when gcmon first observed each process, where the order was by first event timestamp and a process gcmon polled without reading a collection had no place in it
- No two process tracks share a position, where two rows could land at the same rank and leave their order to the UI

### Internal

- `gcmon.TraceExporter` is gone from the public surface
- `gcmon.exporters` no longer re-exports `combine_files` or `convert_jsonl_to_trace_format`
- Removed OS from the coverage report name
- Stability, correctness and performance improvements


## Version 0.6.0 (2026-08-21)

### Breaking changes

- `GC Loss` slice args drop the `missing_` prefix for the `lost_` one
- Bare `--stats` is a parse error: the flag requires a value, and `GCMON_STATS` takes the same words, so `GCMON_STATS=1` stops the run
- The `--stats` table reports one block per interpreter, not one per process: `PID:IID` heads every row, `12345:0` included. `Total` is the only blended row
- The low-coverage warning measures each interpreter on its own and names the least covered. It fires where a busy interpreter used to lift the whole PID over the 90% floor
- `--rate` and `GCMON_RATE` take a plain decimal number of seconds, `0.001` or more: `1e-3` and `0.0005` are refused where they used to be accepted

### Features

- `--stats` takes the view to print: `total` for the run-wide block, `full` for that plus one block per interpreter, `no`/`off`/`false`/`0` for no table
- The low-coverage warning drops the ring-buffer explanation and suggests a smaller `--rate`
- The end-of-run summary counts the events gcmon reconstructed and the share it observed: `Total events: 1234 (+8566 reconstructed, 12.6% observed)`
- The end-of-run summary reports the ticks that ran against the ticks scheduled, `Ticks: 188 of 600 scheduled`, and says whether polling more often can help a lossy run
- The lifetime note under the `--stats` table says what it covers: `summed over 3 interpreters in 2 processes`
- An interpreter's statistics settle when its process exits: its percentiles cover its whole life
- Each process that held a reused PID gets its own `--stats` block, the second headed `12345:0#2`
- Warn when an interpreter gets no row because 256 were already running, and count the ones left out in a footer note. Their records still reach `Total`
- `gcmon --version` prints the installed version

### Bugfixes

- `--rate` is the interval between poll starts, not the wait after each poll: a wide process tree used to stretch every interval by what its reads took
- `gcmon.__version__` reports the installed version; it had said `0.1.0` since `0.2.0`
- Stop a reused PID inheriting its predecessor's `--stats` row, which put two processes' records under one heading
- Stop a reused PID's lifetime totals overwriting its predecessor's, which made the note under the table drop mid-run

### Internal

- Stability, correctness and performance improvements

## Version 0.5.0 (2026-08-14)

### Breaking changes

- Slice names drop the `gen=` prefix: `GC Pause(0)`, `GC Loss(0)`, `Mark Alive(0)` and the rest. Categories are unchanged, so `gc.pause(gen=0)` still matches
- A `Processes` slice now spans how long the process was alive, not how long it was collecting (`real_start_ts` / `real_end_ts` annotations)
- `Count` and `Sum` now include the collections gcmon missed, in the `--stats` table and the pyperf `gc_pause_gen_N_count` / `_sum` / `gc_pause_count` metrics

### Features

- Detect GC records the target ran without gcmon reading them, and draw each blind poll interval as a `GC Loss` slice named for the generations that lost records (`GC Loss(0,2)`)
- `GC Loss` slices carry the interval's coverage, missing collection counts and missing pause time
- Add `Cov` and `F` columns to the `--stats` table and a `gc_pause_gen_N_coverage` pyperf metric
- Show `Count` and `Sum` as `sampled/exact`, with a leading `~` where the second number is `F`-scaled
- Warn once per run when coverage falls below 90%
- Report per-generation totals since the interpreter started, as `gc_pause_gen_N_lifetime_count` / `_lifetime_sum`
- Write a `Processes` slice for a process that never collected, and a trace for a run in which nothing collected

### Bugfixes

- Fix GC events discarded by the poll loop
- Fix a reused PID inheriting its predecessor's GC counts

### Documentation

- Add [ADR-0015](docs/adr/0015-gc-loss-spans-on-their-own-track.md) on the `GC Loss` track
- Add `docs/monitoring.md` on how gcmon collects the GC record stream and why records go missing
- Rewrite `docs/statistics.md` for `GC Loss` support
- Document the `GC Loss` track and spans in `docs/formats.md`
- Document the changed pyperf metrics, and that the lifetime metrics are not benchmark-scoped
- Add a README limitation for the ring-buffer bound and the read-cost floor

## Version 0.4.0 (2026-07-31)

### Breaking changes

- Remove `MonitorThread` (#63); use `MonitorLoop` instead
- Replace `gcmon.data.dur_to_us(ts_start_ns, ts_stop_ns)` with `gcmon.data.dur_to_ms(dur_ns)`

### Features

- Track RSS (Resident Set Size) of monitored processes in Perfetto traces (#55)
- Add `--rss` / `--rss-interval` CLI flags and `GCMON_RSS` / `GCMON_RSS_INTERVAL` env vars (#55)
- Add a `Read Time` row to the `--stats` table: the time each poll spends reading GC stats from the target

### Bugfixes

- Fix under-reported GC activity for child processes
- Fix doubled `Count` and `Sum` in the `--stats` table's `GC Pause` rows
- Fix `--rss` samples discarded with `--format chrome+perfetto`
- Warn that `--rss` has no effect with `jsonl` or `stdout`
- Wait for process termination before reading the return code (#65)

### Documentation

- Correct the JSONL `duration` units (seconds, not milliseconds) and the pyperf `gc_pause_*` units (milliseconds, not microseconds)
- Clarify that `gc_heap_size_p99` is a percentile over per-process peak live object counts, not over all samples
- Document the pyperf `gc_pause_count` metric
- Split the README into per-topic guides under `docs/`, indexed by `docs/README.md`, and add architecture decision records under `docs/adr/`
- Document where a Perfetto trace carries process command lines, with SQL for both forms
- Fix the screenshot URLs so they render on the PyPI project page

## Version 0.3.1 (2026-06-29)

### Bugfixes

- Fix PyPI classifiers

## Version 0.3.0 (2026-06-29)

### Breaking changes

- `TraceEvent.ts` is now stored in nanoseconds (was microseconds); fixes a 1000x compression bug in `ui.perfetto.dev`
- Chrome trace exporter now emits duration events (`B`/`E`) instead of complete events (`X`)
- Per-gen `G{gen}` counters now carry `collected`, `candidates`, `duration` and `uncollectable` (when non-zero), grouped under `GC Metrics`; `heap_size` is a single top-level counter per `(pid, tid)`
- Several metrics moved from counter events to slice args: `increment_size` on `GC Pause` / `Fill increment`, `candidates` on `Deduce Unreachable`, and `finalized_garbage_count` / `deleted_garbage_count` / `clear_weakrefs_count` on their own sub-step slices; `alive_size` is no longer a counter
- Remove `PollStatus.INVALID_PYTHON`, merged into `INVALID_PROCESS` (#32)

### Features

- `gcmon combine` supports `--output-format perfetto` for binary protobuf output, from chrome and jsonl inputs
- `gcmon monitor` / `run` support `--format chrome+perfetto`, writing both `<base>.json` and `<base>.pftrace`
- Add a top-level Perfetto `Processes` track holding one slice per pid, spanning its first-to-last event, named `Process <pid>` and carrying a `cmdline` annotation. Perfetto-only
- Emit a `Start Process` instant on each process track so its cmdline stays visible in the Perfetto UI
- Order Perfetto process tracks by first event timestamp. Needs trace processor 0.57+ and the canary UI channel
- Perfetto counter tracks sharing a metric name now share a Y-axis
- Add a per-PID wait policy

### Bugfixes

- Fix `GCMON_FORMAT=perfetto` falling back to `chrome`
- Fix `ControlServer` closing if not started, and leaking a `Listener` on failure
- Fix the `Processes` track slice END position; it is now emitted once at encoder close

## Version 0.2.0 (2026-06-10)

### Features

- Perfetto binary protobuf export (#25)
- Control plane IPC for start/stop from child process (#14, #16, #21)
- Extra GC counters and runtime data (#22, #23)
- Timestamp normalization per PID (#24)


## Version 0.1.0 (2026-05-22)

### Features

- Real-time GC monitoring via CPython `_remote_debugging` extension (3.15+)
- Chrome Trace Event format export (https://ui.perfetto.dev)
- JSONL export to file and stdout
- CLI with `monitor` (attach to PID), `run` (spawn + monitor), `combine` (merge traces)
- Streaming statistics with optional `DDSketch` percentile accuracy
- Pyperf hook integration for benchmark profiling
