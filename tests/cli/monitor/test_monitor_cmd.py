import json
import os
import subprocess
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gcmon.cli.monitor._env import (
    ENV_DURATION,
    ENV_FLUSH_THRESHOLD,
    ENV_FORMAT,
    ENV_OUTPUT,
    ENV_RATE,
    ENV_VERBOSE,
)
from gcmon.model.names import DURATION, PID
from gcmon.support.vocabulary import (
    CMD_MONITOR,
    DEFAULT_JSONL_FILE,
    DEFAULT_TRACE_FILE,
    FORMAT_JSONL,
    FORMAT_PERFETTO,
    FORMAT_STDOUT,
)
from tests.cli.monitor.conftest import MonitorArgsFactory
from tests.helpers import SUBPROCESS_WATCHDOG, assert_valid_perfetto_trace


@pytest.fixture
def mock_monitoring_loop() -> Generator[MagicMock]:
    with patch("gcmon.cli.monitor.monitor_cmd.run_monitoring_loop") as mock:
        yield mock


# =============================================================================
# Unit Tests for cmd_monitor
# =============================================================================


def test_cmd_monitor_returns_the_exit_code_of_the_loop(
    monitor_args: MonitorArgsFactory, mock_monitoring_loop: MagicMock
) -> None:
    from gcmon.cli.monitor import monitor_cmd

    mock_monitoring_loop.return_value = 1

    result = monitor_cmd.cmd_monitor(monitor_args())

    assert result == 1


class TestCmdMonitorFormat:
    @pytest.mark.parametrize(
        "fmt, extra_kwargs",
        [
            (FORMAT_STDOUT, {}),
            (FORMAT_JSONL, {"thread_id": 99, "flush_threshold": 50}),
        ],
    )
    def test_cmd_monitor_format(
        self,
        caplog: pytest.LogCaptureFixture,
        monitor_args: MonitorArgsFactory,
        fmt: str,
        extra_kwargs: dict[str, int],
        mock_monitoring_loop: MagicMock,
    ) -> None:
        from gcmon.cli.monitor import monitor_cmd

        args = monitor_args(format=fmt, output=Path("test.jsonl"), duration=0.05, **extra_kwargs)
        mock_monitoring_loop.return_value = 0

        result = monitor_cmd.cmd_monitor(args)

        assert result == 0
        assert f"Format: {fmt}" in caplog.text


class TestCmdMonitorValidation:
    @pytest.mark.parametrize(
        "override, expected_msg",
        [
            ({PID: -2}, "PID must be positive"),
            ({"rate": 0}, "Rate must be at least 0.001 seconds"),
            ({DURATION: 0}, "Duration must be positive"),
            ({"flush_threshold": 0}, "Flush threshold must be positive"),
        ],
    )
    def test_a_value_out_of_range_fails_and_says_why(
        self,
        caplog: pytest.LogCaptureFixture,
        monitor_args: MonitorArgsFactory,
        override: dict[str, int],
        expected_msg: str,
    ) -> None:
        from gcmon.cli.monitor import monitor_cmd

        result = monitor_cmd.cmd_monitor(monitor_args(**override))

        assert result == 1
        assert expected_msg in caplog.text


def test_cmd_monitor_self_pid(monitor_args: MonitorArgsFactory, mock_monitoring_loop: MagicMock) -> None:
    from gcmon.cli.monitor import monitor_cmd

    mock_monitoring_loop.return_value = 0

    result = monitor_cmd.cmd_monitor(monitor_args(pid=-1, duration=0.05))

    assert result == 0
    factory_fn = mock_monitoring_loop.call_args[1]["factory"]
    process = factory_fn("dummy-address")
    assert process.pid == os.getpid()


def test_cmd_monitor_names_the_control_plane_as_asked(
    monitor_args: MonitorArgsFactory, mock_monitoring_loop: MagicMock
) -> None:
    from gcmon.cli.monitor import monitor_cmd

    monitor_cmd.cmd_monitor(monitor_args(control_name="bench-7"))

    assert mock_monitoring_loop.call_args.kwargs["address"] == "bench-7"


# =============================================================================
# Subprocess Tests - Basic Execution
# =============================================================================


def test_cli_monitor_invocation(run_monitor: Any) -> None:
    assert run_monitor(["-d", "0.1"], timeout=5).returncode == 0


