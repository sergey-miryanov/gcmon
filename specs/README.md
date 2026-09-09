# Specs

One file per unit of *forward-looking* work: identified, understood and
specified, but not yet built. A spec states the problem, the evidence, the
proposed change, and the seam you will test it through. It does not record a
decision.

It complements [`docs/adr/`](../docs/adr/README.md), which records decisions
already taken and is the authority on why the design looks as it does. If a
spec here contradicts an ADR, assume the spec is wrong.

This file holds the open set and the order to take it in. The other two:

- [CONVENTIONS.md](CONVENTIONS.md): how to write a spec, the templates, how to
  retire one.
- [RETIRED.md](RETIRED.md): every number that no longer has a file, and what
  became of it.

## Open specs

| Spec | Kind | Effort | Summary |
|------|------|--------|---------|
| [0020](0020-process-metadata-in-perfetto-traces.md) | Feature (enhancement) | M | A trace does not say which Python ran it or what GC thresholds it used; gcmon logs both to stderr and loses them |
| [0024](0024-cpython-report-remote-readable-gc-stats.md) | Report (upstream) | S | Five findings on `_remote_debugging.get_gc_stats` to file upstream with CPython; no gcmon change |
| [0025](0025-control-server-accept-loop-survives-transient-errors.md) | Bug (**availability**) | XS | One transient accept error and the control server refuses every later connection, saying nothing |
| [0027](0027-thread-descriptor-tid-for-interpreter-zero.md) | Bug (reporting) | XS | The main interpreter's `thread.tid` is the pid, so a SQL query has to special-case interpreter zero to read ids |
| [0030](0030-exporter-hygiene-batch.md) | Feature (cleanup) | S | Three one-file hazards in the exporter package: rank dict, builtin shadow, one undocumented threading contract |
| [0033](0033-loss-counter-track.md) | Feature (enhancement) | S | The loss row shows where gcmon went blind but not how much it missed; a bar losing 1 record looks like one losing 40 |
| [0035](0035-derive-every-gc-sub-phase-from-one-table.md) | Feature (cleanup) | L | gcmon writes CPython's eight optional GC sub-phases out by hand in six places; adding the ninth means six edits, and nothing fails if you miss one |
| [0036](0036-one-exporter-method-per-record-kind.md) | Feature (cleanup) | M | `EventsExporter` has grown one method per record kind, three of them no-ops, and the CLI keeps a hand-maintained list of which formats handle RSS at all |
| [0037](0037-one-format-dispatch-for-live-and-combined-traces.md) | Feature (cleanup) | S | Two call sites decide what a format name means, so a format can be supported live and missing offline |
| [0040](0040-derive-the-monitoring-options-from-one-table.md) | Feature (cleanup) | M | gcmon declares every monitoring option three times, and echoes a rejected configuration to the log as though it had accepted it |
| [0042](0042-name-the-process-session-for-its-role.md) | Feature (cleanup) | S | The monitored-process seam carries the name of a role it does not fill, and its two adapters do not have the same shape |
| [0044](0044-torn-reads-and-reordered-publishes.md) | Bug (correctness) | S | **Blocked on upstream.** A pause slice can read one inter-collection interval too long, and a hole inside one poll's records reaches no loss window; both are races in the target that every filter gcmon has passes |
| [0047](0047-the-no-subcommand-form-has-never-worked.md) | Bug (reporting) | XS | `gcmon 12345`, the form the README opens with, exits 2; the branch in `main` that would dispatch it is unreachable |
| [0050](0050-name-the-poll-interval-for-what-it-is.md) | Feature (ergonomics) | S | `--rate` is a duration in seconds under a name that means a frequency, and gcmon echoes `Rate: 0.1s` back |
| [0051](0051-key-the-running-rings-by-pid.md) | Feature (efficiency) | S | Asking `StreamingStats` about one process walks every process's rings; `low_coverage` does it once per polled pid per tick, and on a healthy run it never stops |
| [0052](0052-a-recycled-pid-can-be-read-through-a-stale-attachment.md) | Bug (correctness) | S | A pid the OS reissues between two ticks is read through the attachment gcmon still holds, so an unrelated process's memory reaches the trace as plausible records; only Linux is exposed |
| [0054](0054-macos-attachment-leaks-a-mach-task-port.md) | Bug (availability) | S | On macOS every attachment costs gcmon a Mach port name that nothing gives back; CPython's cleanup has a Windows arm and a Linux arm and no Apple one |
| [0060](0060-report-an-exact-mean-and-a-geometric-mean.md) | Feature (enhancement) | S | `Avg` is the sampled mean and reads high on any lossy run, while the exact one sits unprinted in `PauseTotals`; and no column summarises a pause distribution as skewed as this one |
| [0061](0061-build-the-statistics-table-from-a-tracefile.md) | Feature (enhancement) | L | The statistics table exists only while gcmon is running; a capture from last week holds every number and offers no way to see them |
| [0062](0062-name-a-workload-from-a-sanitized-command-line.md) | Feature (enhancement) | M | A pyperformance run prints one `Total` folding sixty benchmarks and hundreds of blocks keyed by a pid that means nothing afterwards; the level anyone asks about is missing |
| [0063](0063-compare-two-tracefiles.md) | Feature (enhancement) | L | Nothing answers "did GC get worse between these two runs"; two tables side by side works for one row and fails for sixty |

Every row here has a file. A missing number either retired or never became
one; [RETIRED.md](RETIRED.md) says which.

## Suggested order

| Spec | Why here |
|------|----------|
| 0025 | The only outage, and the fix is one word |
| 0050 | Unblocked: 0049 landed, and taking this next edits the help text and the advisory once |
| 0027 | Needs an answer from trace-processor before anyone can settle it either way |
| 0047 | XS, and the command that fails is the one the README opens with |
| 0052 | Silent, and what it produces is indistinguishable from a real measurement |
| 0030 | |
| 0035 | 0039 landed, and the nine `Metric` classes it replaces are a module named for the table |
| 0037 | |
| 0036 | |
| 0040 | Constrained: after 0050. Rewrites the option declarations 0045 edited |
| 0042 | |
| 0020 | Unblocked: 0067 landed, and the `Lifetime` slice is where both fields go |
| 0051 | Unblocked: 0039 landed, and `StreamingStats` is in the module it will keep |
| 0060 | Smallest of the comparison set and depends on none of it |
| 0061 | Unblocked: 0059 landed, and a trace now says which process held a pid |
| 0062 | |
| 0063 | Constrained: after 0060, 0061 and 0062 |

Rows run in order, top to bottom. "Constrained" means the table below forces
the position. A blank cell means no recorded reason, so that row can move.

**Not in the run**, which is every other row in the index:

- **0024** is the owner's to file, and depends on nothing here.
- **0033** wants a real capture in front of you before anyone can judge it
  worth a fourth row.
- **0044** waits on CPython synchronizing the ring. Its section 4 states the
  one measurement that would put it back in play sooner.
- **0054** was found in CPython's source and not in a run. Nobody should size
  it until the ports have been counted on a Mac.

**The only ordering constraints:**

| First | Then | Why |
|-------|------|-----|
| 0050 | 0040 | 0040 derives the option declarations from one table and would otherwise have to carry the alias 0050 introduces through a rewrite of the structure holding it |
| 0060, 0061, 0062 | 0063 | 0063 builds two of the tables those three produce and diffs them; it computes no statistic of its own |

0042 depends on nothing else here; take it at any time.
