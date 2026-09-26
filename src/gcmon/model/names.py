"""The names gcmon draws, owned here because two subsystems render them.

A collection phase is spelled the same way on a Perfetto slice and in a
`--stats` row, and so is a loss interval (`docs/formats.md`,
`docs/statistics.md`). `exporters` and `stats` may not import each other
(ADR-0026), so the words live in the base they share rather than once per
package.

`GC_PHASES` is two of the six hand-written sub-phase lists spec 0035 sets
out to collapse; the rest of that work is the JSONL fields, the normalizer
and the predicates, which this table does not reach.

The track names stay in `exporters`. `GC Pauses` is a row and `GC Pause(0)`
is a slice on it: two names, not one name in two spellings.
"""

from collections.abc import Callable, Mapping, Sequence
from typing import Final, NamedTuple

__all__ = [
    "ALIVE_SIZE",
    "CANDIDATES",
    "CLEAR_WEAKREFS",
    "CLEAR_WEAKREFS_COUNT",
    "CMDLINE",
    "COLLECTED",
    "COLLECTIONS",
    "DEDUCE_UNREACHABLE",
    "DELETED_GARBAGE_COUNT",
    "DELETE_GARBAGE",
    "DURATION",
    "FILL_INCREMENT",
    "FINALIZED_GARBAGE_COUNT",
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
    "INCREMENT_SIZE",
    "JSONL_FIELDS",
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
    "SLICE_ARGS",
    "TS",
    "TS_CLEAR_WEAKREFS_STOP",
    "TS_DEDUCE_UNREACHABLE_START",
    "TS_DEDUCE_UNREACHABLE_STOP",
    "TS_DELETE_GARBAGE_START",
    "TS_DELETE_GARBAGE_STOP",
    "TS_FILL_INCREMENT_START",
    "TS_FILL_INCREMENT_STOP",
    "TS_FINALIZE_GARBAGE_STOP",
    "TS_HANDLE_RESURRECTED_STOP",
    "TS_HANDLE_WEAKREF_CALLBACKS_START",
    "TS_HANDLE_WEAKREF_CALLBACKS_STOP",
    "TS_MARK_ALIVE_START",
    "TS_MARK_ALIVE_STOP",
    "TS_START",
    "TS_STOP",
    "TYPE",
    "UNCOLLECTABLE",
    "PerGeneration",
    "Phase",
    "counter_display_name",
    "gc_loss_slice_name",
    "gc_pause_slice_name",
    "phase_category",
    "phase_slice_name",
]


# The generations CPython collects. Every name that carries one is spelled
# for all three at import, which is what lets a conversion look a name up
# instead of formatting it once per slice it emits.
GENERATIONS: Final = (0, 1, 2)


class PerGeneration(dict[int, str]):
    """One name, rendered for every generation, as a lookup.

    Filled at import for the generations above, which is what a conversion
    indexes. A record naming a generation outside them renders on demand
    rather than raising, and is not kept: nothing a collector emits lands
    there, so caching it would let a malformed capture grow the table.
    """

    def __init__(self, render: Callable[[int], str]) -> None:
        super().__init__((gen, render(gen)) for gen in GENERATIONS)
        self._render = render

    def __missing__(self, gen: int) -> str:
        return self._render(gen)


class Phase(NamedTuple):
    """One phase of a collection, as the surfaces that draw it name it.

    `label` is what a reader sees, on a slice and in a `--stats` row alike.
    `category` is the Perfetto category the slice carries; `stats` has no
    use for it and ignores it.

    `slice_names` and `categories` are those two spelled per generation,
    rendered once by `_phase` below. A conversion emits up to nine slices
    per record, so it reads them here rather than formatting them again.
    """

    label: str
    category: str
    slice_names: Mapping[int, str]
    categories: Mapping[int, str]


def _phase(label: str, category: str) -> Phase:
    """One row of the table, with its per-generation names rendered."""

    def slice_name(gen: int) -> str:
        return f"{label}({gen})"

    def slice_category(gen: int) -> str:
        return f"{category}(gen={gen})"

    return Phase(label, category, PerGeneration(slice_name), PerGeneration(slice_category))


