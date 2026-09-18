"""Tests for monitoring options validation."""

from __future__ import annotations

import logging
from argparse import ArgumentParser, Namespace
from pathlib import Path

import pytest

from gcmon.cli.monitor._env import ENV_FORMAT, ENV_RATE, ENV_STATS
from gcmon.cli.monitor.monitoring_options import (
    RSS_CAPABLE_FORMATS,
    MonitoringOptions,
    add_monitoring_options,
    get_monitoring_options,
)
from gcmon.model.names import DURATION, RSS
from gcmon.stats.stats_output import TOTAL_LABEL
from gcmon.stats.views import STATS_OFF_WORDS, StatsView, TableFormat
from gcmon.support.vocabulary import CMD_MONITOR, CMD_RUN, FORMAT_JSONL, FORMAT_PERFETTO, FORMAT_STDOUT, PROGRAM_NAME


def _make_args(**overrides: object) -> Namespace:
    defaults: dict[str, object] = {
        "output": Path("trace.json"),
        "rate": 0.1,
        DURATION: 0.05,
        "format": FORMAT_PERFETTO,
        "flush_threshold": 100,
        "stats": None,
        "table_format": TableFormat.PLAIN,
        "control_name": None,
        RSS: False,
        "rss_interval": 1.0,
    }
    return Namespace(**{**defaults, **overrides})


