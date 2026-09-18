from collections.abc import Callable, Mapping
from types import SimpleNamespace

import pytest

from gcmon.control.protocol import START_EVENT
from gcmon.model.data import GCStatsInfo, GenLoss, InstantMsg, LossMsg
from gcmon.model.names import (
    ALIVE_SIZE,
    CANDIDATES,
    CLEAR_WEAKREFS_COUNT,
    COLLECTED,
    COLLECTIONS,
    DELETED_GARBAGE_COUNT,
    DURATION,
    FINALIZED_GARBAGE_COUNT,
    GEN,
    GENS,
    HEAP_SIZE,
    IID,
    INCREMENT_SIZE,
    LOST_COUNT,
    LOST_FROM,
    LOST_PAUSE_NS,
    NAME,
    OBSERVED_COUNT,
    TS,
    TS_CLEAR_WEAKREFS_STOP,
    TS_DEDUCE_UNREACHABLE_START,
    TS_DEDUCE_UNREACHABLE_STOP,
    TS_DELETE_GARBAGE_START,
    TS_DELETE_GARBAGE_STOP,
    TS_FILL_INCREMENT_START,
    TS_FILL_INCREMENT_STOP,
    TS_FINALIZE_GARBAGE_STOP,
    TS_HANDLE_RESURRECTED_STOP,
    TS_HANDLE_WEAKREF_CALLBACKS_START,
    TS_HANDLE_WEAKREF_CALLBACKS_STOP,
    TS_MARK_ALIVE_START,
    TS_MARK_ALIVE_STOP,
    TS_START,
    TS_STOP,
    TYPE,
    UNCOLLECTABLE,
)
from gcmon.model.protocol import (
    has_clear_weakrefs,
    has_deduce_unreachable,
    has_delete_garbage,
    has_finalize_garbage,
    has_handle_resurrected,
    has_handle_weakrefs,
    has_incremental,
    has_mark_alive,
    has_pause_ts,
    is_gc_stats,
    is_instant,
    is_loss,
    to_mapping,
)
from tests.helpers import create_mock_stats_item


@pytest.fixture
def loss_item() -> LossMsg:
    return LossMsg(
        iid=1,
        ts_start=1_000,
        ts_stop=2_000,
        gens=[
            GenLoss(gen=1, observed_count=4, lost_count=5, lost_pause_ns=8_100_000, lost_from=42),
            GenLoss(gen=2, observed_count=1),
        ],
    )


Guard = Callable[[object], bool]
"""What every `has_*` and `is_*` function is, once the narrowing is spent."""


class TestIsGC:
    def test_regular_returns_true(self, simple_item: GCStatsInfo) -> None:
        assert is_gc_stats(simple_item) is True

    def test_incremental_returns_true(self, incremental_item: GCStatsInfo) -> None:
        assert is_gc_stats(incremental_item) is True

    def test_instant_returns_false(self, instant_item: InstantMsg) -> None:
        assert is_gc_stats(instant_item) is False


class TestIsInstant:
    def test_instant_returns_true(self, instant_item: InstantMsg) -> None:
        assert is_instant(instant_item) is True

    def test_gc_stats_returns_false(self, simple_item: GCStatsInfo) -> None:
        assert is_instant(simple_item) is False

    def test_incremental_returns_false(self, incremental_item: GCStatsInfo) -> None:
        assert is_instant(incremental_item) is False


class TestHasGuards:
    """Each guard answers for one field, and nine near-identical pairs said
    so nine times. The table is the statement: a guard, the field it reads,
    and a value that field can hold."""

    SUB_PHASES = (
        (has_incremental, INCREMENT_SIZE, 500),
        (has_mark_alive, ALIVE_SIZE, 300),
        (has_deduce_unreachable, TS_DEDUCE_UNREACHABLE_START, 100),
        (has_handle_weakrefs, TS_HANDLE_WEAKREF_CALLBACKS_START, 100),
        (has_finalize_garbage, TS_FINALIZE_GARBAGE_STOP, 100),
        (has_handle_resurrected, TS_HANDLE_RESURRECTED_STOP, 100),
        (has_clear_weakrefs, TS_CLEAR_WEAKREFS_STOP, 100),
        (has_delete_garbage, TS_DELETE_GARBAGE_START, 100),
    )

    def test_a_pause_record_carries_the_pause_timestamps(self) -> None:
        assert has_pause_ts(create_mock_stats_item())

    def test_something_that_is_not_a_record_does_not(self) -> None:
        assert not has_pause_ts(SimpleNamespace(gen=0))

    def test_a_loss_record_does_not_either(self, loss_item: LossMsg) -> None:
        """It carries `ts_start` and `ts_stop` as well, between two polls
        rather than round a pause."""
        assert not has_pause_ts(loss_item)

    @pytest.mark.parametrize(("guard", "field", "value"), SUB_PHASES, ids=[f[1] for f in SUB_PHASES])
    def test_a_guard_sees_the_field_it_names(self, guard: Guard, field: str, value: int) -> None:
        assert guard(create_mock_stats_item(**{field: value}))

    @pytest.mark.parametrize(("guard", "field", "value"), SUB_PHASES, ids=[f[1] for f in SUB_PHASES])
    def test_a_guard_passes_over_a_record_that_lacks_it(self, guard: Guard, field: str, value: int) -> None:
        """A pause record leaves every sub-phase unset, so no guard may
        claim it. One that does draws a slice off a missing timestamp."""
        assert not guard(create_mock_stats_item())


