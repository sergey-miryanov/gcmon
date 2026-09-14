import types
from pathlib import Path

import pytest

from tests.helpers import DefaultsValue


class TestEnvVarDefaults:
    """Parameterized tests for default env var values."""

    @pytest.mark.parametrize(
        "env_var, getter_suffix, default",
        [
            ("ENV_OUTPUT", "output", Path("gcmon.pftrace")),
            ("ENV_RATE", "rate", 0.1),
            ("ENV_DURATION", "duration", None),
            ("ENV_THREAD_ID", "thread_id", 0),
            ("ENV_FLUSH_THRESHOLD", "flush_threshold", 100),
            ("ENV_SERVER_HOST", "server_host", "localhost"),
            ("ENV_SERVER_PORT", "server_port", 9999),
            ("ENV_RSS", "rss", False),
            ("ENV_RSS_INTERVAL", "rss_interval", 1.0),
        ],
    )
    def test_default(
        self,
        monkeypatch: pytest.MonkeyPatch,
        env_module: types.ModuleType,
        env_var: str,
        getter_suffix: str,
        default: DefaultsValue,
    ) -> None:
        getter = getattr(env_module, f"get_env_{getter_suffix}")
        actual_env_name = getattr(env_module, env_var)
        monkeypatch.delenv(actual_env_name, raising=False)
        assert getter() == default


class TestEnvVarCustom:
    """Parameterized tests for custom env var values."""

    @pytest.mark.parametrize(
        "env_var, getter_suffix, custom_val, expected",
        [
            ("ENV_OUTPUT", "output", "custom.json", Path("custom.json")),
            ("ENV_RATE", "rate", "0.05", 0.05),
            ("ENV_DURATION", "duration", "30.0", 30.0),
            ("ENV_THREAD_ID", "thread_id", "9999", 9999),
            ("ENV_FLUSH_THRESHOLD", "flush_threshold", "50", 50),
            ("ENV_SERVER_HOST", "server_host", "127.0.0.1", "127.0.0.1"),
            ("ENV_SERVER_PORT", "server_port", "8888", 8888),
            ("ENV_RSS", "rss", "true", True),
            ("ENV_RSS_INTERVAL", "rss_interval", "2.5", 2.5),
        ],
    )
    def test_custom(
        self,
        monkeypatch: pytest.MonkeyPatch,
        env_module: types.ModuleType,
        env_var: str,
        getter_suffix: str,
        custom_val: str,
        expected: DefaultsValue,
    ) -> None:
        getter = getattr(env_module, f"get_env_{getter_suffix}")
        actual_env_name = getattr(env_module, env_var)
        monkeypatch.setenv(actual_env_name, custom_val)
        assert getter() == expected


class TestEnvVarInvalidValues:
    """Parameterized tests for invalid env var values falling back to defaults."""

    @pytest.mark.parametrize(
        "env_var, getter_suffix, default",
        [
            ("ENV_DURATION", "duration", None),
            ("ENV_THREAD_ID", "thread_id", 0),
            ("ENV_FLUSH_THRESHOLD", "flush_threshold", 100),
            ("ENV_SERVER_PORT", "server_port", 9999),
            ("ENV_RSS_INTERVAL", "rss_interval", 1.0),
        ],
    )
    def test_invalid_value_returns_default(
        self,
        monkeypatch: pytest.MonkeyPatch,
        env_module: types.ModuleType,
        env_var: str,
        getter_suffix: str,
        default: DefaultsValue,
    ) -> None:
        getter = getattr(env_module, f"get_env_{getter_suffix}")
        actual_env_name = getattr(env_module, env_var)
        monkeypatch.setenv(actual_env_name, "not-a-number")
        assert getter() == default


