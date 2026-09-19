# ADR-0006: Represent durations as Begin/End pairs in both backends

- **Status:** Superseded by
  [ADR-0024](0024-an-event-names-the-track-it-is-drawn-on.md)
- **Date:** 2026-06-14
- **Modules:** exporters, model

> Both halves of the argument are gone: the two backends this reconciled
> became one ([ADR-0021](0021-write-one-trace-format.md)), and the pair
> carrying independent metadata at each end, the thing a complete event could
> not express, was never built, since an end carried only a track. A span is
> one `Slice` carrying both its ends now, and the encoder derives the pair the
> wire format still needs. What survives is one consequence: duration is a
> property of the pair on the wire, not of a record, and nothing downstream
> had to change.