class TestOutputPathValidation:
    def test_valid_path_in_cwd(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.chdir(tmp_path)
        args = _make_args(output=Path("trace.json"))
        result = get_monitoring_options(args)
        assert result is not None
        assert result.output_path == Path("trace.json")

    def test_valid_path_in_subdirectory(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "subdir").mkdir()
        args = _make_args(output=Path("subdir/trace.json"))
        result = get_monitoring_options(args)
        assert result is not None

    def test_missing_parent_directory_rejected(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.chdir(tmp_path)
        args = _make_args(output=Path("nonexistent/trace.json"))
        result = get_monitoring_options(args)
        assert result is None

    def test_stdout_format_skips_path_validation(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.chdir(tmp_path)
        args = _make_args(output=Path("nonexistent/trace.json"), format=FORMAT_STDOUT)
        result = get_monitoring_options(args)
        assert result is not None

    def test_dot_path_resolves_to_cwd(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.chdir(tmp_path)
        args = _make_args(output=Path("."))
        result = get_monitoring_options(args)
        assert result is not None

    def test_absolute_path_passes(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.chdir(tmp_path)
        path = tmp_path / "trace.json"
        args = _make_args(output=path)
        result = get_monitoring_options(args)
        assert result is not None

    def test_path_traversal_parent_exists_passes(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.chdir(tmp_path)
        args = _make_args(output=Path("../trace.json"))
        result = get_monitoring_options(args)
        assert result is not None


class TestRssIntervalWarning:
    def test_rss_interval_shorter_than_rate_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.WARNING)
        args = _make_args(rss=True, rss_interval=0.05, rate=0.1)
        result = get_monitoring_options(args)
        assert result is not None
        assert result.rss_enabled
        assert "shorter than poll rate" in caplog.text

    def test_rss_interval_equal_to_rate_no_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.WARNING)
        args = _make_args(rss=True, rss_interval=0.1, rate=0.1)
        result = get_monitoring_options(args)
        assert result is not None
        assert "shorter than poll rate" not in caplog.text

    def test_rss_interval_longer_than_rate_no_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.WARNING)
        args = _make_args(rss=True, rss_interval=2.0, rate=0.1)
        result = get_monitoring_options(args)
        assert result is not None
        assert "shorter than poll rate" not in caplog.text

    def test_rss_disabled_no_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.WARNING)
        args = _make_args(rss=False, rss_interval=0.05, rate=0.1)
        result = get_monitoring_options(args)
        assert result is not None
        assert not result.rss_enabled
        assert "shorter than poll rate" not in caplog.text

    def test_rss_interval_zero_rejected(self) -> None:
        args = _make_args(rss=True, rss_interval=0.0)
        result = get_monitoring_options(args)
        assert result is None

    def test_rss_interval_negative_rejected(self) -> None:
        args = _make_args(rss=True, rss_interval=-1.0)
        result = get_monitoring_options(args)
        assert result is None

    def test_rss_disabled_ignores_zero_interval(self) -> None:
        args = _make_args(rss=False, rss_interval=0.0)
        result = get_monitoring_options(args)
        assert result is not None
        assert not result.rss_enabled

    def test_rss_disabled_ignores_negative_interval(self) -> None:
        args = _make_args(rss=False, rss_interval=-1.0)
        result = get_monitoring_options(args)
        assert result is not None
        assert not result.rss_enabled


class TestRssFormatWarning:
    """--rss is accepted for every format, but only some exporters implement
    add_rss_sample; the rest discard the samples and must say so."""

    @pytest.mark.parametrize("output_format", [FORMAT_JSONL, FORMAT_STDOUT])
    def test_unsupported_format_warns(self, output_format: str, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.WARNING)
        args = _make_args(rss=True, format=output_format)
        result = get_monitoring_options(args)
        assert result is not None
        assert result.rss_enabled
        assert "RSS tracking is not supported" in caplog.text

    @pytest.mark.parametrize("output_format", list(RSS_CAPABLE_FORMATS))
    def test_supported_format_does_not_warn(self, output_format: str, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.WARNING)
        args = _make_args(rss=True, format=output_format)
        result = get_monitoring_options(args)
        assert result is not None
        assert "RSS tracking is not supported" not in caplog.text

    def test_rss_disabled_does_not_warn_on_unsupported_format(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.WARNING)
        args = _make_args(rss=False, format=FORMAT_JSONL)
        result = get_monitoring_options(args)
        assert result is not None
        assert "RSS tracking is not supported" not in caplog.text


class TestTheFormatEnvironmentVariable:
    """`GCMON_FORMAT` takes the words `--format` takes, and refuses the rest
    rather than substituting one.

    The same shape ADR-0018 settled for `--stats`. argparse takes a string
    default as given rather than checking it against `choices`, so a word from
    the environment reaches here; a word from the flag dies in the parser.
    """

    def _options(self, argv: list[str]) -> MonitoringOptions | None:
        from gcmon.cli.main import _create_parser

        # The parser reads the variable while it is being built, so it has to
        # be built after the test sets it.
        return get_monitoring_options(_create_parser().parse_args(argv))

    @pytest.mark.parametrize("word", [FORMAT_PERFETTO, FORMAT_JSONL, FORMAT_STDOUT])
    def test_each_word_is_taken(self, monkeypatch: pytest.MonkeyPatch, word: str) -> None:
        monkeypatch.setenv(ENV_FORMAT, word)

        result = self._options([CMD_MONITOR, "12345"])

        assert result is not None
        assert result.output_format == word

    def test_unset_gives_perfetto(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ENV_FORMAT, raising=False)

        result = self._options([CMD_MONITOR, "12345"])

        assert result is not None
        assert result.output_format == FORMAT_PERFETTO

    @pytest.mark.parametrize("value", ["chrome", "trace", "chrome+perfetto", "pftrace"])
    def test_a_word_the_flag_would_refuse_fails_the_run(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, value: str
    ) -> None:
        """`GCMON_FORMAT=chrome` from an older release stops the run at
        startup rather than writing a format nobody asked for."""
        caplog.set_level(logging.ERROR)
        monkeypatch.setenv(ENV_FORMAT, value)

        assert self._options([CMD_MONITOR, "12345"]) is None
        assert ENV_FORMAT in caplog.text
        assert value in caplog.text
        for remaining in (FORMAT_PERFETTO, FORMAT_JSONL, FORMAT_STDOUT):
            assert remaining in caplog.text

    def test_the_rejected_value_is_never_echoed_as_accepted(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Spec 0040's complaint, which this shape closes: the log used to
        read `Format: perfetto` for a run configured as `chrome`."""
        caplog.set_level(logging.INFO)
        monkeypatch.setenv(ENV_FORMAT, "chrome")

        self._options([CMD_MONITOR, "12345"])

        assert "Format: perfetto" not in caplog.text


class TestTheStatsFlagCarriesTheView:
    """`--stats` requires a value, and it is one of two words."""

    def _parse(self, argv: list[str]) -> Namespace:
        from gcmon.cli.main import _create_parser

        return _create_parser().parse_args(argv)

    @pytest.mark.parametrize(
        "word, view", [(StatsView.TOTAL.value, StatsView.TOTAL), (StatsView.FULL.value, StatsView.FULL)]
    )
    def test_each_word_selects_its_view(self, word: str, view: StatsView) -> None:
        result = get_monitoring_options(self._parse([CMD_MONITOR, "12345", f"--stats={word}"]))

        assert result is not None
        assert result.stats_view is view

    def test_no_flag_asks_for_no_table(self) -> None:
        result = get_monitoring_options(self._parse([CMD_MONITOR, "12345"]))

        assert result is not None
        assert result.stats_view is None

    @pytest.mark.parametrize(
        "argv",
        [
            [CMD_MONITOR, "12345", "--stats=total"],
            [CMD_MONITOR, "12345", "--stats", StatsView.TOTAL.value],
            [CMD_MONITOR, "--stats", StatsView.TOTAL.value, "12345"],
        ],
    )
    def test_the_pid_survives_the_flag(self, argv: list[str]) -> None:
        """The ordering an alias would have eaten: `--stats` before the pid."""
        assert self._parse(argv).pid == 12345

    def test_a_bare_flag_is_refused(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exit_info:
            self._parse([CMD_MONITOR, "12345", "--stats"])

        assert exit_info.value.code != 0
        err = capsys.readouterr().err
        assert StatsView.TOTAL.value in err
        assert StatsView.FULL.value in err

    def test_an_unknown_value_is_refused(self, capsys: pytest.CaptureFixture[str]) -> None:
        """`all` reads as the wider view and is not one."""
        with pytest.raises(SystemExit) as exit_info:
            self._parse([CMD_MONITOR, "12345", "--stats=all"])

        assert exit_info.value.code != 0
        err = capsys.readouterr().err
        assert StatsView.TOTAL.value in err
        assert StatsView.FULL.value in err

    def test_run_takes_the_same_two_words(self) -> None:
        result = get_monitoring_options(self._parse([CMD_RUN, "--stats=total", "-m", "timeit"]))

        assert result is not None
        assert result.stats_view is StatsView.TOTAL


class TestTheStatsEnvironmentVariable:
    """`GCMON_STATS` takes the same two words, and an unreadable value fails
    the run rather than falling back.
    """

    def _options(self, argv: list[str]) -> MonitoringOptions | None:
        from gcmon.cli.main import _create_parser

        # The parser reads the variable while it is being built, so it has to
        # be built after the test sets it.
        return get_monitoring_options(_create_parser().parse_args(argv))

    @pytest.mark.parametrize(
        "word, view", [(StatsView.TOTAL.value, StatsView.TOTAL), (StatsView.FULL.value, StatsView.FULL)]
    )
    def test_each_word_selects_its_view(self, monkeypatch: pytest.MonkeyPatch, word: str, view: StatsView) -> None:
        monkeypatch.setenv(ENV_STATS, word)

        result = self._options([CMD_MONITOR, "12345"])

        assert result is not None
        assert result.stats_view is view

    @pytest.mark.parametrize("value", [TOTAL_LABEL, "TOTAL", " total", "total\n"])
    def test_case_insensitive_and_stripped(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv(ENV_STATS, value)

        result = self._options([CMD_MONITOR, "12345"])

        assert result is not None
        assert result.stats_view is StatsView.TOTAL

    def test_the_flag_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_STATS, StatsView.FULL.value)

        result = self._options([CMD_MONITOR, "12345", "--stats=total"])

        assert result is not None
        assert result.stats_view is StatsView.TOTAL

    def test_unset_asks_for_no_table(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ENV_STATS, raising=False)

        result = self._options([CMD_MONITOR, "12345"])

        assert result is not None
        assert result.stats_view is None

    @pytest.mark.parametrize("value", ["1", "true", "all", "brief"])
    def test_a_value_it_does_not_know_fails_the_run(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, value: str
    ) -> None:
        """`GCMON_STATS=1` from an older release stops the run at startup."""
        caplog.set_level(logging.ERROR)
        monkeypatch.setenv(ENV_STATS, value)

        assert self._options([CMD_MONITOR, "12345"]) is None
        assert StatsView.TOTAL.value in caplog.text
        assert StatsView.FULL.value in caplog.text
        assert value in caplog.text

    def test_the_message_names_the_variable(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """argparse checks the flag's own values, so only the variable gets here."""
        caplog.set_level(logging.ERROR)
        monkeypatch.setenv(ENV_STATS, "1")

        self._options([CMD_MONITOR, "12345"])

        assert ENV_STATS in caplog.text


class TestTheWordsThatTurnTheTableOff:
    """`no`, `off`, `false` and `0` ask for no table. Their truthy opposites
    stay out.
    """

    def _parse(self, argv: list[str]) -> Namespace:
        from gcmon.cli.main import _create_parser

        return _create_parser().parse_args(argv)

    @pytest.mark.parametrize("word", STATS_OFF_WORDS)
    def test_the_flag_takes_each_of_them(self, word: str) -> None:
        result = get_monitoring_options(self._parse([CMD_MONITOR, "12345", f"--stats={word}"]))

        assert result is not None
        assert result.stats_view is None

    @pytest.mark.parametrize("word", STATS_OFF_WORDS)
    def test_the_variable_takes_each_of_them(self, monkeypatch: pytest.MonkeyPatch, word: str) -> None:
        from gcmon.cli.main import _create_parser

        monkeypatch.setenv(ENV_STATS, word)

        result = get_monitoring_options(_create_parser().parse_args([CMD_MONITOR, "12345"]))

        assert result is not None
        assert result.stats_view is None

    @pytest.mark.parametrize("value", ["Off", "OFF", " off", "off\n"])
    def test_the_variable_is_case_insensitive_and_stripped(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        from gcmon.cli.main import _create_parser

        monkeypatch.setenv(ENV_STATS, value)

        result = get_monitoring_options(_create_parser().parse_args([CMD_MONITOR, "12345"]))

        assert result is not None
        assert result.stats_view is None

    def test_the_flag_turns_off_what_the_variable_turned_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Why "no table" needs a spelling of its own."""
        from gcmon.cli.main import _create_parser

        monkeypatch.setenv(ENV_STATS, StatsView.FULL.value)

        result = get_monitoring_options(_create_parser().parse_args([CMD_MONITOR, "12345", "--stats=no"]))

        assert result is not None
        assert result.stats_view is None

    def test_the_pid_survives_them(self) -> None:
        assert self._parse([CMD_MONITOR, "--stats", "off", "12345"]).pid == 12345

    @pytest.mark.parametrize("word", ["1", "true", "yes", "on"])
    def test_their_truthy_opposites_are_still_refused(self, capsys: pytest.CaptureFixture[str], word: str) -> None:
        with pytest.raises(SystemExit) as exit_info:
            self._parse([CMD_MONITOR, "12345", f"--stats={word}"])

        assert exit_info.value.code != 0
        assert StatsView.TOTAL.value in capsys.readouterr().err


class TestRateValidation:
    """A rate that reaches the loop is one gcmon can hold.

    This is the gate for a rate the parser never saw: one passed as an already
    typed value, and a `GCMON_RATE` that failed to parse.
    """

    @pytest.mark.parametrize("rate", [1e-12, 0.0005])
    def test_a_rate_under_the_minimum_is_refused(self, caplog: pytest.LogCaptureFixture, rate: float) -> None:
        """Below a millisecond the spin guard is longer than the interval asked
        for, so no tick can start on time (ADR-0019)."""
        with caplog.at_level(logging.ERROR, logger=PROGRAM_NAME):
            assert get_monitoring_options(_make_args(rate=rate)) is None

        assert "0.001 seconds" in caplog.text

    def test_the_minimum_itself_is_taken(self) -> None:
        """The guard is exactly the interval asked for, which the loop can hold."""
        result = get_monitoring_options(_make_args(rate=0.001))

        assert result is not None
        assert result.rate == 0.001

    def test_an_env_rate_that_is_not_a_rate_stops_the_run(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """`None` is how `get_env_rate` reports one, since the parser applies
        no type to a default. Falling back to 0.1 would poll at a rate nobody
        asked for."""
        monkeypatch.setenv(ENV_RATE, "1e-3")

        with caplog.at_level(logging.ERROR, logger=PROGRAM_NAME):
            assert get_monitoring_options(_make_args(rate=None)) is None

        assert ENV_RATE in caplog.text


class TestRateArgument:
    """`--rate` on the command line, refused by the parser rather than below."""

    @pytest.fixture
    def parser(self, monkeypatch: pytest.MonkeyPatch) -> ArgumentParser:
        monkeypatch.delenv(ENV_RATE, raising=False)
        parser = ArgumentParser()
        add_monitoring_options(parser)
        return parser

    def test_a_plain_decimal_is_taken(self, parser: ArgumentParser) -> None:
        assert parser.parse_args(["--rate", "0.25"]).rate == 0.25

    def test_the_minimum_itself_is_taken(self, parser: ArgumentParser) -> None:
        assert parser.parse_args(["--rate", "0.001"]).rate == 0.001

    @pytest.mark.parametrize("value", ["1e-3", "1E-3", "0", "-0.1", "0.0000000001", "0.0005", "inf"])
    def test_a_value_the_loop_cannot_hold_exits(self, parser: ArgumentParser, value: str) -> None:
        with pytest.raises(SystemExit):
            parser.parse_args(["--rate", value])


class TestTableFormatArgument:
    """`--table-format` on the command line: three spellings, in any case."""

    @pytest.fixture
    def parser(self) -> ArgumentParser:
        parser = ArgumentParser()
        add_monitoring_options(parser)
        return parser

    @pytest.mark.parametrize(
        ("word", "table_format"),
        [
            ("plain", TableFormat.PLAIN),
            ("markdown", TableFormat.MARKDOWN),
            ("md", TableFormat.MARKDOWN),
            ("MD", TableFormat.MARKDOWN),
            ("Plain", TableFormat.PLAIN),
        ],
    )
    def test_each_spelling_selects_its_format(
        self, parser: ArgumentParser, word: str, table_format: TableFormat
    ) -> None:
        assert parser.parse_args(["--table-format", word]).table_format is table_format

    def test_an_unknown_word_is_refused_by_name(
        self, parser: ArgumentParser, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as exit_info:
            parser.parse_args(["--table-format", "html"])

        assert exit_info.value.code != 0
        assert "got 'html'" in capsys.readouterr().err
