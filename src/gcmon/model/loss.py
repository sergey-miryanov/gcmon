"""Reconstructing the GC records a poll could not read.

CPython writes one record per finished GC run, and a target whose collector
runs faster than gcmon polls overwrites records before any poll reads them. Two
cumulative fields make the loss measurable: ``collections`` counts how many GC
runs finished, and ``duration`` gives the total pause time of those runs.

``EventsMonitor`` owns one ``RingAccumulator`` per ``(pid, iid, gen)``.
"""

from collections.abc import Iterable, Sequence

import msgspec

from ..support.time_units import secs_to_ns
from .data import GenLoss
from .protocol import TGCStatsInfo

__all__ = [
    "RingAccumulator",
    "RingKey",
]

# (iid, gen). One CPython ring buffer, with its own `collections` counter.
type RingKey = tuple[int, int]


class RingAccumulator(msgspec.Struct):
    """What one ring did, against what gcmon saw of it.

    ``last_collections`` holds the newest counter this ring has returned.
    :meth:`unseen` drops anything at or below it as already handled.
    """

    first_collections: int = 0
    first_pause_ns: int = 0
    first_duration: float = 0.0
    last_collections: int = 0
    last_duration: float = 0.0
    sampled_count: int = 0
    sampled_pause_ns: int = 0

    def unseen(self, events: Iterable[TGCStatsInfo]) -> list[TGCStatsInfo]:
        """The records in *events* this ring has not seen before, keeping the
        order they came in.

        Two slots can report one GC run and no marker splits them, so the
        first of the pair is dropped, see ADR-0015.
        """
        fresh = {event.collections: event for event in events if event.collections > self.last_collections}
        return list(fresh.values())

    def ingest(self, events: Sequence[TGCStatsInfo]) -> GenLoss:
        """Fold the records one poll returned for this ring.

        *events* is what :meth:`unseen` returned, ordered by ``collections``
        and not empty. A ring holds consecutive records, so only the first of
        them can sit across a gap and only the last settles the cursor.
        CPython's sequence check keeps a read contiguous, see ADR-0015.

        Returns the generation's entry for the poll, ready for a ``LossMsg``
        as it stands.
        """
        assert len(events) > 0
        # The first record on a ring opens no gap: what ran before it sits
        # outside what `exact_count` counts.
        seeding = self.sampled_count == 0
        if seeding:
            self.first_collections = events[0].collections
            self.first_pause_ns = events[0].ts_stop - events[0].ts_start
            self.first_duration = events[0].duration
            entry = GenLoss(gen=events[0].gen, observed_count=len(events))
        else:
            entry = self._gen_loss(events[0], len(events))

        for event in events:
            self.sampled_pause_ns += event.ts_stop - event.ts_start

        last = events[-1]
        self.sampled_count += len(events)
        self.last_collections = last.collections
        self.last_duration = last.duration

        return entry

    def _gen_loss(self, event: TGCStatsInfo, observed_count: int) -> GenLoss:
        """This ring's entry for one poll: *observed_count* records read, and
        the records missing ahead of *event*, if any.

        Touches no running total; :meth:`ingest` owns those.
        """
        # `last_collections` is the newest counter this ring returned, so the
        # counters between it and *event* never reached gcmon.
        lost_from = self.last_collections + 1
        lost = event.collections - lost_from
        if lost <= 0:
            return GenLoss(gen=event.gen, observed_count=observed_count)

        # `duration` is cumulative, so this delta covers the lost records and
        # *event* with them. Taking *event*'s own pause back out leaves the
        # lost ones.
        spanned_ns = secs_to_ns(event.duration - self.last_duration)
        return GenLoss(
            gen=event.gen,
            observed_count=observed_count,
            lost_count=lost,
            # Floored: cumulative seconds as a float against ns timestamps can
            # land a hair below zero, and a negative would drag
            # `exact_pause_ns` under the sum gcmon measured.
            lost_pause_ns=max(0, spanned_ns - (event.ts_stop - event.ts_start)),
            lost_from=lost_from,
        )

    @property
    def exact_count(self) -> int:
        """GC runs between the first and last record gcmon observed, counting
        both ends.

        Runs before the first fall outside it, since gcmon cannot tell "ran
        before we attached" from "lost".
        """
        if self.sampled_count == 0:
            return 0
        return self.last_collections - self.first_collections + 1

    @property
    def exact_pause_ns(self) -> int:
        """Pause time over the runs ``exact_count`` counts, from the target's
        own ``duration``.

        ``first_duration`` is cumulative through the first observed record, so
        the delta starts after it. Adding that record's pause back makes this
        cover the runs ``exact_count`` counts.
        """
        if self.sampled_count == 0:
            return 0
        return secs_to_ns(self.last_duration - self.first_duration) + self.first_pause_ns
