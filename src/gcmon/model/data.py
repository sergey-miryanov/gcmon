import msgspec

from .protocol import TMapping


class GCStatsInfo(msgspec.Struct):
    gen: int
    iid: int
    ts_start: int
    ts_stop: int
    heap_size: int
    collections: int
    collected: int
    uncollectable: int
    candidates: int
    duration: float
    increment_size: int | None = None
    alive_size: int | None = None
    ts_mark_alive_start: int | None = None
    ts_mark_alive_stop: int | None = None
    ts_fill_increment_start: int | None = None
    ts_fill_increment_stop: int | None = None
    ts_deduce_unreachable_start: int | None = None
    ts_deduce_unreachable_stop: int | None = None
    ts_handle_weakref_callbacks_start: int | None = None
    ts_handle_weakref_callbacks_stop: int | None = None
    ts_finalize_garbage_stop: int | None = None
    finalized_garbage_count: int | None = None
    ts_handle_resurrected_stop: int | None = None
    ts_clear_weakrefs_stop: int | None = None
    clear_weakrefs_count: int | None = None
    ts_delete_garbage_start: int | None = None
    ts_delete_garbage_stop: int | None = None
    deleted_garbage_count: int | None = None
    old_work: int | None = None
    auto_collect: int | None = None
    aging_threshold: int | None = None
    aging_spaces: int | None = None
    aging_next: int | None = None
    survivor_count: int | None = None
    heap_size_stop: int | None = None


class InstantMsg(msgspec.Struct):
    type: str
    name: str
    ts: int


class GenLoss(msgspec.Struct):
    """What one generation did over one poll interval, seen and unseen.

    ``lost_from`` is the counter of the first record gcmon missed, and the
    far end follows from ``lost_count``.
    """

    gen: int
    observed_count: int
    lost_count: int = 0
    lost_pause_ns: int = 0
    lost_from: int = 0

    @property
    def no_loss(self) -> bool:
        return self.lost_count == 0


class LossMsg(msgspec.Struct):
    """One poll interval on one interpreter, holding the GC runs whose records
    gcmon never read.

    ``ts_start`` and ``ts_stop`` are two consecutive polls, and every
    collection the record names ran between them. ``gens`` carries one
    :class:`GenLoss` per generation that collected or lost anything; ADR-0015
    says why the counts ride there rather than on a span each.

    ``gens`` is also what ``is_loss`` looks for. This record carries neither
    ``collections`` nor ``type``, so no other guard claims it.
    """

    iid: int
    ts_start: int
    ts_stop: int
    gens: list[GenLoss]


def from_mapping(data: TMapping) -> GCStatsInfo | InstantMsg | LossMsg:
    # A capture is GC records by orders of magnitude and the three shapes are
    # mutually exclusive, so `collections` answers first and the rest of the
    # line never reads the dict again. A record carrying none of the three
    # still falls through to the GC branch, which is where an old-format loss
    # record has to land to be refused.
    if "collections" in data:
        return msgspec.convert(data, GCStatsInfo)
    if "gens" in data:
        return msgspec.convert(data, LossMsg)
    if data.get("type") == "i":
        return msgspec.convert(data, InstantMsg)
    return msgspec.convert(data, GCStatsInfo)


def instant_msg(name: str, ts: int) -> InstantMsg:
    return InstantMsg("i", name, ts)