class TestEnvRate:
    """GCMON_RATE takes what `--rate` takes, and refuses the rest outright.

    The other getters fall back to their default on a value they cannot read.
    This one cannot: a rate nobody asked for is not a safe thing to poll at,
    so `None` says so and the run stops.
    """

    def test_a_plain_decimal_is_taken(self, monkeypatch: pytest.MonkeyPatch, env_module: types.ModuleType) -> None:
        monkeypatch.setenv(env_module.ENV_RATE, "0.05")
        assert env_module.get_env_rate() == 0.05

    def test_the_minimum_itself_is_taken(self, monkeypatch: pytest.MonkeyPatch, env_module: types.ModuleType) -> None:
        monkeypatch.setenv(env_module.ENV_RATE, "0.001")
        assert env_module.get_env_rate() == 0.001

    @pytest.mark.parametrize(
        "value", ["not-a-number", "1e-3", "1E-3", "0", "-0.1", "0.0000000001", "0.0005", "inf", "nan"]
    )
    def test_a_value_that_is_not_a_rate_answers_none(
        self, monkeypatch: pytest.MonkeyPatch, env_module: types.ModuleType, value: str
    ) -> None:
        """`1e-3` is a rate anywhere else. Here the spelling goes, because
        `1e-12` reads the same and is not one."""
        monkeypatch.setenv(env_module.ENV_RATE, value)
        assert env_module.get_env_rate() is None


class TestEnvVerbose:
    """Tests for GCMON_VERBOSE parsing."""

    def test_default(self, monkeypatch: pytest.MonkeyPatch, env_module: types.ModuleType) -> None:
        monkeypatch.delenv(env_module.ENV_VERBOSE, raising=False)
        assert env_module.get_env_verbose() == 0

    def test_numeric(self, monkeypatch: pytest.MonkeyPatch, env_module: types.ModuleType) -> None:
        monkeypatch.setenv(env_module.ENV_VERBOSE, "2")
        assert env_module.get_env_verbose() == 2

    @pytest.mark.parametrize("value", ["true", "yes", "on", "1"])
    def test_truthy_values(self, monkeypatch: pytest.MonkeyPatch, env_module: types.ModuleType, value: str) -> None:
        monkeypatch.setenv(env_module.ENV_VERBOSE, value)
        assert env_module.get_env_verbose() == 1


class TestEnvFormat:
    """Tests for GCMON_FORMAT parsing."""

    def test_default(self, monkeypatch: pytest.MonkeyPatch, env_module: types.ModuleType) -> None:
        monkeypatch.delenv(env_module.ENV_FORMAT, raising=False)
        assert env_module.get_env_format() == "perfetto"

    @pytest.mark.parametrize("value, expected", [("stdout", "stdout"), ("jsonl", "jsonl")])
    def test_valid_values(
        self, monkeypatch: pytest.MonkeyPatch, env_module: types.ModuleType, value: str, expected: str
    ) -> None:
        monkeypatch.setenv(env_module.ENV_FORMAT, value)
        assert env_module.get_env_format() == expected

    def test_an_unknown_word_is_handed_on_as_written(
        self, monkeypatch: pytest.MonkeyPatch, env_module: types.ModuleType
    ) -> None:
        """Not substituted here. `get_monitoring_options` refuses it once
        logging is configured, so the operator is told rather than handed a
        format they did not ask for."""
        monkeypatch.setenv(env_module.ENV_FORMAT, "chrome")
        assert env_module.get_env_format() == "chrome"


class TestEnvOutputSpecialCases:
    """Tests for GCMON_OUTPUT special behavior."""

    def test_default_format_jsonl(self, monkeypatch: pytest.MonkeyPatch, env_module: types.ModuleType) -> None:
        monkeypatch.setenv(env_module.ENV_FORMAT, "jsonl")
        monkeypatch.delenv(env_module.ENV_OUTPUT, raising=False)
        assert env_module.get_env_output() == Path("gcmon.jsonl")

    @pytest.mark.parametrize("value", [" jsonl ", "JSONL", "\tjsonl\n"])
    def test_the_default_name_reads_the_variable_the_way_the_format_does(
        self, monkeypatch: pytest.MonkeyPatch, env_module: types.ModuleType, value: str
    ) -> None:
        """One spelling, two answers, is how a run ends up writing JSONL into a
        file called `gcmon.pftrace`."""
        monkeypatch.setenv(env_module.ENV_FORMAT, value)
        monkeypatch.delenv(env_module.ENV_OUTPUT, raising=False)

        assert env_module.get_env_format() == "jsonl"
        assert env_module.get_env_output() == Path("gcmon.jsonl")


