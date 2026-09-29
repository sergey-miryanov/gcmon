"""The names gcmon draws, owned here because two subsystems render them.

A collection phase is spelled the same way on a Perfetto slice and in a
`--stats` row, and so is a loss interval (`docs/formats.md`,
`docs/statistics.md`). `exporters` and `stats` may not import each other
(ADR-0026), so the words live in the base they share rather than once per
package.

The track names stay in `exporters`. `GC Pauses` is a row and `GC Pause(0)`
is a slice on it: two names, not one name in two spellings.
"""

from collections.abc import Callable, Mapping, Sequence
from typing import Final, NamedTuple

__all__ = [
    "CANDIDATES",
    "CLEAR_WEAKREFS",
    "CMDLINE",
    "COLLECTED",
    "COLLECTIONS",
    "DEDUCE_UNREACHABLE",
    "DELETE_GARBAGE",
    "DURATION",
    "FILL_INCREMENT",
    "FINALIZE_GARBAGE",
    "GC_LOSS_CATEGORY",
    "GC_LOSS_NAME",
    "GC_PAUSE_NAME",
    "GC_PHASES",
    "GEN",
    "GENERATION",
    "GENERATIONS",
    "GENS",
    "HANDLE_RESURRECTED",
    "HANDLE_WEAKREFS",
    "HEAP_SIZE",
    "IID",
    "LOST_COUNT",
    "LOST_FROM",
    "LOST_PAUSE",
    "LOST_PAUSE_NS",
    "MARK_ALIVE",
    "NAME",
    "OBSERVED_COUNT",
    "PAUSE",
    "PID",
    "PID_EPOCH",
    "RSS",
    "SAMPLED_COUNT",
    "TS",
    "TS_START",
    "TS_STOP",
    "TYPE",
    "UNCOLLECTABLE",
    "PerGeneration",
    "PhaseName",
    "counter_display_name",
    "gc_loss_slice_name",
]


# The generations CPython collects. Every name that carries one is spelled
# for all three at import, which is what lets a conversion look a name up
# instead of formatting it once per slice it emits.
GENERATIONS: Final = (0, 1, 2)


class PerGeneration[T](dict[int, T]):
    """One name, rendered for every generation, as a lookup.

    Filled at import for the generations above, which is what a conversion
    indexes. A record naming a generation outside them renders on demand
    rather than raising, and is not kept: nothing a collector emits lands
    there, so caching it would let a malformed capture grow the table.
    """

    def __init__(self, render: Callable[[int], T]) -> None:
        super().__init__((gen, render(gen)) for gen in GENERATIONS)
        self._render = render

    def __missing__(self, gen: int) -> T:
        return self._render(gen)


class PhaseName(NamedTuple):
    """What the surfaces that draw a phase of a collection name it.

    `label` is what a reader sees, on a slice and in a `--stats` row alike.
    `category` is the Perfetto category the slice carries; `stats` has no
    use for it and ignores it.

    `names` holds the slice name and category spelled per generation,
    rendered once by `_phase_name` below, as one pair so that a slice costs one
    lookup. A conversion emits up to nine slices per record, so it reads
    them here rather than formatting them again.
    """

    label: str
    category: str
    names: Mapping[int, tuple[str, str]]


def _phase_name(label: str, category: str) -> PhaseName:
    """A phase's name, with its per-generation names rendered."""

    def names(gen: int) -> tuple[str, str]:
        return f"{label}({gen})", f"{category}(gen={gen})"

    return PhaseName(label, category, PerGeneration(names))


PAUSE: Final = _phase_name("GC Pause", "gc.pause")
MARK_ALIVE: Final = _phase_name("GC Mark Alive", "gc.mark.alive")
FILL_INCREMENT: Final = _phase_name("GC Fill Increment", "gc.increment")
DEDUCE_UNREACHABLE: Final = _phase_name("GC Deduce Unreachable", "gc.deduce")
HANDLE_WEAKREFS: Final = _phase_name("GC Handle Weakrefs Callbacks", "gc.weakrefs")
FINALIZE_GARBAGE: Final = _phase_name("GC Finalize Garbage", "gc.finalize")
HANDLE_RESURRECTED: Final = _phase_name("GC Handle Resurrected", "gc.resurrect")
CLEAR_WEAKREFS: Final = _phase_name("GC Clear Weakrefs", "gc.clear_weakrefs")
DELETE_GARBAGE: Final = _phase_name("GC Delete Garbage", "gc.delete")

# The pause first, then its sub-phases in the order the collector runs them.
GC_PHASES: Final = (
    PAUSE,
    MARK_ALIVE,
    FILL_INCREMENT,
    DEDUCE_UNREACHABLE,
    HANDLE_WEAKREFS,
    FINALIZE_GARBAGE,
    HANDLE_RESURRECTED,
    CLEAR_WEAKREFS,
    DELETE_GARBAGE,
)

GC_PAUSE_NAME: Final = PAUSE.label

# What a record carries, spelled once. The same word is the attribute on a
# record, the field in a JSONL line, and the metric on a counter track or a
# slice arg, so naming it here is what keeps the three from drifting.
COLLECTED: Final = "collected"
UNCOLLECTABLE: Final = "uncollectable"
CANDIDATES: Final = "candidates"
DURATION: Final = "duration"
HEAP_SIZE: Final = "heap_size"
RSS: Final = "rss"


def counter_display_name(gen: int, metric: str) -> str:
    """What a per-generation counter track is called.

    The generation is in the name because the tracks sit side by side
    under one group and the metric alone would repeat (ADR-0027).
    """
    return f"G{gen} {metric}"


# The rest of what a JSONL line carries (docs/formats.md). `gcmon combine`
# reads back what the monitor wrote, so the two halves have to agree on
# every one of these.
PID: Final = "pid"
GEN: Final = "gen"
IID: Final = "iid"
TS: Final = "ts"
TS_START: Final = "ts_start"
TS_STOP: Final = "ts_stop"
COLLECTIONS: Final = "collections"
GENS: Final = "gens"
TYPE: Final = "type"
NAME: Final = "name"

# The annotations a slice carries, which is where a reader finds the figures
# a counter track cannot hold (docs/formats.md, docs/perfetto-sql.md).
GENERATION: Final = "generation"
CMDLINE: Final = "cmdline"
PID_EPOCH: Final = "pid_epoch"
SAMPLED_COUNT: Final = "sampled_count"
OBSERVED_COUNT: Final = "observed_count"
LOST_COUNT: Final = "lost_count"
LOST_FROM: Final = "lost_from"
LOST_PAUSE_NS: Final = "lost_pause_ns"
LOST_PAUSE: Final = "lost_pause"

GC_LOSS_NAME: Final = "GC Loss"

# The category every loss slice carries, and the one a pause query has to
# exclude: a blind interval is not a pause anything measured (ADR-0015).
GC_LOSS_CATEGORY: Final = "gc.loss"


def gc_loss_slice_name(blind: Sequence[int]) -> str:
    """The slice one blind interval is drawn as.

    Named for the generations that lost records, so each combination hashes
    to a colour of its own. An interval that lost nothing carries the bare
    name (ADR-0015).
    """
    if not blind:
        return GC_LOSS_NAME
    return f"{GC_LOSS_NAME}({','.join(str(gen) for gen in blind)})"