class TestToMappingPartial:
    def test_fill_increment_only(self) -> None:
        item = create_mock_stats_item(
            increment_size=500,
            ts_fill_increment_start=1_000_500,
            ts_fill_increment_stop=1_001_000,
        )
        result = to_mapping(item)
        assert result[INCREMENT_SIZE] == 500
        assert result[TS_FILL_INCREMENT_START] == 1_000_500
        assert result[TS_FILL_INCREMENT_STOP] == 1_001_000
        assert ALIVE_SIZE not in result
        assert TS_MARK_ALIVE_START not in result
        assert TS_DEDUCE_UNREACHABLE_START not in result
        assert TS_HANDLE_WEAKREF_CALLBACKS_START not in result
        assert TS_FINALIZE_GARBAGE_STOP not in result
        assert TS_HANDLE_RESURRECTED_STOP not in result
        assert TS_CLEAR_WEAKREFS_STOP not in result
        assert TS_DELETE_GARBAGE_START not in result
        assert FINALIZED_GARBAGE_COUNT not in result
        assert DELETED_GARBAGE_COUNT not in result
        assert CLEAR_WEAKREFS_COUNT not in result

    def test_mark_alive_only(self) -> None:
        item = create_mock_stats_item(
            alive_size=300,
            ts_mark_alive_start=1_000_500,
            ts_mark_alive_stop=1_001_000,
        )
        result = to_mapping(item)
        assert result[ALIVE_SIZE] == 300
        assert result[TS_MARK_ALIVE_START] == 1_000_500
        assert result[TS_MARK_ALIVE_STOP] == 1_001_000
        assert INCREMENT_SIZE not in result
        assert TS_FILL_INCREMENT_START not in result
        assert TS_DEDUCE_UNREACHABLE_START not in result
        assert TS_HANDLE_WEAKREF_CALLBACKS_START not in result
        assert TS_FINALIZE_GARBAGE_STOP not in result
        assert TS_HANDLE_RESURRECTED_STOP not in result
        assert TS_CLEAR_WEAKREFS_STOP not in result
        assert TS_DELETE_GARBAGE_START not in result
        assert FINALIZED_GARBAGE_COUNT not in result
        assert DELETED_GARBAGE_COUNT not in result
        assert CLEAR_WEAKREFS_COUNT not in result

    def test_deduce_unreachable_only(self) -> None:
        item = create_mock_stats_item(
            ts_deduce_unreachable_start=1_000_500,
            ts_deduce_unreachable_stop=1_001_000,
        )
        result = to_mapping(item)
        assert result[TS_DEDUCE_UNREACHABLE_START] == 1_000_500
        assert result[TS_DEDUCE_UNREACHABLE_STOP] == 1_001_000
        assert INCREMENT_SIZE not in result
        assert ALIVE_SIZE not in result
        assert TS_MARK_ALIVE_START not in result
        assert TS_HANDLE_WEAKREF_CALLBACKS_START not in result
        assert TS_FINALIZE_GARBAGE_STOP not in result
        assert TS_HANDLE_RESURRECTED_STOP not in result
        assert TS_CLEAR_WEAKREFS_STOP not in result
        assert TS_DELETE_GARBAGE_START not in result
        assert FINALIZED_GARBAGE_COUNT not in result
        assert DELETED_GARBAGE_COUNT not in result
        assert CLEAR_WEAKREFS_COUNT not in result

    def test_finalize_garbage_only(self) -> None:
        item = create_mock_stats_item(
            ts_finalize_garbage_stop=1_005_000,
            finalized_garbage_count=42,
        )
        result = to_mapping(item)
        assert result[TS_FINALIZE_GARBAGE_STOP] == 1_005_000
        assert result[FINALIZED_GARBAGE_COUNT] == 42
        assert DELETED_GARBAGE_COUNT not in result
        assert CLEAR_WEAKREFS_COUNT not in result

    def test_delete_garbage_only(self) -> None:
        item = create_mock_stats_item(
            ts_delete_garbage_start=1_008_000,
            ts_delete_garbage_stop=1_009_000,
            deleted_garbage_count=13,
        )
        result = to_mapping(item)
        assert result[TS_DELETE_GARBAGE_START] == 1_008_000
        assert result[TS_DELETE_GARBAGE_STOP] == 1_009_000
        assert result[DELETED_GARBAGE_COUNT] == 13
        assert FINALIZED_GARBAGE_COUNT not in result
        assert CLEAR_WEAKREFS_COUNT not in result

    def test_clear_weakrefs_only(self) -> None:
        item = create_mock_stats_item(
            ts_clear_weakrefs_stop=1_007_000,
            clear_weakrefs_count=7,
        )
        result = to_mapping(item)
        assert result[TS_CLEAR_WEAKREFS_STOP] == 1_007_000
        assert result[CLEAR_WEAKREFS_COUNT] == 7
        assert FINALIZED_GARBAGE_COUNT not in result
        assert DELETED_GARBAGE_COUNT not in result

    def test_all_partial_phases(self) -> None:
        item = create_mock_stats_item(
            increment_size=500,
            alive_size=300,
            ts_mark_alive_start=1_000_500,
            ts_mark_alive_stop=1_001_000,
            ts_fill_increment_start=1_001_500,
            ts_fill_increment_stop=1_002_000,
            ts_deduce_unreachable_start=1_002_500,
            ts_deduce_unreachable_stop=1_003_000,
            ts_handle_weakref_callbacks_start=1_003_000,
            ts_handle_weakref_callbacks_stop=1_004_000,
            ts_finalize_garbage_stop=1_005_000,
            finalized_garbage_count=42,
            ts_handle_resurrected_stop=1_006_000,
            ts_clear_weakrefs_stop=1_007_000,
            clear_weakrefs_count=7,
            ts_delete_garbage_start=1_008_000,
            ts_delete_garbage_stop=1_009_000,
            deleted_garbage_count=13,
        )
        result = to_mapping(item)
        assert result[INCREMENT_SIZE] == 500
        assert result[ALIVE_SIZE] == 300
        assert result[TS_MARK_ALIVE_START] == 1_000_500
        assert result[TS_MARK_ALIVE_STOP] == 1_001_000
        assert result[TS_FILL_INCREMENT_START] == 1_001_500
        assert result[TS_FILL_INCREMENT_STOP] == 1_002_000
        assert result[TS_DEDUCE_UNREACHABLE_START] == 1_002_500
        assert result[TS_DEDUCE_UNREACHABLE_STOP] == 1_003_000
        assert result[TS_HANDLE_WEAKREF_CALLBACKS_START] == 1_003_000
        assert result[TS_HANDLE_WEAKREF_CALLBACKS_STOP] == 1_004_000
        assert result[TS_FINALIZE_GARBAGE_STOP] == 1_005_000
        assert result[FINALIZED_GARBAGE_COUNT] == 42
        assert result[TS_HANDLE_RESURRECTED_STOP] == 1_006_000
        assert result[TS_CLEAR_WEAKREFS_STOP] == 1_007_000
        assert result[CLEAR_WEAKREFS_COUNT] == 7
        assert result[TS_DELETE_GARBAGE_START] == 1_008_000
        assert result[TS_DELETE_GARBAGE_STOP] == 1_009_000
        assert result[DELETED_GARBAGE_COUNT] == 13


