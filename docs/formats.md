# Output formats

gcmon writes traces in three formats, selected with `--format`: `perfetto`
(compressed Perfetto binary protobuf), `jsonl` (JSONL to file), and `stdout`
(JSONL to stdout). See the [CLI reference](cli.md) for the flag.

A `.pftrace` needs Perfetto v58 or newer. An older one shows the capture as an
empty timeline rather than refusing to open it: a run that collected nothing
looks the same.

## Perfetto output

<img src="images/chrome-trace-example.png" alt="Perfetto Trace Example" width="800">

*A gcmon capture in the Perfetto UI.*

A trace carries these, on one track per interpreter:

- **`GC Pause(gen)` slices**, one per GC run gcmon read, carrying that run's
  counters as args.
- **Sub-step slices** nested inside a pause: Mark Alive, Fill increment,
  Deduce Unreachable, Handle Weakrefs Callbacks, Finalize Garbage, Handle
  Resurrected, Clear Weakrefs, Delete Garbage.
- **Counter tracks** per generation, `G{gen}`, carrying `collected`,
  `candidates`, `duration` and `uncollectable`, with `Thread {iid} heap_size`
  as a process-level counter beside them, one per interpreter. A build running
  the new incremental collector adds more, one series per interpreter and not
  per generation: `Thread {iid} old_work` beside `heap_size`, and
  `survivor_count`, `aging_threshold`, `aging_spaces`, `aging_next` and
  `new_increment_size` inside the group, each with the same `Thread {iid}`
  prefix. `new_increment_size` plots the `increment_size` field, and only for
  a run whose `next_gen` is 1.
- **Counter Y-axis sharing**: one metric shares an axis across generations, so
  `G0 collected`, `G1 collected` and `G2 collected` line up.
- **`GC Loss` track**: one row per interpreter, `GC Loss {iid}`, under that
  process's own track; see [GC Loss slices](#gc-loss-slices).
- **`rss` counter** per process under `--rss`, in bytes, sampled at
  `--rss-interval` (default 1s).
- **`Processes` track**: a minimap of the session, one slice per monitored
  process. A reused PID gets one slice per process, the second named
  `Process 12345#2`, and every slice carries a `pid_epoch` annotation counting
  from 1. Filter on the track name `Processes` in SQL. **Read a process's span
  from the `real_start_ts` and `real_end_ts` annotations, not from the slice
  width**, which overlapping processes cut short and sometimes to nothing;
  `clipped` says which slices were cut. See [Perfetto SQL](perfetto-sql.md).
- **One process track per process**, `Process 12345` and `Process 12345#2`,
  each carrying that process's own pause rows, `GC Loss` rows, counters, start
  time and command line. The PID on the row is gcmon's, not the operating
  system's; see [The `Lifetime` slice](#the-lifetime-slice).
- **Process ordering**: the tracks sort by when gcmon first observed each
  process, earliest at the top, and a process it read no collections from
  takes a position like any other. A process gcmon reaches only after it has
  placed others sits below them whatever its own start time.
