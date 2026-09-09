# Retired specs

Every number this folder has used that is no longer open work, and what became
of it. A spec missing from [README.md](README.md) resolves here, so look
before calling a reference broken.
[CONVENTIONS.md's Lifecycle section](CONVENTIONS.md#lifecycle) governs these
rows.

## Retired

| Spec | Kind | Effort | Summary |
|------|------|--------|---------|
| 0026 | **Superseded** by 0065 | XS | A live capture named a process `Process 12345` and a combined one `12345`, and no trace carried either: the encoder builds the name itself. 0065 removed the drift by deleting the events rather than sharing a helper |
| 0027 | **Landed** 2026-09-09 (reporting) | XS | The main interpreter's `thread.tid` was its process row's pid, so a query reading interpreter ids special-cased it, and a row pid meeting an interpreter id gave two threads one `tid`. Every thread row carries its iid now, and the only row left under a `tid` equal to the pid is the nameless one the trace processor makes per process. [ADR-0011](../docs/adr/0011-process-lifetime-and-ordering.md) |
| 0028 | **Superseded** by 0055 | XS | `chrome+perfetto` worked only by reading a private attribute off each sub-exporter, with the type checkers told not to look. 0055 deleted the fan-out exporter rather than fixing it |
| 0029 | **Superseded** by 0036 | M | Three byte-identical copies of the buffer-and-flush logic in `JsonlExporter`. 0036 collapses the interface that produced them, and its section 4.4 carries the JSONL-schema argument forward |
| 0031 | **Superseded** by 0055 | XS | The README headed its only trace example "Chrome Trace Output" and captioned it as the Perfetto UI. 0055's documentation pass rewrote it |
| 0034 | **Superseded** by ADR-0015 | S | Loss spans reached back across a collection gcmon watched start. [ADR-0015](../docs/adr/0015-gc-loss-spans-on-their-own-track.md)'s rewrite moved the edge to the poll instant, and its rejected alternatives hold the argument |
| 0038 | **Landed** 2026-08-17 (cleanup) | M | Per-pid state had two owners; had they disagreed, gcmon would have reported a loss window that never happened. One tick is one call on `EventsMonitor` now. [ADR-0017](../docs/adr/0017-monitor-owns-the-pid-lifecycle.md), and [ADR-0011](../docs/adr/0011-process-lifetime-and-ordering.md) for the liveness site it moved |
| 0039 | **Landed** 2026-08-21 (cleanup) | S | The record model and `stats/stats.py` carried three jobs each. The unit conversions are `support/time_units.py` now, the `GC Loss` slice text sits with `trace_converter`, and the 749-line stats module is three. Nothing it moved changed behaviour, and `gcmon.__all__` never moved |
| 0041 | **Landed** 2026-08-21 (cleanup) | L | Nineteen modules sat in one flat namespace with the layering unchecked. The package has seven directories now, one per layer, and `tests/architecture/test_layering.py` fails on an import crossing a layer the wrong way. The deep import paths moved with the modules |
| 0043 | **Landed** 2026-08-18 (reporting) | XS | `gcmon.__version__` had said `0.1.0` since `0.2.0`, five releases behind the distribution. It reads the installed metadata now, `pyproject.toml` is the single source, and `gcmon --version` prints it. [RELEASE.md](../docs/RELEASE.md) carries the versioning policy |
| 0045 | **Landed** 2026-08-18 (ergonomics) | S | `--stats` printed one table, half of it a copy of the other half on a single-interpreter run. The flag takes `total` or `full` now, `GCMON_STATS` the same words, and neither keeps a bare spelling. [ADR-0018](../docs/adr/0018-stats-requires-a-view-and-keeps-no-bare-alias.md); its section 7 became 0047 |
| 0046 | **Landed** 2026-08-19 (performance) | S | Settling a departed pid rescanned every running ring, so a fan-out exiting together cost the tick that noticed it tens of milliseconds. `StreamingStats.retain` groups the departing keys in one traversal now, under [ADR-0016](../docs/adr/0016-the-ring-is-the-statistics-unit.md). Its open question is [0051](0051-key-the-running-rings-by-process.md) |
| 0047 | **Landed** 2026-09-09 (reporting) | XS | The README opened its Quick Start with `gcmon 12345`, a form no release ever accepted. `main` lost the dead branch that would have dispatched it in 0.7.0, and no documented example omits the subcommand now |
| 0048 | **Landed** 2026-08-21 (efficiency) | M | gcmon re-derived where a process keeps its GC state on every poll and threw it away again. It attaches once and reads many times now. [ADR-0020](../docs/adr/0020-attach-to-a-process-once.md) owns the attachment's lifetime, and [0052](0052-a-recycled-pid-can-be-read-through-a-stale-attachment.md) is the window it left open |
| 0049 | **Landed** 2026-08-20 (correctness) | S | `--rate` was the wait after a tick rather than the interval between two, so a wide tree polled slower than asked and nothing said so. Tick starts sit on a fixed grid now. [ADR-0019](../docs/adr/0019-schedule-tick-starts-on-a-fixed-grid.md); the naming half is [0050](0050-name-the-poll-interval-for-what-it-is.md) |
| 0055 | **Landed** 2026-08-22 (cleanup) | L | gcmon wrote two trace formats and defaulted to the weaker one: no command lines, no `Processes` minimap, microsecond timestamps. `--format` takes `perfetto`, `jsonl` and `stdout` now and defaults to the first, and `gcmon combine` reads JSONL only. [ADR-0021](../docs/adr/0021-write-one-trace-format.md) supersedes [ADR-0012](../docs/adr/0012-trace-output-formats.md) |
| 0056 | **Declined** 2026-08-23 (efficiency) | M | Over half a trace's bytes were the same few dozen strings. Interning them halves the writer's output but takes 8-16% off the file: [ADR-0022](../docs/adr/0022-compress-each-batch-of-packets.md) collects the repetition per batch. Declined, and re-measured under zstd so 0058 does not reopen it |
| 0057 | **Landed** 2026-08-22 (efficiency) | S | Every trace gcmon wrote was several times larger than it had to be, in a format Perfetto already reads compressed. Each batch is deflated into one `TracePacket.compressed_packets` field now, with no flag to remember. [ADR-0022](../docs/adr/0022-compress-each-batch-of-packets.md) |
| 0058 | **Landed** 2026-08-25 (efficiency) | S | Each batch went out deflated, larger than the same events compress to. It is one `TracePacket.zstd_compressed_packets` at level 3 now, costing readers older than Perfetto v58, and an interpreter built without `compression.zstd` writes the deflated field instead. It amends [ADR-0022](../docs/adr/0022-compress-each-batch-of-packets.md) |
| 0059 | **Landed** 2026-08-31 (enhancement) | M | A reused pid drew one span over a stretch in which that process did not exist, under its predecessor's command line. The monitor assigns the epoch once now, and a record is filed under a `Process`, not a pid. [ADR-0025](../docs/adr/0025-create-every-process-in-one-place.md) |
| 0064 | **Landed** 2026-08-25 (enhancement) | M | The pyperf hook spawned a monitor per measurement phase, six hundred over a suite. It marks each benchmark's region in the trace a running monitor is writing now, refuses to run where none is listening, and holds no statistics. [ADR-0023](../docs/adr/0023-the-pyperf-hook-annotates-and-does-not-drive.md) |
| 0065 | **Landed** 2026-08-26 (cleanup) | M | `TraceEvent` kept Chrome's shape: a `ph` discriminator and a `(pid, tid)` pair no interpreter claimed. An event names its `Track` now. A counter track is `Thread {iid} heap_size`, and a capture has no `tid`. [ADR-0024](../docs/adr/0024-an-event-names-the-track-it-is-drawn-on.md) supersedes [ADR-0004](../docs/adr/0004-toplevel-shared-counters.md) and [ADR-0006](../docs/adr/0006-begin-end-slice-pairs.md) |
| 0066 | **Landed** 2026-09-01 (enhancement) | M | A pid held twice drew one process track, interleaving two processes' pauses under the first one's command line. Each draws its own rows now, `Process 12345` and `Process 12345#2`. [ADR-0011](../docs/adr/0011-process-lifetime-and-ordering.md) reverses its own "the process track is not split" |
| 0067 | **Landed** 2026-09-02 (enhancement) | M | A process's row carried one point and an empty Args panel, and how long gcmon watched it sat on a shared track, drawn clipped. Each row draws a `Lifetime` bar over the observed interval now, saying what gcmon read and missed. [ADR-0011](../docs/adr/0011-process-lifetime-and-ordering.md) |
| 0068 | **Landed** 2026-09-09 (cleanup) | M | `cli` could import every layer, so an offline command could reach into the monitoring one and the layer test would pass. The tree is two subsystems over a shared base now, and the walk fails on the crossing. [ADR-0026](../docs/adr/0026-two-subsystems-over-a-shared-base.md) |

## Gaps

A number in neither table: from 0033 on every number became a file, so a gap
there is a lost row and git has the text. Below that, **0022**, **0023** and
**0032** never became files and nothing records what they were. 0018, 0019 and
0021 sit in [Provenance](#provenance).

**Two numbers got used twice before anyone wrote the never-reuse rule down**,
both reclaimed by the batch in `4731e50` from specs that had landed days
earlier. A reference from before 2026-08-15 resolves against the first column:

| Number | First held by | Reused for |
|---|---|---|
| 0035 | `end-of-run-summary-says-what-the-capture-is-worth`, deleted in `8431858` | `derive-every-gc-sub-phase-from-one-table` |
| 0036 | `statistics-report-the-ring-not-the-process`, deleted in `7f87d32` | `one-exporter-method-per-record-kind` |

They stay. Renumbering nine files to repair two would break the other half of
the same rule, and nothing outside this folder cites either number.

**Findings a review raised that never became specs**, because an open spec
already covered them. One row per review, so the next one appends a line
rather than editing this text:

| Review | Findings already covered |
|---|---|
| `src/gcmon` structure, 2026-08-15 | 0028, 0029 (since retired), [0030](0030-exporter-hygiene-batch.md) section 4.2 |

## Provenance

This folder takes its conventions from the sibling `gcscope` repo. Before
2026-08-05 it held six files in three header formats, sat behind a
`.gitignore` exclusion, and carried a template from an unrelated web project
(JWT examples, a "Definition of Done" checklist, an "AI Implementation Plan"
section telling an agent to execute step 1 and await confirmation). Git tracks
it now, so the retire-the-file rule has somewhere to delete to.

On 2026-08-05 all six went back against the code, and three retired. Their
outcomes predate the keep-the-row rule and run too long for a table cell:

| Old spec | Outcome |
|---|---|
| 18: post-v0.2.0 review fixes (15 REQs) | Split. REQ-1 is **obsolete**: `EventsMonitor` has no `_last_ts` at all now, working from per-pid `_cursors` keyed on the `collections` counter ([ADR-0015](../docs/adr/0015-gc-loss-spans-on-their-own-track.md)). REQ-2 landed as `TestMetaDedupRaceClosed`. REQ-13 lost on the record: graceful degradation without `psutil` is a documented, tested property. The remaining twelve became 0025–0030. |
| 19: README update for v0.2.0 | Landed but for one item. The `combine`, `ControlClient`, Perfetto-SQL and de-duplicated "How It Works" sections are all in the README; the `## Optional Dependencies` heading is moot, since `## Installation` covers both extras with links and the graceful-degradation note. The remainder became 0031. |
| 21: monitor-reported process liveness | **Landed** 2026-08-02. `EventsMonitor` calls `add_process_liveness` once per tick, `PerfettoExporter` overrides it, and the provisional counter carve-out is gone. [ADR-0011](../docs/adr/0011-process-lifetime-and-ordering.md) records it; 0038 later moved the call from `MonitorLoop` into the monitor. |

20 and 24 survived and keep their numbers, both rewritten into the templates.
20 also settled a decision it had left open: its section 4 says why the
monitoring process's own `gc.get_threshold()` cannot serve as a source for the
target's thresholds.

One correction, worth stating because the old spec asserted the opposite:
REQ-4 proposed moving `JsonlExporter` onto `BufferedTraceExporter` with a
`JsonlEventEncoder`. That would have rewritten the JSONL schema and broken the
combine reader in the same commit, since the schema is public, documented
per-field in [docs/formats.md](../docs/formats.md#jsonl-output), and
`jsonl_io.read_jsonl` reads it back to drive `gcmon combine`. 0029 shared the
buffering instead; 0036 carries that constraint now.
