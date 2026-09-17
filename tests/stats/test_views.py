"""Tests for the words the `--stats` flag takes."""

import pytest

from gcmon.stats.views import STATS_OFF_WORDS, StatsView


class TestTheVocabularyOfTheFlag:
    """`StatsView` owns every word `--stats` and `GCMON_STATS` take."""

    @pytest.mark.parametrize("word, view", [("total", StatsView.TOTAL), ("full", StatsView.FULL)])
    def test_each_view_word_parses_to_its_view(self, word: str, view: StatsView) -> None:
        assert StatsView.parse(word) is view

    @pytest.mark.parametrize("word", STATS_OFF_WORDS)
    def test_each_off_word_parses_to_no_table(self, word: str) -> None:
        assert StatsView.parse(word) is None

    @pytest.mark.parametrize(
        "word, view",
        [
            ("Total", StatsView.TOTAL),
            ("TOTAL", StatsView.TOTAL),
            (" total", StatsView.TOTAL),
            ("total\n", StatsView.TOTAL),
            (" Off ", None),
            ("NO", None),
        ],
    )
    def test_case_insensitive_and_stripped(self, word: str, view: StatsView | None) -> None:
        assert StatsView.parse(word) is view

    @pytest.mark.parametrize("word", ["", "  ", "all", "brief", "1", "true", "yes", "on", "totals"])
    def test_a_word_it_does_not_know_is_refused(self, word: str) -> None:
        """The truthy opposites of the off words included."""
        with pytest.raises(ValueError):
            StatsView.parse(word)

    def test_words_lists_both_views_and_every_off_word(self) -> None:
        assert StatsView.words() == ["total", "full", *STATS_OFF_WORDS]

    def test_every_word_offered_is_a_word_accepted(self) -> None:
        """`words()` feeds argparse `choices`, so `parse` has to take them all."""
        for word in StatsView.words():
            StatsView.parse(word)  # does not raise
