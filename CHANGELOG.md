# Changelog

## WIP

### Breaking changes

- The modules moved into layers, so every deep import path changed and the old ones are gone. `from gcmon import ...` still gives the same names
- `--format chrome`, `--format trace` and `--format chrome+perfetto` are parse errors: the flag takes `perfetto`, `jsonl` or `stdout`. A `.json` capture from an earlier release still opens in the Perfetto UI
- The default output is `gcmon.pftrace`, where it was `gcmon.json`. `--format jsonl` still defaults to `gcmon.jsonl`
- `GCMON_FORMAT` refuses a word `--format` would refuse and stops the run, where it used to fall back to the default without saying so
- A run that read no records writes no file, where it used to write an empty `gcmon.json`
- The pyperf hook spawns no monitor and publishes no GC metrics. Running the suite under `gcmon run` with `--inherit-environ=GCMON_CONTROL_ADDRESS` is required now, and the first worker fails the run when no monitor is listening
- The pyperf hook's metadata keys `gc_pause_*` and `gc_heap_size_p99` are gone
- The pyperf hook's `GCMON_PYPERF_HOOK_OUTPUT` and `GCMON_PYPERF_HOOK_TEMP_DIR` environment variables are gone
- `gcmon combine` reads JSONL only: `--input-format` is gone, `--output-format` takes `perfetto` or `jsonl` and defaults to `perfetto`. Handed a `.json` from an earlier release, it names the Chrome format instead of reporting malformed JSON
- A compressed `.pftrace` needs Perfetto v58 or newer to open. An older Perfetto shows an empty timeline rather than refusing the file
- A `heap_size` counter track is named `Thread {iid} heap_size`, where it was `heap_size`. Two interpreters in one process drew two sibling rows under the same name; a PerfettoSQL query matching `name = 'heap_size'` now matches nothing. `rss` is unchanged
- A JSONL capture carries no `tid`, on any kind of line. It was `iid` again on a GC record and `-2 - iid` on a loss one; derive it from `iid` if you read it. gcmon still reads a capture that has it
- `gcmon combine` writes no command line, on the process descriptor or the track description. It used to read whatever process held that PID on the machine running the conversion
- A `process` row's `pid` in a Perfetto trace is one gcmon writes per process, not the operating system's, so a PerfettoSQL query matching `pid = 12345` matches nothing. The `pid` annotation on the `Lifetime` bar and on the `Processes` span carries the operating system's PID, and so does the row's name
- A process track carries no `Start Process` instant. The `Lifetime` slice on the same row keeps the row rendered, so a PerfettoSQL query matching `name = 'Start Process'` matches nothing. It opens at gcmon's first observation of the process, at or before the timestamp the instant carried

### Features

- A Perfetto trace is compressed: the same events in a file several times smaller. It opens the same way, and there is nothing to run first
- `ControlClient.instant_msg` takes a `ts`, so an instant captured in a hot path can be sent after it and still land where it happened
- The pyperf hook marks where each benchmark ran: `gcmon:`-prefixed begin and end marks per measured region
- A capture from the new incremental collector carries the collector's state: `old_work`, `next_gen`, `survivor_count`, `aging_threshold`, `aging_spaces` and `aging_next` on the `GC Pause` args and in a JSONL line. All but `next_gen` draw a counter track too, `old_work` beside `heap_size` and the rest inside `GC Metrics`, joined there by `new_increment_size`
- Each process that held a reused PID gets its own slice on the `Processes` track, the second named `Process 12345#2`, matching the `--stats` block. Every slice carries a `pid_epoch` annotation, so a PerfettoSQL query reads the number without parsing the name, and a `clipped` annotation saying whether an overlapping process cut this one's drawn width short
- Each process that held a reused PID draws its own rows: a `Process 12345#2` process track beside `Process 12345`, with its own pause row, `GC Loss` row and counters. In PerfettoSQL the two are separate `upid`s, and a per-process figure is a `GROUP BY`
- Each process track draws a `Lifetime` slice over the interval gcmon watched that process. Click it and the args say what the process was running, which process on the PID it is, how many of its interpreters collected, and how much of it gcmon read: records sampled against records lost, with the GC pause inside the lost ones. It keeps the observed width where the same process's `Processes` slice was cut short by an overlapping one, so the row says how long gcmon watched rather than only that the process existed
- A process gcmon polled and read no collections from draws a row of its own, with its command line and a `Lifetime` bar over the interval it was watched. It used to reach the trace as a `Processes` slice and nothing else, indistinguishable in the UI from a process gcmon never reached
- A trace from a run that was killed rather than stopped still shows a row for every process gcmon had finished with. Their rows are written as gcmon lets go of each pid, not at the end of the run; a process still running when the kill lands has no row, and neither does the `Processes` track

### Bugfixes

- `gcmon` with no subcommand prints a usage message and exits 2, where it used to print an `AttributeError` traceback
- An instant sent close to the end of a run reaches the trace, where the last one a client sent could be dropped without a word
- A `Processes` slice names the program its own process was running. A reused PID used to put the first process's command line on every slice of that PID, and a process that exited before the first flush got none at all
- A `Processes` slice covers only the process it names, where a reused PID used to draw one slice spanning both and the stretch between them
- A process track names the program its own process was running and opens when that process started. A reused PID used to draw one track for both processes, showing the first one's command line and stamped before the second existed
- A child that leaves the process tree and comes back draws each of its collections once. gcmon re-reads the ring it returns holding, and the records it had already drawn used to be drawn a second time
- A process's row ends where gcmon last read it. A record read after gcmon let go of a pid used to stretch that process's span past the last poll that saw it
- A PID the operating system handed out three or more times draws a row per process. The third process on a PID used to land on the second one's row and the fifth on the fourth's, leaving one row with two `Lifetime` bars, a name from the later process and a start stamp from the earlier
- The process tracks sort by when gcmon first observed each process, and no two of them share a position. A trace holding more than one process could put two rows at the same rank and leave their order to the UI. The order was by first event timestamp before, so a process gcmon read no collections from had no place in it

### Internal

- `gcmon.TraceExporter` is gone from the public surface
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