PAUSE: Final = _phase("GC Pause", "gc.pause")
MARK_ALIVE: Final = _phase("GC Mark Alive", "gc.mark.alive")
FILL_INCREMENT: Final = _phase("GC Fill Increment", "gc.increment")
DEDUCE_UNREACHABLE: Final = _phase("GC Deduce Unreachable", "gc.deduce")
HANDLE_WEAKREFS: Final = _phase("GC Handle Weakrefs Callbacks", "gc.weakrefs")
FINALIZE_GARBAGE: Final = _phase("GC Finalize Garbage", "gc.finalize")
HANDLE_RESURRECTED: Final = _phase("GC Handle Resurrected", "gc.resurrect")
CLEAR_WEAKREFS: Final = _phase("GC Clear Weakrefs", "gc.clear_weakrefs")
DELETE_GARBAGE: Final = _phase("GC Delete Garbage", "gc.delete")

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
INCREMENT_SIZE: Final = "increment_size"
ALIVE_SIZE: Final = "alive_size"
FINALIZED_GARBAGE_COUNT: Final = "finalized_garbage_count"
DELETED_GARBAGE_COUNT: Final = "deleted_garbage_count"
CLEAR_WEAKREFS_COUNT: Final = "clear_weakrefs_count"


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

# What a JSONL line carries, and what a slice carries, each as a page
# documents it. `tests/exporters/test_track_names_are_documented.py` reads
# these, so adding a name here fails until `docs/formats.md` describes it.
JSONL_FIELDS: Final = (
    PID,
    GEN,
    IID,
    TS_START,
    TS_STOP,
    COLLECTIONS,
    LOST_FROM,
    OBSERVED_COUNT,
    LOST_COUNT,
    LOST_PAUSE_NS,
)

SLICE_ARGS: Final = (
    CMDLINE,
    PID_EPOCH,
    SAMPLED_COUNT,
    LOST_PAUSE,
)

# `generation` is absent: it repeats on the slice the name already carries
# the generation of, so no page describes it as a figure worth reading.

# When each sub-phase started and stopped, as an instrumented CPython
# reports it. A name here is a field on the record and nothing else reads
# it, so the phase it belongs to is the comment beside it.
TS_MARK_ALIVE_START: Final = "ts_mark_alive_start"
TS_MARK_ALIVE_STOP: Final = "ts_mark_alive_stop"
TS_FILL_INCREMENT_START: Final = "ts_fill_increment_start"
TS_FILL_INCREMENT_STOP: Final = "ts_fill_increment_stop"
TS_DEDUCE_UNREACHABLE_START: Final = "ts_deduce_unreachable_start"
TS_DEDUCE_UNREACHABLE_STOP: Final = "ts_deduce_unreachable_stop"
TS_HANDLE_WEAKREF_CALLBACKS_START: Final = "ts_handle_weakref_callbacks_start"
TS_HANDLE_WEAKREF_CALLBACKS_STOP: Final = "ts_handle_weakref_callbacks_stop"
TS_FINALIZE_GARBAGE_STOP: Final = "ts_finalize_garbage_stop"
TS_HANDLE_RESURRECTED_STOP: Final = "ts_handle_resurrected_stop"
TS_CLEAR_WEAKREFS_STOP: Final = "ts_clear_weakrefs_stop"
TS_DELETE_GARBAGE_START: Final = "ts_delete_garbage_start"
TS_DELETE_GARBAGE_STOP: Final = "ts_delete_garbage_stop"

GC_LOSS_NAME: Final = "GC Loss"

# The category every loss slice carries, and the one a pause query has to
# exclude: a blind interval is not a pause anything measured (ADR-0015).
GC_LOSS_CATEGORY: Final = "gc.loss"


def phase_slice_name(phase: Phase, gen: int) -> str:
    """The slice one generation's *phase* is drawn as.

    A reading of the table `_phase` rendered. The conversion loop indexes
    `phase.slice_names` directly, since a call per slice is what this
    spelling used to cost it; everything colder reads it through here.
    """
    return phase.slice_names[gen]


def phase_category(phase: Phase, gen: int) -> str:
    """The category that slice carries.

    Filtering on the prefix reaches every generation, on the exact string
    reaches one.
    """
    return phase.categories[gen]


def gc_pause_slice_name(gen: int) -> str:
    """The slice one collection is drawn as, named for its generation."""
    return phase_slice_name(PAUSE, gen)


def gc_loss_slice_name(blind: Sequence[int]) -> str:
    """The slice one blind interval is drawn as.

    Named for the generations that lost records, so each combination hashes
    to a colour of its own. An interval that lost nothing carries the bare
    name (ADR-0015).
    """
    if not blind:
        return GC_LOSS_NAME
    return f"{GC_LOSS_NAME}({','.join(str(gen) for gen in blind)})"