class TestCliBasicRun:
    def test_a_short_duration_is_logged(self, run_monitor_self: Any, tmp_path: Path) -> None:
        result = run_monitor_self(["-o", str(tmp_path / "test.json"), "-d", "0.01", "-v"], timeout=15)

        assert result.returncode == 0
        assert "Duration: 0.01s" in result.stderr

    def test_creates_valid_trace(self, run_monitor_self: Any, tmp_path: Path) -> None:
        output_file = tmp_path / "test_trace.pftrace"

        result = run_monitor_self(["-o", str(output_file), "-d", "0.5", "-r", "0.1"])

        assert result.returncode == 0
        assert output_file.exists()
        assert len(assert_valid_perfetto_trace(output_file)) >= 1

    def test_default_output_file(self, run_monitor_self: Any, tmp_path: Path) -> None:
        assert run_monitor_self(["-d", "0.3"], cwd=tmp_path).returncode == 0
        assert (tmp_path / DEFAULT_TRACE_FILE).exists()
        assert not (tmp_path / "gcmon.json").exists()

    def test_the_rate_flag_is_logged(self, run_monitor_self: Any, tmp_path: Path) -> None:
        result = run_monitor_self(["-o", str(tmp_path / "test_trace.json"), "-d", "0.5", "-r", "0.05", "-v"])

        assert result.returncode == 0
        assert "Rate: 0.05" in result.stderr

    def test_the_run_lasts_at_least_its_duration(self, run_monitor_self: Any, tmp_path: Path) -> None:
        start = time.monotonic()

        result = run_monitor_self(["-o", str(tmp_path / "test_trace.json"), "-d", "0.5", "-r", "0.1", "-v"])

        assert result.returncode == 0
        assert time.monotonic() - start >= 0.5
        assert "Duration: 0.5s" in result.stderr


class TestCliOutput:
    def test_verbose_names_the_pid_and_the_output_path(self, run_monitor: Any, tmp_path: Path) -> None:
        output_file = tmp_path / "test_trace.json"

        result = run_monitor(["-o", str(output_file), "-d", "0.3", "-v"])

        assert result.returncode == 0
        assert "Monitoring PID: 12345" in result.stderr
        assert str(output_file) in result.stderr

    def test_without_verbose_the_pid_is_not_logged(self, run_monitor: Any, tmp_path: Path) -> None:
        result = run_monitor(["-o", str(tmp_path / "test_trace.json"), "-d", "0.3"])

        assert result.returncode == 0
        assert "Monitoring PID" not in result.stderr


class TestCliStdoutFormat:
    def test_every_stdout_line_is_a_record_with_a_pid(self, run_monitor_self: Any, tmp_path: Path) -> None:
        """Against the running gcmon: pid 12345 holds no process, and a run
        that read nothing prints nothing to parse."""
        result = run_monitor_self(["--format", FORMAT_STDOUT, "-d", "0.3"], cwd=tmp_path)

        records: list[dict[str, Any]] = [json.loads(line) for line in result.stdout.splitlines()]

        assert result.returncode == 0
        assert records
        assert [record for record in records if PID not in record] == []

    def test_verbose_names_the_pid_and_the_stdout_format(self, run_monitor: Any, tmp_path: Path) -> None:
        result = run_monitor(["--format", FORMAT_STDOUT, "-d", "0.3", "-v"], cwd=tmp_path)

        assert "Monitoring PID: 12345" in result.stderr
        assert "Format: stdout" in result.stderr

    def test_without_verbose_the_pid_is_not_logged(self, run_monitor: Any, tmp_path: Path) -> None:
        result = run_monitor(["--format", FORMAT_STDOUT, "-d", "0.5"], cwd=tmp_path)

        assert result.returncode == 0
        assert "Monitoring PID" not in result.stderr


class TestCliJsonlFormat:
    def test_the_jsonl_format_is_logged(self, run_monitor: Any, tmp_path: Path) -> None:
        output_file = tmp_path / "test.jsonl"

        result = run_monitor(["--format", FORMAT_JSONL, "-o", str(output_file), "-d", "0.1", "-v"])

        assert "Format: jsonl" in result.stderr

    def test_cli_overrides_env(self, run_monitor_self: Any, tmp_path: Path) -> None:
        output_file = tmp_path / "test.pftrace"
        env = os.environ.copy()
        env[ENV_FORMAT] = FORMAT_JSONL

        run_monitor_self(["--format", FORMAT_PERFETTO, "-o", str(output_file), "-d", "0.3"], env=env)

        assert output_file.exists()
        assert_valid_perfetto_trace(output_file)


