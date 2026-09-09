# ADR-0021: Write one trace format, and read only JSONL back

- **Status:** Accepted
- **Date:** 2026-08-22, amended:
  - 2026-09-02: the list of what the Chrome format lacked follows the
    `Lifetime` slice, see
    [ADR-0010](0010-process-identity-cmdline-and-start-marker.md)

## Context

gcmon wrote two trace formats for one run, and only one of them was worth
opening. The Chrome Trace Event format has no command lines
([ADR-0010](0010-process-identity-cmdline-and-start-marker.md)), no
`Processes` minimap and no per-process ordering
([ADR-0011](0011-process-lifetime-and-ordering.md)), no counter Y-axis sharing
([ADR-0005](0005-counter-y-axis-share-key.md)) and no `Lifetime` slice on a
process row. Its timestamps are microseconds. Every event lost three digits to
an integer division on the way out
([ADR-0009](0009-nanoseconds-canonical-time-unit.md)).

It was also the default. `gcmon monitor 12345` wrote `gcmon.json`, and an
operator found out what that file did not contain when they went looking for a
command line in the UI.

The encoder itself was forty lines. The cost sat around it: a
`chrome+perfetto` fan-out exporter reading a private attribute off each
sub-exporter with the type checkers told not to look, a `combine` matrix with
a cell that had to be rejected, an `RSS_CAPABLE_FORMATS` tuple naming three of
its four entries after Chrome, and about 1,800 lines of test parametrized over
both.

[ADR-0012](0012-trace-output-formats.md) is where the second format and the
conversion matrix were decided. Half of what it settled is still right, and
this record carries that half forward.

## Decision

**`--format` takes `perfetto`, `jsonl` and `stdout`, and defaults to
`perfetto`.** `chrome`, `trace` and `chrome+perfetto` are not spellings
argparse accepts: the run stops at the argument with a message naming the
three that remain. The default output path is `gcmon.pftrace`, and
`--format jsonl` still defaults to `gcmon.jsonl`.

**`GCMON_FORMAT` refuses a word `--format` would refuse.** The variable's
value is handed on as written and refused later, once logging is configured.
argparse takes a string default as given; it does not check one against
`choices`. Without the refusal `GCMON_FORMAT=chrome` became `perfetto` with no
message, and the log read `Format: perfetto` for a run configured as something
else. [ADR-0018](0018-stats-requires-a-view-and-keeps-no-bare-alias.md)
settled this shape for `--stats`.

**`gcmon combine` reads JSONL and nothing else.** It has no `--input-format`,
and `--output-format` takes `jsonl` or `perfetto`, defaulting to `perfetto`.
There is no combination it refuses, because there is one input. Handed a
`.json` from an earlier release it says so by name: the reader checks whether
the first non-blank line opens a JSON array and raises naming the Chrome
format, rather than letting msgspec report a malformed line 1, which reads as
a corrupt capture.

**Carried forward from ADR-0012:**

- Normalization is **per input file** on the Perfetto path and **per pid
  across the whole merge** on the `jsonl` path. The JSONL items keep their
  original `TGCStatsInfo` / `TInstantMsg` structure, and per-pid zeroing
  across the merge is the established behaviour for them. `--normalize`'s help
  names both halves; it named only the second while Chrome was the default
  output.
- **Perfetto is not an input.** Reading one would need a protobuf decoder,
  which sits outside the encoder's remit
  ([ADR-0001](0001-hand-rolled-perfetto-protobuf-encoder.md)).
- The CLI derives no file extension from `--output-format`; it uses the `-o`
  path verbatim. Only the *default* changed.

**`EventEncoder` stays a Protocol with one implementation.** ADR-0008 split
the encoder from the exporter for two reasons, and only one of them was "two
formats": the other is that `combine` drives `ProtobufEventEncoder` with no
exporter, no buffer and no lock. That is still true, so the split stays, and a
new output format is a second `EventEncoder` implementation.

## Consequences

- The file an operator gets by default is the one that carries command lines
  and process spans.
- **Breaking.** `--format chrome`, `--format trace` and
  `--format chrome+perfetto` exit 2. The default output moves from
  `gcmon.json` to `gcmon.pftrace`. A CI job asserting on that name fails on
  the first run after upgrading. `combine` no longer takes `--input-format`.
  `gcmon.TraceExporter` leaves the public surface: an importer gets an
  `ImportError` at the import, not at the first call.
