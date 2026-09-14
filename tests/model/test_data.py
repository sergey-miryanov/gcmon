import msgspec
import pytest

from gcmon.model.data import (
    GCStatsInfo,
    GenLoss,
    InstantMsg,
    LossMsg,
    from_mapping,
    instant_msg,
)
from gcmon.model.protocol import TMapping, has_deduce_unreachable, has_incremental, has_mark_alive, to_mapping


class TestGCStatsInfo:
    def test_struct_creation(self, simple_item: GCStatsInfo) -> None:
        assert simple_item.gen == 0
        assert simple_item.iid == 1
        assert simple_item.ts_start == 1_000_000
        assert simple_item.ts_stop == 2_000_000
        assert simple_item.heap_size == 1024
        assert simple_item.collections == 5
        assert simple_item.collected == 50
        assert simple_item.uncollectable == 0
        assert simple_item.candidates == 10
        assert simple_item.duration == 0.005


class TestInstantMsg:
    def test_instant_msg_creation(self, instant_item: InstantMsg) -> None:
        assert instant_item.type == "i"
        assert instant_item.name == "start GC monitor"
        assert instant_item.ts == 5_000_000

    def test_instant_msg_with_explicit_ts(self) -> None:
        msg = instant_msg("test event", 12345)
        assert isinstance(msg, InstantMsg)
        assert msg.type == "i"
        assert msg.name == "test event"
        assert msg.ts == 12345


class TestFromMapping:
    def test_regular_item(self, gc_stats_dict: TMapping) -> None:
        result = from_mapping(gc_stats_dict)
        assert isinstance(result, GCStatsInfo)
        assert not has_incremental(result)
        assert result.gen == 0
        assert result.iid == 1
        assert result.ts_start == 1_000_000
        assert result.ts_stop == 2_000_000
        assert result.heap_size == 1024
        assert result.collections == 5
        assert result.collected == 50
        assert result.uncollectable == 0
        assert result.candidates == 10
        assert result.duration == 0.005

    def test_incremental_item(self, incremental_dict: TMapping) -> None:
        result = from_mapping(incremental_dict)
        assert isinstance(result, GCStatsInfo)
        # Common fields (always present)
        assert result.gen == 1
        assert result.iid == 2
        assert result.ts_start == 3_000_000
        assert result.ts_stop == 4_000_000
        assert result.heap_size == 2048
        assert result.collections == 10
        assert result.collected == 100
        assert result.uncollectable == 1
        assert result.candidates == 20
        assert result.duration == 0.01
        assert result.finalized_garbage_count == 42
        assert result.clear_weakrefs_count == 7
        assert result.deleted_garbage_count == 13
        # Incremental fields
        assert has_incremental(result)
        assert result.increment_size == 500
        assert result.ts_fill_increment_start == 3_001_500
        assert result.ts_fill_increment_stop == 3_002_000
        # Mark-alive fields
        assert has_mark_alive(result)
        assert result.alive_size == 300
        assert result.ts_mark_alive_start == 3_000_500
        assert result.ts_mark_alive_stop == 3_001_000
        # Deduce-unreachable fields
        assert has_deduce_unreachable(result)
        assert result.ts_deduce_unreachable_start == 3_002_500
        assert result.ts_deduce_unreachable_stop == 3_003_000

    def test_from_mapping_returns_instant_msg(self, instant_dict: TMapping) -> None:
        result = from_mapping(instant_dict)
        assert isinstance(result, InstantMsg)
        assert result.type == "i"
        assert result.name == "start GC monitor"
        assert result.ts == 5_000_000

    def test_from_mapping_empty_raises(self) -> None:
        with pytest.raises(msgspec.ValidationError):
            from_mapping({})


class TestLossMsg:
    def test_it_carries_one_entry_per_generation(self) -> None:
        msg = LossMsg(
            iid=0,
            ts_start=1_000,
            ts_stop=2_000,
            gens=[GenLoss(gen=1, observed_count=4, lost_count=76), GenLoss(gen=2, observed_count=1)],
        )

        assert [entry.gen for entry in msg.gens] == [1, 2]
        assert msg.gens[0].lost_pause_ns == 0

    def test_neither_a_gc_record_nor_an_instant(self) -> None:
        """What keeps the existing branches in ``to_mapping``,
        ``convert_to_trace_format`` and the normalizers from claiming it."""
        msg = LossMsg(iid=0, ts_start=1_000, ts_stop=2_000, gens=[])

        assert not hasattr(msg, "collections")
        assert not hasattr(msg, "type")

    def test_from_mapping_returns_loss_msg(self) -> None:
        result = from_mapping(
            {
                "pid": 42,
                "tid": -2,
                "iid": 1,
                "ts_start": 1_000,
                "ts_stop": 2_000,
                "gens": [{"gen": 2, "observed_count": 3, "lost_count": 76}],
            }
        )

        assert isinstance(result, LossMsg)
        assert result.iid == 1
        assert [(entry.gen, entry.lost_count) for entry in result.gens] == [(2, 76)]

    def test_round_trips_through_a_mapping(self) -> None:
        msg = LossMsg(
            iid=1,
            ts_start=1_000,
            ts_stop=2_000,
            gens=[GenLoss(gen=0, observed_count=9, lost_count=76, lost_pause_ns=8_100_000, lost_from=413)],
        )

        assert from_mapping(to_mapping(msg)) == msg

    def test_a_record_naming_no_generation_still_decodes_as_loss(self) -> None:
        """``from_mapping`` discriminates on ``gens`` being present, not
        populated, so a record reporting nothing must still round-trip rather
        than come back as a GC record missing every field."""
        msg = LossMsg(iid=0, ts_start=1_000, ts_stop=2_000, gens=[])

        assert from_mapping(to_mapping(msg)) == msg
