import pytest

from gcmon.exporters.trace_converter import (
    convert_item_to_trace_format,
    convert_to_trace_format,
    duration_text,
    seen_text,
)
from gcmon.model.names import (
    ALIVE_SIZE,
    CLEAR_WEAKREFS_COUNT,
    FINALIZED_GARBAGE_COUNT,
    GENERATIONS,
    HEAP_SIZE,
    INCREMENT_SIZE,
    TS_CLEAR_WEAKREFS_STOP,
    TS_FINALIZE_GARBAGE_STOP,
    TS_HANDLE_RESURRECTED_STOP,
    gc_pause_slice_name,
)
from gcmon.model.protocol import TItem
from gcmon.model.trace_event import Counter, Instant, Slice
from tests.data_helpers import create_instant_msg
from tests.helpers import create_mock_incremental_item, create_mock_loss_item, create_mock_stats_item, proc


class TestDurationText:
    """The readable half of a pause total.

    Exactness lives in the `_ns` arg beside it; this one only has to be read
    correctly at a glance, which the digits are not.
    """

    @pytest.mark.parametrize(
        ("ns", "text"),
        [
            (3_316_458_100, "3s 316ms 458µs 100ns"),
            (5_000_000, "5ms"),
            (200, "200ns"),
            (1_000_000_100, "1s 100ns"),
            (90_000_000_000, "1m 30s"),
            (3_600_000_000_000, "1h"),
            (0, "0ns"),
            (-5_000_100, "-5ms 100ns"),
        ],
    )
    def test_it_reads_as_a_duration(self, ns: int, text: str) -> None:
        assert duration_text(ns) == text

    def test_the_units_multiply_back_to_the_nanoseconds(self) -> None:
        """Every unit a component carries, against the number it came from.
        A wrong divisor produces text that still looks like a duration."""
        sizes = {"h": 3_600_000_000_000, "m": 60_000_000_000, "s": 1_000_000_000, "ms": 1_000_000, "µs": 1_000}

        for ns in (1, 999, 1_000, 3_316_458_100, 86_400_000_000_123):
            total = 0
            for part in duration_text(ns).split():
                digits = part.rstrip("hmsnµ")
                total += int(digits) * sizes.get(part.removeprefix(digits), 1)
            assert total == ns


class TestSeenText:
    """How much of an interval gcmon read, for a reader deciding whether to
    trust the bar's neighbours."""

    @pytest.mark.parametrize(
        ("observed", "lost", "text"),
        [
            (47, 7, "87.0% (47 of 54)"),
            (0, 5, "0.0% (0 of 5)"),
            (9, 0, "100.0% (9 of 9)"),
            (1, 2, "33.3% (1 of 3)"),
        ],
    )
    def test_it_reads_as_a_share_of_a_total(self, observed: int, lost: int, text: str) -> None:
        assert seen_text(observed, lost) == text

    def test_an_empty_interval_divides_by_nothing(self) -> None:
        """No collection ran and none was lost. A loss record never carries
        this, but the helper must not raise on the way to finding that out."""
        assert seen_text(0, 0) == "100.0% (0 of 0)"

    def test_the_total_is_what_ran_not_what_was_read(self) -> None:
        """The denominator is the reason this is worth writing out: a bare
        percentage says how bad the blindness was, not how much there was to
        be blind about."""
        assert seen_text(2, 98).endswith("(2 of 100)")


class TestAGenerationTheCounterTableNeverHeld:
    """A counter's display name is rendered for `GENERATIONS` at import and
    read off that row. No collector emits a fourth generation, so what is
    asked here is that a capture carrying one converts anyway, with the
    names spelled on the spot instead."""

    def _counters(self, gen: int) -> dict[str, str]:
        """``{metric: display name}`` for the counters one record draws."""
        events = convert_item_to_trace_format(proc(1), create_mock_stats_item(gen=gen, uncollectable=2))
        return {e.metric: e.display_name for e in events if isinstance(e, Counter)}

    def test_it_draws_the_counters_a_held_generation_draws(self) -> None:
        """A metric the fallback misses is a track the record never gets."""
        assert self._counters(max(GENERATIONS) + 1).keys() == self._counters(max(GENERATIONS)).keys()

    def test_no_track_shares_a_name_with_a_held_generation(self) -> None:
        """Sharing a name would draw the record onto the row beside it.
        `heap_size` is the one they do share: it gauges the interpreter
        rather than a generation, so it carries none (ADR-0004).
        """
        beyond = set(self._counters(max(GENERATIONS) + 1).values())

        assert beyond & set(self._counters(max(GENERATIONS)).values()) == {HEAP_SIZE}


class TestTheSizesAPauseCarries:
    """`increment_size` stops below the oldest generation and `alive_size`
    starts above the youngest, whatever the record itself holds."""

    @pytest.mark.parametrize(
        ("gen_number", "sizes"),
        [
            (0, {INCREMENT_SIZE}),
            (1, {INCREMENT_SIZE, ALIVE_SIZE}),
            (2, {ALIVE_SIZE}),
        ],
    )
    def test_a_generation_gets_only_the_sizes_it_has(self, gen_number: int, sizes: set[str]) -> None:
        record = create_mock_incremental_item(gen=gen_number)

        events = convert_item_to_trace_format(proc(1), record)

        pause = next(e for e in events if isinstance(e, Slice) and e.name == gc_pause_slice_name(gen_number))
        assert pause.args.keys() & {INCREMENT_SIZE, ALIVE_SIZE} == sizes


class TestAPhaseWhoseStartIsMissing:
    """Three phases have no start of their own: each begins where the one
    before it stopped. A record gcmon wrote holds both or neither, so only a
    hand-edited line gets here, and it still has to convert."""

    @pytest.mark.parametrize(
        "fields",
        [
            pytest.param({TS_FINALIZE_GARBAGE_STOP: 9_000, FINALIZED_GARBAGE_COUNT: 1}, id="finalize garbage"),
            pytest.param({TS_HANDLE_RESURRECTED_STOP: 9_000}, id="handle resurrected"),
            pytest.param({TS_CLEAR_WEAKREFS_STOP: 9_000, CLEAR_WEAKREFS_COUNT: 1}, id="clear weakrefs"),
        ],
    )
    def test_the_record_converts_and_draws_the_pause_alone(self, fields: dict[str, int]) -> None:
        record = create_mock_stats_item(gen=0, **fields)

        events = convert_item_to_trace_format(proc(1), record)

        assert {e.name for e in events if isinstance(e, Slice)} == {gc_pause_slice_name(0)}


class TestAnItemOfNoKnownKind:
    """`TItem` is a union of three, and the guards that take it apart read
    attributes rather than types, so nothing stops a fourth thing arriving.
    Converting it to nothing would drop a record silently, which is a row a
    reader never learns is missing; `to_mapping` refuses the same way."""

    def test_converting_one_refuses_rather_than_dropping_it(self) -> None:
        with pytest.raises(NotImplementedError, match="Unknown item type"):
            convert_to_trace_format({1: ["neither a record nor an instant nor a loss"]})  # type: ignore[list-item]

    def test_the_three_kinds_it_knows_convert(self) -> None:
        """The refusal above only says something about a fourth kind if the
        three real ones reach their branches, so all three go in together."""
        capture: list[TItem] = [create_mock_stats_item(), create_mock_loss_item(), create_instant_msg()]

        assert {type(e) for e in convert_to_trace_format({1: capture})} == {Slice, Counter, Instant}
