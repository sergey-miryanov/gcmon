# Trace Analysis with Perfetto SQL

Perfetto's SQL panel opens gcmon's `.pftrace`. PerfettoSQL is SQLite with
extensions.

[Output formats](formats.md#perfetto-output) lists what a capture holds. The
trace processor puts gcmon's slice args under `debug.`: a per-generation loss
count is `debug.gen0.lost_count`.

## Accessing the SQL Interface

1. Open your `.pftrace` file in [Perfetto UI](https://ui.perfetto.dev)
2. Press `Ctrl+Space` (or `Cmd+Space` on Mac) to open the SQL query panel
3. Enter your SQL query and press `Run`

## Understanding the Schema

gcmon traces use the standard Perfetto schema:

- **`process`**: one row per process that drew a row of its own, plus an idle
  `pid = 0` entry the trace processor adds
  - `upid`, the trace processor's own key and the one to group by; `pid`, one
    gcmon writes per process, not the operating system's; `name`
    (`"Process 12345"`); `start_ts`
- **`thread`**: one nameless row per process, which the trace processor builds
  out of the `ProcessDescriptor`. gcmon writes none of its own
  - `utid`, its own key; `upid`, the process it belongs to; `tid`, equal to
    the row's `pid`
- **`slice`**: GC pauses and sub-steps
  - `name` (`"GC Pause(0)"`), `ts` and `dur` in nanoseconds, `arg_set_id`
- **`counter`**: counter samples
  - `track_id`, `ts`, `value`
- **`counter_track`**: one row per counter track
  - `id`, `name` (`"G0 collected"`, `"heap_size"`), `parent_id`
- **`process_track`**: every row gcmon draws, the process's own and the ones
  nested under its `Python Interpreters` group, each with the process's `upid`
  - `id`, `name`, `parent_id`, `upid`, and `source_arg_set_id` for the track's
    own args
- **`args`**: key/value arguments for slices and tracks
  - `arg_set_id`, `string_value` / `int_value`
  - `key` / `flat_key`: bare for a track arg (`description`), prefixed for a
    slice annotation (`debug.cmdline`)

> **Note:** `process.cmdline` always returns `NULL`, since the trace processor
> does not surface `ProcessDescriptor.cmdline`. Query the process track's
> `description` or the `debug.cmdline` annotation, both below.

> **Note:** `process.pid` is gcmon's, not the operating system's: one number
> per process row, counted from 1. A PID handed on has an entry per process.
> The `debug.pid` annotation on the `Processes` span and on the `Lifetime` bar
> carries the operating system's PID, and so does the row's name.

> **Note:** gcmon writes no thread of its own. Every row an interpreter owns
> is a plain custom track under a group named `Interpreter {iid}`. The one row
> in `thread` is the nameless one the trace processor builds per process,
> carrying the row's `pid` as its `tid` and marked by `thread.is_main_thread`;
> it has no name, no `thread_track` and no slices. Count interpreters by
> counting the `Interpreter %` tracks under a process's `Python Interpreters`
> group.

## Example: Replicating the Stats Table

SQL reproduces the [`--stats` table](statistics.md). `gc.loss` is left out: a
loss span's width is an interval gcmon went blind for rather than a pause it
measured, so the two share no distribution
([GC Loss slices](formats.md#gc-loss-slices)).

```sql
-- GC pause statistics
SELECT
name,
    COUNT(dur) AS count,
    ROUND(SUM(dur) / 1e6, 2) as dur_ms,
    ROUND(AVG(dur) /1e6, 4) AS avg_ms,
    -- Calculate P50, P90, and P99 in milliseconds
    ROUND(PERCENTILE(dur, 50) / 1e6, 4) AS P50_dur_ms,
    ROUND(PERCENTILE(dur, 90) / 1e6, 4) AS P90_dur_ms,
    ROUND(PERCENTILE(dur, 95) / 1e6, 4) AS P95_dur_ms,
    ROUND(PERCENTILE(dur, 99) / 1e6, 4) AS P99_dur_ms
FROM slice
WHERE category IS NOT NULL AND category != 'gc.loss'
GROUP BY name
ORDER BY IF(parent_id IS NULL, 0, 1), name
```

## Example: Naming the Interpreter a Counter Belongs To

Every row an interpreter owns hangs under a group named `Interpreter {iid}`,
and those under one `Python Interpreters` group per process. Two hops up
`parent_id` reach the interpreter from a per-generation counter and one from
`heap_size`, and the `Python Interpreters` group carries the process's `upid`:

```sql
-- Every per-generation counter, with the interpreter and process that own it
SELECT
    p.name AS process,
    ig.name AS interpreter,
    ct.name AS counter,
    COUNT(c.id) AS samples
FROM counter_track ct
JOIN track gm ON ct.parent_id = gm.id AND gm.name = 'GC Metrics'
JOIN track ig ON gm.parent_id = ig.id
JOIN process_track lt ON ig.parent_id = lt.id AND lt.name = 'Python Interpreters'
JOIN process p ON lt.upid = p.upid
LEFT JOIN counter c ON c.track_id = ct.id
GROUP BY ct.id
ORDER BY p.name, ig.name, ct.name
```

`heap_size` sits one hop closer, on the group itself:

```sql
-- Each interpreter's heap size
SELECT ig.name AS interpreter, c.ts, c.value
FROM counter c
JOIN counter_track ct ON c.track_id = ct.id AND ct.name = 'heap_size'
JOIN track ig ON ct.parent_id = ig.id
ORDER BY ig.name, c.ts
```

A pause takes the same walk, from a row named `GC Pauses` under every
interpreter's group:

```sql
-- GC pauses with the interpreter that ran them
SELECT ig.name AS interpreter, s.name, s.ts, s.dur
FROM slice s
JOIN process_track pt ON s.track_id = pt.id AND pt.name = 'GC Pauses'
JOIN track ig ON pt.parent_id = ig.id
ORDER BY s.ts
```

A pause slice carries the same number as a `debug.iid` annotation, which
`EXTRACT_ARG(s.arg_set_id, 'debug.iid')` reads without the join. A counter
carries no annotations, and the parent chain is what it has instead.

## Example: Querying RSS Values

Under `--rss`, samples land in the `counter` table on a track named `rss`:

```sql
-- RSS values per process (requires --rss)
SELECT
    p.name,
    (c.ts - p.start_ts) / 1e9 AS sec_from_start,
    ROUND(c.value / 1e6, 2) AS rss_mb
FROM counter c
JOIN counter_track ct ON c.track_id = ct.id
JOIN process_counter_track pt on ct.id = pt.id
JOIN process p ON pt.upid = p.upid
WHERE ct.name like '%rss%'
ORDER BY p.start_ts, c.ts
```

## Example: Querying Process Command Lines

Requires the [`[cmdline]` extra](rss.md#the-cmdline-extra). gcmon writes the
command line to [several places](formats.md#process-command-lines), and the
ones below are what SQL reaches.

The process track's `description` holds the space-joined command line:

```sql
-- Command line per process, from the process track description
SELECT p.name, a.string_value AS cmdline
FROM args a
JOIN process_track pt ON a.arg_set_id = pt.source_arg_set_id
JOIN process p ON p.upid = pt.upid
WHERE a.key = 'description'
ORDER BY p.start_ts
```

A `cmdline` debug annotation carries the same string on each slice of the
`Processes` lifetime track, which pairs it with that process's start and end
times. On a reused PID both are per process, and the two agree:

```sql
-- Command line alongside each process's lifetime
SELECT
    s.name,
    s.ts,
    s.dur,
    EXTRACT_ARG(s.arg_set_id, 'debug.cmdline') AS cmdline,
    EXTRACT_ARG(s.arg_set_id, 'debug.real_end_ts')
        - EXTRACT_ARG(s.arg_set_id, 'debug.real_start_ts') AS observed_dur
FROM slice s
JOIN track t ON s.track_id = t.id
WHERE t.name = 'Processes'
ORDER BY s.ts
```

Both return nothing when the extra is missing or gcmon could not read the
command line. The rest of the trace still queries.

**Do not read `dur` on this track as an observed duration.** Crossing spans
cut each other short, sometimes to a microsecond. `s.dur` is what Perfetto
could draw; `real_start_ts` and `real_end_ts` are what gcmon observed, and
every slice carries them whether it was cut or not.

Every monitored process gets one slice, a process that never collected
included. A process known from liveness alone drew no row of its own and has
no `process` entry at all, so this track is the only place it appears.

**Scope by `upid` or by name.** A reused PID has one entry per process, none
of them under the operating system's PID. To gather every process that held
one, filter on the `debug.pid` annotation or on the name, `Process 12345` and
`Process 12345#2` from the second process on. A `dur = 0` slice is one
observed at a single instant, or cut down to nothing.

A slice and the process it describes carry **the same name**. That is the
pairing: `p.pid` is per process, and the epoch reaches no column of its own.
Start from the span and left-join the process, so one with a span and no entry
keeps its row:

```sql
-- Each process's observed lifetime beside the pauses it recorded
SELECT
    span.name,
    COUNT(gc.id) AS pauses,
    EXTRACT_ARG(span.arg_set_id, 'debug.real_end_ts')
        - EXTRACT_ARG(span.arg_set_id, 'debug.real_start_ts') AS observed_dur
FROM slice span
JOIN track spant ON span.track_id = spant.id AND spant.name = 'Processes'
LEFT JOIN process p ON p.name = span.name
LEFT JOIN process_track pt ON pt.upid = p.upid AND pt.name = 'GC Pauses'
LEFT JOIN slice gc ON gc.track_id = pt.id AND gc.name GLOB 'GC Pause*'
GROUP BY span.id
ORDER BY span.ts
```

Processes still alive when monitoring stops share an end timestamp and nest,
and the trace processor closes at most **512** nested slices. Past that they
return `dur = -1` with no diagnostic, so filter on `s.dur >= 0` if more than
512 processes may have been running at the end. Compare spans only across
traces captured the same way: `gcmon combine` spans cover GC activity alone.

```sql
-- Processes whose drawn duration is shorter than what gcmon observed
SELECT
    s.name,
    s.dur AS drawn_dur,
    EXTRACT_ARG(s.arg_set_id, 'debug.real_end_ts')
        - EXTRACT_ARG(s.arg_set_id, 'debug.real_start_ts') AS observed_dur
FROM slice s
JOIN track t ON s.track_id = t.id
WHERE t.name = 'Processes'
  AND observed_dur > s.dur
ORDER BY observed_dur - s.dur DESC
```

## Tips for Writing Queries

- Timestamps are nanoseconds. Divide by `1e6` for milliseconds, `1e9` for
  seconds.
- `EXTRACT_ARG` reads a slice annotation, under the `debug.` prefix the trace
  processor adds: `EXTRACT_ARG(arg_set_id, 'debug.heap_size')`. Without the
  prefix it returns `NULL`.

## Further Reading

- [Perfetto SQL Getting Started](https://perfetto.dev/docs/analysis/perfetto-sql-getting-started)
- [Perfetto SQL documentation](https://perfetto.dev/docs/analysis/perfetto-sql-syntax)