class TestTheDroppedFormatsAreRefusedByName:
    """What an operator who scripted the old flag sees.

    Asserting the outcome and not the absence of a symbol: the run has to stop
    at the argument, and the message has to name what is left, or the operator
    is left guessing which word replaced theirs.
    """

    @pytest.mark.parametrize("fmt", ["chrome", "trace", "chrome+perfetto"])
    def test_the_run_stops_at_the_argument(self, run_monitor: Any, tmp_path: Path, fmt: str) -> None:
        result = run_monitor(["--format", fmt, "-d", "0.1"], cwd=tmp_path)

        assert result.returncode == 2
        assert fmt in result.stderr
        for remaining in (FORMAT_PERFETTO, FORMAT_JSONL, FORMAT_STDOUT):
            assert remaining in result.stderr

    @pytest.mark.parametrize("fmt", ["chrome", "trace", "chrome+perfetto"])
    def test_nothing_is_written(self, run_monitor: Any, tmp_path: Path, fmt: str) -> None:
        run_monitor(["--format", fmt, "-d", "0.1"], cwd=tmp_path)

        assert list(tmp_path.iterdir()) == []

    def test_the_environment_stops_the_run_and_names_the_value(self, run_monitor: Any, tmp_path: Path) -> None:
        """The asymmetry ADR-0018 settled for `--stats`. The parser takes a
        string default as given rather than checking it against `choices`, so
        this word reaches the validator, which refuses it instead of logging
        `Format: perfetto` for a run configured as something else."""
        env = os.environ.copy()
        env[ENV_FORMAT] = "chrome"

        result = run_monitor(["-d", "0.1"], cwd=tmp_path, env=env)

        assert result.returncode != 0
        assert ENV_FORMAT in result.stderr
        assert "chrome" in result.stderr
        assert "Format: perfetto" not in result.stderr
        assert list(tmp_path.iterdir()) == []


# =============================================================================
# Environment Variable Tests
# =============================================================================


