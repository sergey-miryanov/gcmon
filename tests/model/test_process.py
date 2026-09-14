"""What identifies a `Process`, and what it may not carry."""

from gcmon.model.process import Process


class TestAProcessIsThePairAndNothingElse:
    """Every field is part of identity, which is what lets msgspec generate
    the hash and the comparisons in C. A field holding what gcmon read about
    the process would put them back in Python, on a value the encoder keys a
    dict on several times per event (ADR-0025)."""

    def test_the_struct_holds_the_pair_alone(self) -> None:
        assert Process.__struct_fields__ == ("pid", "pid_epoch")

    def test_two_naming_the_same_process_are_equal(self) -> None:
        assert Process(12345, 1) == Process(12345, 1)

    def test_it_keys_a_dict_on_the_pair(self) -> None:
        assert {Process(12345, 1): "rings"}[Process(12345, 1)] == "rings"

    def test_a_successor_on_the_pid_is_another_process(self) -> None:
        assert Process(12345, 2) != Process(12345, 1)


class TestProcessesSort:
    def test_by_pid_then_epoch(self) -> None:
        """The order the `--stats` table prints its blocks in."""
        out_of_order = [Process(2, 1), Process(1, 2), Process(1, 1)]

        assert sorted(out_of_order) == [Process(1, 1), Process(1, 2), Process(2, 1)]


class TestProcessReadsAsItsPid:
    def test_the_first_to_hold_a_pid_is_unmarked(self) -> None:
        assert str(Process(12345, 1)) == "12345"

    def test_a_later_one_carries_the_epoch(self) -> None:
        assert str(Process(12345, 2)) == "12345#2"

    def test_the_suffix_is_available_on_its_own(self) -> None:
        """The table writes `12345:0#2`, so it needs the piece rather than
        the whole label."""
        assert Process(12345, 2).epoch_suffix == "#2"