- **Process command lines**: with the [`[cmdline]`
  extra](rss.md#the-cmdline-extra); see
  [Process command lines](#process-command-lines).
- **`Lifetime` slice**: one per process track, over the interval gcmon
  observed that process, carrying what it was running and how much of it gcmon
  read; see [The `Lifetime` slice](#the-lifetime-slice). It reads longer than
  the same process's `Processes` slice wherever that one was cut short.

> **Note:** sub-step slices (Mark Alive, Fill increment, Deduce Unreachable,
> …) need a CPython build carrying the extra GC instrumentation. A standard
> build gives the top-level `GC Pause` slices and the counters.

### The `Lifetime` slice

One per process track, drawn over the interval gcmon observed that process. It
sits on the process's own track rather than on an interpreter row, so a query
over the pause rows never picks it up.

Its args say how complete the capture is for that process:

| Arg | Meaning |
|---|---|
| `cmdline` | What the process was running, where gcmon read one |
| `pid` | The PID the operating system gave the process |
| `pid_epoch` | Which process on this PID, counting from 1 |
| `interpreters` | Interpreters gcmon read a collection from |
| `sampled_count` | GC records gcmon read for this process |
| `lost_count` | GC records overwritten before a poll reached them |
| `lost_pause` | GC pause inside the lost records |
| `lost_pause_ns` | The same figure in nanoseconds |

**`process.pid` in SQL is not the PID the operating system gave the process.**
gcmon numbers the rows itself, one per process. The `pid` annotation on this
slice and on the `Processes` span carries the real PID, and so does the row's
name.

**The loss figures are the process's totals, and the `GC Loss` slices carry
the same three names per interval.** A query summing `debug.lost_count` over
every slice in a trace counts each loss twice. Filter on the slice name:
`Lifetime` for the per-process figure, `GC Loss` for the per-interval one.
Both rows resolve as `process_track`, so joining through that separates
neither.

`sampled_count` and `lost_count` do not add up to everything the process
collected. A record that fell out of the ring before gcmon's first poll of
that process is in neither: loss is measured between two polls, and the first
one has nothing to measure against. The `--stats` table's `Cov` column is the
figure that covers a whole run; see [Statistics](statistics.md).

### GC Loss slices

A target whose collector runs faster than gcmon polls loses records; see
[How gcmon reads a process](monitoring.md). Each interval gcmon went blind in
gets one slice on a `GC Loss {iid}` track of its own.

**One span per poll interval**, from one read of the target to the next, so
consecutive spans meet without overlapping and the row reads as a sequence.
Every GC run a span accounts for finished between those two reads, and nothing
narrows that further.

**Read the magnitude from the args, not the width.** One lost 5 ms run can
draw a 130 ms bar.

The name lists the generations that lost records, `GC Loss(0,2)`, so the row
says which went blind before you click anything, and each combination keeps
its own colour.

Each slice carries these totals for the whole interval:

| Arg | Meaning |
|---|---|
| `iid` | Interpreter the records were lost from |
| `observed_count` | Records gcmon read in this interval, across every generation |
| `lost_count` | Records it missed in the same interval |
| `seen` | The share that survived, as `87.0% (47 of 54)`. One interval wide, unlike the `--stats` table's `Cov` |
| `lost_pause` | Pause time the runs behind those records took, as `3s 316ms 458µs 100ns`. The bar above it can be 29 s wide |
| `lost_pause_ns` | The same total in nanoseconds, from the target's own counter. Sum this one in SQL |

Then one group per generation that collected or lost anything, named `gen0`,
`gen1`, `gen2`:

| Arg | Meaning |
|---|---|
| `observed_count` | Records of that generation gcmon read in this interval |
| `lost_count` | Records of it gcmon missed |
| `lost_collections` | Which ones, on that generation's `collections` counter: `413..431` for a range, `11` for a single one, both ends included |
| `lost_pause` / `_ns` | What those cost, as text and as nanoseconds |

A generation that came through whole still gets a group with what it observed,
so the groups add up to the totals above them. In SQL the trace processor
flattens a group by joining the names with a dot, so `gen1`'s count is
`args.debug.gen1.lost_count`. A JSONL capture carries the same numbers under
the same names; see [Loss records](#loss-records).

**The counts are exact**, so a group reading `lost_count = 19` also reads
`413..431`. Between the first and last record gcmon read on a generation's
counter, every run is either drawn as a `GC Pause` slice or inside exactly one
span's range. None twice, none missing.

At default settings the track reads as a near-solid bar, since gcmon is blind
for most of every tick. Lower `--rate` or a calmer workload thins it out. See
[Statistics](statistics.md) for what the loss does to the numbers.

### Process command lines

gcmon writes each command line to **three** places, no one of which serves
both the UI and SQL:

| Where | Form | Visible in the UI | Queryable from SQL |
|---|---|---|---|
| `ProcessDescriptor.cmdline` on the process track | argv, one string per argument | Yes | **No**. The trace processor does not surface this field |
| `description` on the process track | argv joined with single spaces | Yes | Yes, via `args` (key `description`) |
| `cmdline` debug annotation on the `Process {pid}` slice of the `Processes` track | argv joined with single spaces | Yes, in the slice's details | Yes, via `args` (key `debug.cmdline`) |

Each is read once, while the process is running. On a reused PID all three
name the program that process ran.

Queries for the latter two are in
[Trace Analysis with Perfetto SQL](perfetto-sql.md#example-querying-process-command-lines).

They need the [`[cmdline]` extra](rss.md#the-cmdline-extra). Without it, or
where gcmon never polled the process, it writes no command line and says
nothing about it. The trace stays valid.

**A `combine` run writes no command line.** A capture carries none, and the
processes it names are historical.

## JSONL output

With `--format jsonl` (writes to file) or `--format stdout` (writes to
terminal), each line is a JSON object holding one GC record:

```jsonl
{"pid": 12345, "gen": 0, "iid": 1, "ts_start": 1700000000000000, "ts_stop": 1700000001500000, "heap_size": 1048576, "collections": 42, "collected": 120, "uncollectable": 0, "candidates": 300, "duration": 0.0015}
{"pid": 12345, "gen": 1, "iid": 2, "ts_start": 1700000200000000, "ts_stop": 1700000235000000, "heap_size": 2097152, "collections": 3, "collected": 85, "uncollectable": 1, "candidates": 150, "duration": 0.035}
```

| Field | Description | Build |
|-------|-------------|-------|
| `pid` | Process ID of the monitored target | Standard |
| `gen` | GC generation (0, 1, or 2) | Standard |
| `iid` | Interpreter ID (`0` for the main interpreter) | Standard |
| `ts_start`, `ts_stop` | When the run started and stopped (nanoseconds) | Standard |
| `heap_size` | Number of live objects the run recorded | Standard |
| `collections` | How many times this generation has run, cumulative | Standard |
| `collected` | Objects this run collected | Standard |
| `uncollectable` | Objects that could not be collected | Standard |
| `candidates` | Candidate objects for collection | Standard |
| `duration` | Pause duration in seconds (float, as reported by CPython) | Standard |
| `increment_size` | Increment size for incremental GC | Custom build |
| `alive_size` | Objects marked alive (gen > 0) | Custom build |
| `finalized_garbage_count` | Objects this run finalized | Custom build |
| `deleted_garbage_count` | Objects this run deleted | Custom build |
| `clear_weakrefs_count` | Weakrefs this run cleared | Custom build |
| `old_work` | Work the collector carries into the next increment | Custom build |
| `next_gen` | Generation the next collection will run | Custom build |
| `survivor_count` | Objects that survived this run | Custom build |
| `aging_threshold` | Threshold an object ages past before promotion | Custom build |
| `aging_spaces` | Aging spaces the collector keeps | Custom build |
| `aging_next` | Aging space the next run will use | Custom build |

> **Note:** fields marked **Custom build** need the instrumented CPython
> build, as the [sub-step slices](#perfetto-output) do.

### Loss records

A session that missed records writes one line per blind poll interval per
interpreter, alongside the GC records. A loss record carries no `collections`
and no `gen` of its own; the per-generation counts sit in `gens`:

```jsonl
{"pid": 12345, "iid": 0, "ts_start": 1700000001500000, "ts_stop": 1700000098000000, "gens": [{"gen": 0, "observed_count": 11, "lost_from": 413, "lost_count": 9, "lost_pause_ns": 57450000}, {"gen": 1, "observed_count": 3, "lost_from": 27, "lost_count": 1, "lost_pause_ns": 8100000}]}
```

| Field | Description |
|-------|-------------|
| `iid` | Interpreter the records were lost from |
| `ts_start`, `ts_stop` | The interval, from one poll to the next (nanoseconds). Its width is uncertainty; the pause is in `lost_pause_ns` |
| `gens` | One entry per generation that ran or lost anything in the interval |

and in each `gens` entry:

| Field | Description |
|-------|-------------|
| `gen` | The generation |
| `observed_count` | Records of it gcmon read in this interval |
| `lost_from` | First record gcmon missed, on that generation's `collections` counter |
| `lost_count` | Records of it gcmon missed. Zero for a generation that lost nothing |
| `lost_pause_ns` | Pause time the runs behind those records took, in nanoseconds |

An entry and the `gen{N}` group on the slice drawn from it hold the same
numbers under the same names. `observed_count`, `lost_count` and
`lost_pause_ns` carry across unchanged; three things are written differently
on the slice, because a slice is read by eye:

| `gens` entry | `gen{N}` group |
|---|---|
| `gen` | the group's own name, `gen0`, `gen1`, `gen2` |
| `lost_from` with `lost_count` | `lost_collections`, one field, both ends included |
| `lost_pause_ns` | `lost_pause` beside it, the same total as text |

Tell the record types apart by field presence: a GC record has `collections`,
a loss record has `gens`, an instant event has `type`. Line order carries no
meaning.