class TestEnvStats:
    """GCMON_STATS names a view, and this layer only reads it.

    `get_monitoring_options` refuses an unknown one.
    """

    def test_default(self, monkeypatch: pytest.MonkeyPatch, env_module: types.ModuleType) -> None:
        monkeypatch.delenv(env_module.ENV_STATS, raising=False)
        assert env_module.get_env_stats() is None

    @pytest.mark.parametrize("value", ["total", "full"])
    def test_each_view_reads_back_as_typed(
        self, monkeypatch: pytest.MonkeyPatch, env_module: types.ModuleType, value: str
    ) -> None:
        monkeypatch.setenv(env_module.ENV_STATS, value)
        assert env_module.get_env_stats() == value

    @pytest.mark.parametrize("value", ["1", "true", "nope"])
    def test_a_value_it_cannot_use_is_still_handed_on(
        self, monkeypatch: pytest.MonkeyPatch, env_module: types.ModuleType, value: str
    ) -> None:
        """Swallowing it here would leave the run to discover it at the end."""
        monkeypatch.setenv(env_module.ENV_STATS, value)
        assert env_module.get_env_stats() == value

    @pytest.mark.parametrize("value", ["", " ", "\t\n"])
    def test_a_blank_value_reads_as_unset(
        self, monkeypatch: pytest.MonkeyPatch, env_module: types.ModuleType, value: str
    ) -> None:
        """The one unusable value that does not stop the run."""
        monkeypatch.setenv(env_module.ENV_STATS, value)
        assert env_module.get_env_stats() is None


class TestEnvTableFormat:
    """Tests for GCMON_TABLE_FORMAT parsing."""

    def test_default(self, monkeypatch: pytest.MonkeyPatch, env_module: types.ModuleType) -> None:
        monkeypatch.delenv(env_module.ENV_TABLE_FORMAT, raising=False)
        assert env_module.get_env_table_format() == env_module.TableFormat.PLAIN

    @pytest.mark.parametrize("value", ["md", "markdown", "MD", "Markdown"])
    def test_markdown_values(self, monkeypatch: pytest.MonkeyPatch, env_module: types.ModuleType, value: str) -> None:
        monkeypatch.setenv(env_module.ENV_TABLE_FORMAT, value)
        assert env_module.get_env_table_format() == env_module.TableFormat.MARKDOWN

    def test_invalid_value_returns_default(self, monkeypatch: pytest.MonkeyPatch, env_module: types.ModuleType) -> None:
        monkeypatch.setenv(env_module.ENV_TABLE_FORMAT, "html")
        assert env_module.get_env_table_format() == env_module.TableFormat.PLAIN


class TestEnvVarEmpty:
    """Tests for env vars set to empty string (different from unset)."""

    @pytest.mark.parametrize(
        "env_var, getter_suffix, default",
        [
            ("ENV_OUTPUT", "output", Path("gcmon.pftrace")),
            ("ENV_RATE", "rate", 0.1),
            ("ENV_DURATION", "duration", None),
            ("ENV_THREAD_ID", "thread_id", 0),
            ("ENV_FLUSH_THRESHOLD", "flush_threshold", 100),
            ("ENV_SERVER_HOST", "server_host", "localhost"),
            ("ENV_SERVER_PORT", "server_port", 9999),
            ("ENV_VERBOSE", "verbose", 0),
            ("ENV_FORMAT", "format", "perfetto"),
            ("ENV_STATS", "stats", None),
            ("ENV_TABLE_FORMAT", "table_format", None),
            ("ENV_CONTROL_NAME", "control_name", None),
            ("ENV_RSS", "rss", False),
            ("ENV_RSS_INTERVAL", "rss_interval", 1.0),
        ],
    )
    def test_empty_string_returns_default(
        self,
        monkeypatch: pytest.MonkeyPatch,
        env_module: types.ModuleType,
        env_var: str,
        getter_suffix: str,
        default: DefaultsValue,
    ) -> None:
        getter = getattr(env_module, f"get_env_{getter_suffix}")
        actual_env_name = getattr(env_module, env_var)
        monkeypatch.setenv(actual_env_name, "")
        result = getter()
        if getter_suffix == "table_format":
            assert result == env_module.TableFormat.PLAIN
        else:
            assert result == default
