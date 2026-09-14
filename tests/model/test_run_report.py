import pytest

from gcmon.model.run_report import RunReport


class TestRunReportOverran:
    """When the summary is allowed to say a smaller rate cannot help.

    Not on any skipped position at all. The loop waits on an event whose
    timeout the platform rounds up to its scheduler tick (ADR-0019), so a long
    run is near certain to overshoot a position occasionally, and reading that
    as an overrun would contradict the advice on the strength of one late
    wake-up.
    """

    def test_a_run_that_hit_every_position_did_not_overrun(self) -> None:
        assert not RunReport(ticks_run=600, ticks_scheduled=600).overran

    def test_one_miss_in_a_long_run_is_not_an_overrun(self) -> None:
        """Ten minutes at the default rate is about 6000 ticks. One hiccup --
        an oversized wake-up, a momentary fan-out -- must not rewrite the
        advice for the whole run."""
        assert not RunReport(ticks_run=5_999, ticks_scheduled=6_000).overran

    def test_a_run_missing_most_of_its_positions_overran(self) -> None:
        assert RunReport(ticks_run=188, ticks_scheduled=600).overran

    @pytest.mark.parametrize(
        ("ticks_run", "ticks_scheduled", "overran"),
        [
            (900, 1_000, False),
            (899, 1_000, True),
        ],
    )
    def test_the_share_is_a_floor_not_a_ceiling(self, ticks_run: int, ticks_scheduled: int, overran: bool) -> None:
        """Exactly a tenth missing is not yet an overrun; past it is."""
        assert RunReport(ticks_run=ticks_run, ticks_scheduled=ticks_scheduled).overran is overran

    def test_a_run_with_no_ticks_divides_by_nothing(self) -> None:
        """A run stopped before its first tick schedules none. Nothing to
        report and nothing to divide by."""
        assert not RunReport(ticks_run=0, ticks_scheduled=0).overran
