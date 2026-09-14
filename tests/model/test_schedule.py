"""The grid, apart from the loop that walks it.

Positions are `t0 + k * rate`, and the idle is to the position after the
one a tick ended on. See ADR-0019.
"""

import pytest

from gcmon.model.schedule import MIN_IDLE_NS, idle_to_next_position, position_of


class TestThePositionOfAnInstant:
    """Where an instant falls on `t0 + k * rate`, apart from the loop."""

    def test_the_run_starts_on_the_first_position(self) -> None:
        assert position_of(0, 0, 100_000_000) == 0

    def test_an_instant_on_a_position_is_that_position(self) -> None:
        assert position_of(200_000_000, 0, 100_000_000) == 2

    def test_an_instant_between_two_belongs_to_the_one_behind(self) -> None:
        """The position a tick starting here occupies is the one that has come
        round, not the one it is waiting for."""
        assert position_of(250_000_000, 0, 100_000_000) == 2

    def test_a_rate_that_is_not_one_has_no_grid_to_answer(self) -> None:
        """The division has no meaning without a rate, and the loop refuses
        one before a tick ever runs."""
        with pytest.raises(AssertionError):
            position_of(250_000_000, 0, 0)


class TestTheIdleToTheNextPosition:
    """The wait one tick asks for: to the position after the one it ended on."""

    def test_it_subtracts_what_the_tick_cost(self) -> None:
        """The defect: the loop used to wait the whole rate on top of the tick,
        so the target's size decided how often gcmon looked."""
        assert idle_to_next_position(30_000_000, 0, 100_000_000) == 70_000_000

    def test_a_tick_past_its_position_waits_for_the_next_one(self) -> None:
        """A tick 50 ms over does not start the next one late: it goes to the
        position after, so starts stay on the grid."""
        assert idle_to_next_position(150_000_000, 0, 100_000_000) == 50_000_000

    def test_a_tick_ending_on_a_position_waits_a_whole_rate(self) -> None:
        """That position is now, so nothing can start on it any more."""
        assert idle_to_next_position(100_000_000, 0, 100_000_000) == 100_000_000

    def test_a_tick_ending_a_hair_early_still_yields(self) -> None:
        """Otherwise the loop re-enters immediately and pins gcmon at a full
        duty cycle against a target that is already struggling."""
        assert idle_to_next_position(99_999_500, 0, 100_000_000) == MIN_IDLE_NS

    def test_a_long_stall_costs_one_division(self) -> None:
        """A tick that stalled for a minute at a 1 ms rate ran through sixty
        thousand positions. Stepping to them would cost sixty thousand
        iterations inside the poll interval."""
        assert idle_to_next_position(60_000_000_000, 0, 1_000_000) == MIN_IDLE_NS