class TestToMapping:
    def test_regular_item(self, simple_item: GCStatsInfo) -> None:
        result = to_mapping(simple_item)

        assert isinstance(result, Mapping)
        assert result[GEN] == 0
        assert result[IID] == 1
        assert result[TS_START] == 1_000_000
        assert result[TS_STOP] == 2_000_000
        assert result[HEAP_SIZE] == 1024
        assert result[COLLECTIONS] == 5
        assert result[COLLECTED] == 50
        assert result[UNCOLLECTABLE] == 0
        assert result[CANDIDATES] == 10
        assert result[DURATION] == 0.005
        assert INCREMENT_SIZE not in result

    def test_incremental_item(self, incremental_item: GCStatsInfo) -> None:
        result = to_mapping(incremental_item)

        assert isinstance(result, Mapping)
        assert result[GEN] == 1
        assert result[IID] == 2
        assert result[TS_START] == 3_000_000
        assert result[TS_STOP] == 4_000_000
        assert result[HEAP_SIZE] == 2048
        assert result[COLLECTIONS] == 10
        assert result[COLLECTED] == 100
        assert result[UNCOLLECTABLE] == 1
        assert result[CANDIDATES] == 20
        assert result[DURATION] == 0.01
        assert result[INCREMENT_SIZE] == 500
        assert result[ALIVE_SIZE] == 300
        assert result[TS_MARK_ALIVE_START] == 3_000_500
        assert result[TS_MARK_ALIVE_STOP] == 3_001_000
        assert result[TS_FILL_INCREMENT_START] == 3_001_500
        assert result[TS_FILL_INCREMENT_STOP] == 3_002_000
        assert result[TS_DEDUCE_UNREACHABLE_START] == 3_002_500
        assert result[TS_DEDUCE_UNREACHABLE_STOP] == 3_003_000
        assert result[TS_HANDLE_WEAKREF_CALLBACKS_START] == 3_003_000
        assert result[TS_HANDLE_WEAKREF_CALLBACKS_STOP] == 3_004_000
        assert result[TS_FINALIZE_GARBAGE_STOP] == 3_005_000
        assert result[FINALIZED_GARBAGE_COUNT] == 42
        assert result[TS_HANDLE_RESURRECTED_STOP] == 3_006_000
        assert result[TS_CLEAR_WEAKREFS_STOP] == 3_007_000
        assert result[CLEAR_WEAKREFS_COUNT] == 7
        assert result[TS_DELETE_GARBAGE_START] == 3_008_000
        assert result[TS_DELETE_GARBAGE_STOP] == 3_009_000
        assert result[DELETED_GARBAGE_COUNT] == 13

    def test_instant_item(self, instant_item: InstantMsg) -> None:
        result = to_mapping(instant_item)

        assert isinstance(result, Mapping)
        assert result[TYPE] == "i"
        assert result[NAME] == START_EVENT
        assert result[TS] == 5_000_000

    def test_to_mapping_unknown_type_raises(self) -> None:
        import pytest

        with pytest.raises(NotImplementedError, match="Unknown item type"):
            to_mapping("not a valid item")  # type: ignore[arg-type]

    def test_loss_item(self, loss_item: LossMsg) -> None:
        result = to_mapping(loss_item)

        assert isinstance(result, Mapping)
        assert result[IID] == 1
        assert result[TS_START] == 1_000
        assert result[TS_STOP] == 2_000

    def test_a_loss_item_names_every_generation_in_the_interval(self, loss_item: LossMsg) -> None:
        """One record per poll, so the counts are per generation and the
        record says which is which rather than carrying three sets and naming
        none of them."""
        assert to_mapping(loss_item)[GENS] == [
            {GEN: 1, OBSERVED_COUNT: 4, LOST_FROM: 42, LOST_COUNT: 5, LOST_PAUSE_NS: 8_100_000},
            {GEN: 2, OBSERVED_COUNT: 1, LOST_FROM: 0, LOST_COUNT: 0, LOST_PAUSE_NS: 0},
        ]

    def test_a_loss_item_carries_no_collections(self, loss_item: LossMsg) -> None:
        """What keeps ``is_gc_stats`` off it."""
        assert COLLECTIONS not in to_mapping(loss_item)