class TestCliEnvVars:
    """CLI integration with individual environment variables."""

    def test_the_output_variable_names_the_file_written(
        self, monkeypatch: pytest.MonkeyPatch, run_monitor_self: Any, tmp_path: Path
    ) -> None:
        output_file = tmp_path / "env_test_trace.pftrace"
        monkeypatch.setenv(ENV_OUTPUT, str(output_file))

        assert run_monitor_self(["-d", "0.3"]).returncode == 0
        assert output_file.exists()

    def test_output_cli_override(self, monkeypatch: pytest.MonkeyPatch, run_monitor_self: Any, tmp_path: Path) -> None:
        monkeypatch.setenv(ENV_OUTPUT, str(tmp_path / "env_trace.pftrace"))
        cli_file = tmp_path / "cli_trace.pftrace"

        assert run_monitor_self(["-o", str(cli_file), "-d", "0.3"]).returncode == 0
        assert cli_file.exists()
        assert not (tmp_path / "env_trace.pftrace").exists()

    def test_the_rate_variable_is_logged(
        self, monkeypatch: pytest.MonkeyPatch, run_monitor: Any, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(ENV_RATE, "0.05")

        result = run_monitor(["-o", str(tmp_path / "test_trace.json"), "-d", "0.3", "-v"])

        assert "Rate: 0.05" in result.stderr

    def test_rate_cli_override(self, monkeypatch: pytest.MonkeyPatch, run_monitor: Any, tmp_path: Path) -> None:
        monkeypatch.setenv(ENV_RATE, "0.05")

        result = run_monitor(["-o", str(tmp_path / "test_trace.json"), "-r", "0.2", "-d", "0.3", "-v"])

        assert "Rate: 0.2" in result.stderr

    def test_the_duration_variable_is_logged(
        self, monkeypatch: pytest.MonkeyPatch, run_monitor: Any, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(ENV_DURATION, "0.5")

        result = run_monitor(["-o", str(tmp_path / "test_trace.json"), "-v"])

        assert "Duration: 0.5" in result.stderr

    def test_duration_cli_override(self, monkeypatch: pytest.MonkeyPatch, run_monitor: Any, tmp_path: Path) -> None:
        monkeypatch.setenv(ENV_DURATION, "0.5")

        result = run_monitor(["-o", str(tmp_path / "test_trace.json"), "-d", "0.3", "-v"])

        assert "Duration: 0.3" in result.stderr

    def test_the_format_variable_is_logged(
        self, monkeypatch: pytest.MonkeyPatch, run_monitor: Any, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(ENV_FORMAT, FORMAT_STDOUT)

        result = run_monitor(["-d", "0.3", "-v"])

        assert "Format: stdout" in result.stderr

    def test_format_cli_override(self, monkeypatch: pytest.MonkeyPatch, run_monitor: Any, tmp_path: Path) -> None:
        monkeypatch.setenv(ENV_FORMAT, FORMAT_STDOUT)

        result = run_monitor(["--format", FORMAT_PERFETTO, "-d", "0.3", "-v"])

        assert "Format: perfetto" in result.stderr

    @pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
    def test_verbose_truthy_values(self, monkeypatch: pytest.MonkeyPatch, run_monitor: Any, value: str) -> None:
        monkeypatch.setenv(ENV_VERBOSE, value)

        result = run_monitor(["-d", "0.3"])

        assert "Monitoring PID: 12345" in result.stderr

    def test_verbose_cli_override(self, monkeypatch: pytest.MonkeyPatch, run_monitor: Any) -> None:
        monkeypatch.setenv(ENV_VERBOSE, "0")

        result = run_monitor(["-d", "0.3", "-v"])

        assert "Monitoring PID: 12345" in result.stderr

    def test_several_variables_apply_together(
        self, monkeypatch: pytest.MonkeyPatch, run_monitor_self: Any, tmp_path: Path
    ) -> None:
        output_file = tmp_path / "multi_env_test.pftrace"
        monkeypatch.setenv(ENV_OUTPUT, str(output_file))
        monkeypatch.setenv(ENV_RATE, "0.05")
        monkeypatch.setenv(ENV_DURATION, "0.4")
        monkeypatch.setenv(ENV_VERBOSE, "1")
        monkeypatch.setenv(ENV_FORMAT, FORMAT_PERFETTO)

        result = run_monitor_self([])

        assert output_file.exists()
        assert "Rate: 0.05" in result.stderr
        assert "Duration: 0.4" in result.stderr

    def test_a_flush_threshold_of_zero_in_the_variable_fails_the_run(
        self, monkeypatch: pytest.MonkeyPatch, run_monitor: Any, tmp_path: Path
    ) -> None:
        """A run prints no threshold, so the variable is given the one value
        a run refuses."""
        output_file = tmp_path / "test.jsonl"
        monkeypatch.setenv(ENV_FLUSH_THRESHOLD, "0")

        result = run_monitor(["--format", FORMAT_JSONL, "-o", str(output_file), "-d", "0.1"], timeout=30)

        assert result.returncode == 1
        assert "Flush threshold must be positive, got 0" in result.stderr

    def test_flush_threshold_cli_override(
        self, monkeypatch: pytest.MonkeyPatch, run_monitor: Any, tmp_path: Path
    ) -> None:
        output_file = tmp_path / "test.jsonl"
        monkeypatch.setenv(ENV_FLUSH_THRESHOLD, "0")

        result = run_monitor(
            ["--format", FORMAT_JSONL, "-o", str(output_file), "--flush-threshold", "200", "-d", "0.1"], timeout=30
        )

        assert result.returncode == 0

    def test_env_output_default_format_jsonl(
        self, monkeypatch: pytest.MonkeyPatch, run_monitor_self: Any, tmp_path: Path
    ) -> None:
        """Against the running gcmon, since a run that read nothing writes no
        file to find."""
        monkeypatch.setenv(ENV_FORMAT, FORMAT_JSONL)

        result = run_monitor_self(["-d", "0.3"], cwd=tmp_path, timeout=30)

        assert result.returncode == 0
        assert (tmp_path / DEFAULT_JSONL_FILE).exists()
        assert not (tmp_path / DEFAULT_TRACE_FILE).exists()


class TestCliEnvHelp:
    def test_monitor_help_shows_env_vars(self, gcmon_cmd: list[str]) -> None:
        result = subprocess.run(
            [*gcmon_cmd, CMD_MONITOR, "--help"],
            capture_output=True,
            text=True,
            check=True,
            timeout=SUBPROCESS_WATCHDOG,
        )

        for var in (ENV_OUTPUT, ENV_RATE, ENV_DURATION, ENV_VERBOSE, ENV_FORMAT):
            assert var in result.stdout
