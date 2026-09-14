"""The mark grammar: what a workload writes and a reader reads back."""

import pytest

from gcmon.model.marks import Mark, Side, format_mark, parse_mark


class TestRoundTrip:
    @pytest.mark.parametrize("bench", ["bm_base64", "a", "with-dash", "with_underscore", "0"])
    @pytest.mark.parametrize("region, phase_region", [(1, 1), (2, 2), (42, 7), (1000, 1)])
    @pytest.mark.parametrize("side", [Side.BEGIN, Side.END])
    def test_a_formatted_mark_parses_back_to_its_parts(
        self, bench: str, region: int, phase_region: int, side: Side
    ) -> None:
        assert parse_mark(format_mark(bench, region, phase_region, side)) == Mark(bench, region, phase_region, side)


class TestTheLiteralShape:
    def test_the_grammar_is_pinned(self) -> None:
        assert format_mark("bm_base64", 4, 2, Side.BEGIN) == "gcmon:bm_base64:4:2:begin"
        assert format_mark("bm_base64", 4, 2, Side.END) == "gcmon:bm_base64:4:2:end"

    def test_a_mark_is_selectable_by_prefix(self) -> None:
        assert format_mark("bm_base64", 1, 1, Side.BEGIN).startswith("gcmon:")

    def test_a_separator_in_the_benchmark_name_cannot_reach_the_grammar(self) -> None:
        assert format_mark("a:b", 1, 1, Side.BEGIN) == "gcmon:a_b:1:1:begin"

    @pytest.mark.parametrize(
        "bench, expected",
        [("a b", "a_b"), ("a.b", "a_b"), ("a/b", "a_b"), ("bm[x]", "bm_x_"), ("é", "_")],
    )
    def test_a_benchmark_name_keeps_only_word_characters(self, bench: str, expected: str) -> None:
        assert format_mark(bench, 1, 1, Side.BEGIN) == f"gcmon:{expected}:1:1:begin"


class TestTheWriterNeverBeatsTheReader:
    """Whatever `format_mark` emits, `parse_mark` reads back."""

    @pytest.mark.parametrize("bench", ["", ":", "::", "   ", "éé", "...", "bm_ok", "a" * 200])
    def test_any_name_produces_a_mark_that_parses(self, bench: str) -> None:
        assert parse_mark(format_mark(bench, 1, 1, Side.BEGIN)) is not None

    def test_an_unnamed_workload_keeps_a_field(self) -> None:
        assert format_mark("", 7, 3, Side.END) == "gcmon:_:7:3:end"


class TestParsingSomethingElse:
    @pytest.mark.parametrize(
        "name",
        [
            "",
            "start GC monitor",
            "gcmon",
            "gcmon:bm_base64:1",
            "gcmon:bm_base64:1:1:begin:extra",
            "gcmon:bm_base64:1:begin",
            "other:bm_base64:1:1:begin",
            "gcmon:bm_base64:1:1:middle",
            "gcmon:bm_base64:x:1:begin",
            "gcmon:bm_base64:1:x:begin",
            "gcmon:bm_base64:-1:1:begin",
            "gcmon:bm_base64:1:-1:begin",
            "gcmon::1:1:begin",
            "gcmon:bm_base64:\u0663:1:begin",
        ],
    )
    def test_returns_none_rather_than_raising(self, name: str) -> None:
        assert parse_mark(name) is None


class TestOneModule:
    def test_the_writer_and_the_reader_cannot_drift(self) -> None:
        assert format_mark.__module__ == parse_mark.__module__