- A `.json` file from an earlier release still opens in the Perfetto UI. You
  can no longer produce another one, or feed that one back through `combine`.
- **A JSONL capture is the only thing that converts.** That was already true
  in practice: ADR-0012 rejected `chrome → jsonl` because the Chrome format
  had lost the `TGCStatsInfo` structure, and Perfetto was never an input.
  JSONL is now what an operator keeps if they want to convert later.
- **A trace with nothing in it is no file at all.** The Chrome encoder wrote
  `[]` for a run that read no records; the Perfetto encoder writes nothing.
  Monitoring a pid that never collects now leaves no output file.
- **`combine` has two rough edges, and they sit on the default path now.**
  Per-file normalization draws two captures of one pid from zero, and their
  slices overlap on that pid's track. The encoder resolves a command line
  against whatever holds the pid on this machine now, which for a reissued pid
  is an unrelated process. Both predate this record and both were reachable
  through `--output-format perfetto` before. ADR-0012 rejected a custom
  cmdline provider for `combine` on the grounds that "historical pids have no
  cmdline to find"; a reissued pid falsifies that, and the fix is one argument
  at the call site.
- `TraceEvent` keeps its Chrome-derived shape. It is ADR-0007's
  format-independent intermediate and the Perfetto converter's input, and
  reshaping it around Perfetto's own vocabulary is a separate change to the
  converter, the track state and the loss-slice builder.
- Two tests run on the trace processor now. The whole-run characterization
  pinned Chrome bytes, because Chrome resolved no cmdline and dropped liveness
  on a base-class no-op; the loss-row round trip read the combined output as
  JSON and resolved BEGIN/END as a stack. Both read the trace through a
  decoder gcmon did not write, and both cost a trace-processor load per test.

## Alternatives considered

- **Keeping the words in the whitelist for one release, to make
  `--format chrome` exit 1 with a message.** Rejected: it buys a better error
  for anyone who scripted the flag at the cost of a format name that exists in
  three files and produces nothing. The breaking-changes entries are the
  notice, and argparse names the three formats that remain.
- **Reducing `--input-format` to one choice rather than removing it.**
  Rejected: a flag with a single value is a question with one answer, and it
  would keep `combine --input-format chrome` a spelling argparse accepts.
- **Converting an existing Chrome file to a Perfetto trace.** Rejected: it
  means keeping the Chrome parser alive for one purpose, and the Perfetto UI
  already opens the file, the reason anyone would convert it.
- **Folding `ProtobufEventEncoder` back into `PerfettoExporter`.** Rejected:
  it would put `combine`'s Perfetto output back inside an exporter's
  lifecycle, which ADR-0008 rejected.
- **Keeping `JsonEventEncoder` in `tests/` as the encoder's oracle.**
  Rejected: it keeps the format alive under a different roof and makes the
  trace processor's JSON reader a test dependency for something nothing ships.
  The oracle now reads the trace back against the `list[TraceEvent]` it was
  built from.

## Implementation

- `src/gcmon/cli/commands/monitoring_options.py` holds `FORMATS`, which the
  parser's `choices` and the `GCMON_FORMAT` refusal both read, and
  `RSS_CAPABLE_FORMATS`.
- `src/gcmon/cli/_env.py` holds the `GCMON_FORMAT` reading and the default
  output path.
- `src/gcmon/cli/analyze/convert_cmd.py` holds `combine`'s arguments.
- `src/gcmon/exporters/combine.py` holds the two output paths and the
  normalization split.
- `src/gcmon/exporters/jsonl_io.py` holds the Chrome-file check.
- Tests: `tests/monitoring/test_monitor_cmd.py` and
  `tests/monitoring/test_monitoring_options.py` cover the refusals;
  `tests/test_convert_cmd.py` covers `combine`'s arguments and what it writes;
  `tests/test_convert_cmd_perfetto.py` carries the encoder's oracle, which
  reads a `.pftrace` through the trace processor and compares it against the
  events it was built from; `tests/monitoring/test_monitored_run_trace.py`
  pins a whole run as decoded `TracePacket` text.
