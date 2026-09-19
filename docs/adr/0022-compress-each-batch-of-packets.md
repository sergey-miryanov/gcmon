# ADR-0022: Compress each batch into one `TracePacket.zstd_compressed_packets` field

- **Status:** Accepted
- **Date:** 2026-08-25
- **Modules:** exporters

## Context

A gcmon trace is large, and most of it is repetition: a fixed set of slice
names, categories and annotation keys, written out again for every slice
drawn. An operator attaching to a production process copies all of it off the
host, attaches all of it to an issue, and waits while the UI loads all of it.

Compressing each batch as gcmon writes it takes most of that out. Perfetto's
wire format has a field for compressed packets, and the trace processor and
the UI both read it.

## Decision

**A batch goes out as one compressed `TracePacket`.** It holds the serialized
`TracePacket` entries the encoder already builds, under
`zstd_compressed_packets`, field 133, where the interpreter has
`compression.zstd`, and under `compressed_packets`, field 50, where it does
not.

**The codec resolves once at import, from what the interpreter has.**
`compression.zstd` is an optional part of a CPython build. No flag chooses the
codec, it does not vary per run, and no file records which wrote it. A
fallback capture is the more portable one: it opens on any Perfetto.

**The file keeps its `.pftrace` extension and opens the same way.** Dropping
it on the Perfetto UI still works.

**gcmon writes one form per file.** The format allows a file to hold plain
packets and either compressed form, and a reader accepts the mixture.

**The compression boundary is the flush boundary.** A batch is compressed
whole before any of it reaches the file, and a run killed in that window loses
all of it. A coarser boundary puts more events in the window.

**Level 3 for zstd, 6 for deflate, both written out here, and no flag.** Level
3 is zstd's own default today. An upstream change to that default does not
reach gcmon. Higher levels buy little size for the CPU they spend inside the
flush, and the highest cannot run there.

## Consequences

- The trace an operator copies off a host is a fraction of the size, with
  nothing to set and no pipeline to edit.
- **A zstd trace needs Perfetto v58 or newer.** An older reader does not know
  field 133: it skips the field, loads a trace with no packets and reports no
  error, and only a human looking at the empty timeline notices. That is the
  failure mode [ADR-0001](0001-hand-rolled-perfetto-protobuf-encoder.md)
  exists to keep out, accepted here for the size. gcmon cannot warn: it does
  not know what will open the file. Documentation is the whole mitigation, and
  a fallback capture opens anywhere.
- **A killed run loses at most one batch.** The file opens and yields the
  batches that completed, with their names, categories and args intact. The
  kill window was one packet before this and is one batch now.
- **A truncated trace still says nothing about what it lost.** The `stats`
  table on a short file reports no error row and no data-loss counter, on this
  encoding and on the plain one alike.
- **The trace stops being byte-reproducible across machines.** Compressed
  output depends on the codec build, and the CI legs do not agree on bytes for
  the same input. Two runs on one machine still produce identical files.
- **No test may pin compressed bytes, or assert a size or a ratio.** Sizes
  differ by codec build. A bound loose enough to pass everywhere is too loose
  to catch a level that changed. The guard is structural: the compressed batch
  is there, and it inflates to the packets.
- **The trace stops being greppable.** `strings gcmon.pftrace` says nothing
  now. It was never a documented property.
- **The suite pins its own trace processor.** The `perfetto` package ships
  protos and prebuilt binary from different releases, and its prebuilt does
  not read field 133. Left to the package, the suite would load every zstd
  batch as an empty trace and report no error.
- `perfetto` stays a development dependency, and both codecs come from the
  stdlib: `compression.zstd` and `zlib`.
- **The trace format depends on the interpreter that wrote it.** Two people on
  one gcmon version can produce different files, and neither names the codec
  that wrote it. The reader takes both.
- **Verification has to cover both codecs, on a machine that writes one.** A
  suite exercising only what its own interpreter resolved would leave the
  other branch untested everywhere it ran.

## Alternatives considered

- **Gzip the whole file.** `gzip.open` in place of `open` is a smaller change
  and lands at much the same size. Rejected: gzip refuses a truncated file and
  recovers nothing, and a killed run is when an operator most wants what was
  captured. The bytes survive, but getting them back needs a repair script an
  operator does not have, run on a file that reports itself corrupt.
- **One gzip member per flush.** It reads correctly whole and fails
  identically when truncated. Rejected: it costs the "it is just a gzip file"
  simplicity and buys nothing.
- **Deflate everywhere, which every Perfetto generation reads.** Rejected:
  what argued for it was the empty timeline an older reader draws, never the
  codec, and v58.2 ships a trace processor for field 133 that the suite pins.
  Deflate is the fallback, not the format.
- **Writing both fields, so that an old reader takes 50 and a new one takes
  133.** Rejected: a reader that knows both expands both and draws every slice
  twice. Perfetto's advice to set both codecs is about a service choosing one,
  not about a file carrying two.
- **Keeping deflate behind a flag for old readers.** Rejected: the fallback
  covers a build that cannot write zstd, a reader that cannot read it is a
  different problem, and a flag would put both encodings on the tested write
  path for good.
- **A `--compress` or `--compress-level` flag.** Rejected on the ground
  [ADR-0021](0021-write-one-trace-format.md) refused `--input-format`: there
  is one value anyone would set, and every other value would have to be kept
  working and tested.
- **A minimum size below which a batch is written plain.** A one-packet batch
  comes out larger than it went in. Rejected: the branch costs more in
  untested code than it saves in bytes.
