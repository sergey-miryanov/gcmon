from gcmon.support.time_units import dur_to_ms


class TestTimeConversions:
    def test_dur_to_ms(self) -> None:
        assert dur_to_ms(2_000_000) == 2.0

    def test_dur_to_ms_zero(self) -> None:
        assert dur_to_ms(0) == 0.0

    def test_dur_to_ms_negative(self) -> None:
        assert dur_to_ms(-3_000_000) == -3.0

    def test_dur_to_ms_keeps_sub_microsecond(self) -> None:
        assert dur_to_ms(500) == 0.0005