class TestIsLoss:
    def test_loss_returns_true(self, loss_item: LossMsg) -> None:
        assert is_loss(loss_item) is True

    def test_gc_stats_returns_false(self, simple_item: GCStatsInfo) -> None:
        assert is_loss(simple_item) is False

    def test_instant_returns_false(self, instant_item: InstantMsg) -> None:
        assert is_loss(instant_item) is False

    def test_the_existing_guards_reject_it(self, loss_item: LossMsg) -> None:
        """``to_mapping`` and the converters dispatch on these three, so a
        record answering to two of them would take whichever branch came
        first."""
        assert is_gc_stats(loss_item) is False
        assert is_instant(loss_item) is False


class TestGuardsAreMutuallyExclusive:
    def test_exactly_one_guard_claims_each_record_type(
        self,
        simple_item: GCStatsInfo,
        incremental_item: GCStatsInfo,
        instant_item: InstantMsg,
        loss_item: LossMsg,
    ) -> None:
        """A record two guards claim takes a different branch depending on
        which guard a call site happens to ask first, and does it silently.
        Keeping them disjoint is cheaper than auditing every dispatch order
        as call sites come and go. Exactly one may hold, for every record
        type, whatever fields those types grow later."""
        for item in (simple_item, incremental_item, instant_item, loss_item):
            claims = [is_gc_stats(item), is_instant(item), is_loss(item)]
            assert claims.count(True) == 1, f"{type(item).__name__} matched {claims}"
