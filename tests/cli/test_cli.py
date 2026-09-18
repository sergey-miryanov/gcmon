import importlib.metadata
import subprocess
import sys
import types
from collections.abc import Generator
from pathlib import Path

import msgspec
import pytest

from gcmon.model.names import PID
from gcmon.support.vocabulary import CMD_COMBINE, CMD_MONITOR, CMD_RUN, PROGRAM_NAME


@pytest.fixture
def gcmon_cli() -> list[str]:
    return [sys.executable, "-m", PROGRAM_NAME]


@pytest.fixture
def cli_module() -> types.ModuleType:
    from gcmon.cli import main

    return main


# =============================================================================
# _setup_logging Tests
# =============================================================================


class TestSetupLogging:
    @pytest.fixture(autouse=True)
    def reset_logging(self) -> Generator[None]:
        """`_setup_logging` touches the gcmon logger alone, so that is all
        this empties, and it hands back what it found."""
        import logging

        logger = logging.getLogger(PROGRAM_NAME)
        handlers, level = logger.handlers[:], logger.level
        logger.handlers.clear()
        yield
        logger.handlers[:] = handlers
        logger.setLevel(level)

    @pytest.mark.parametrize(
        "verbose_count, expected_level",
        [
            (1, "INFO"),
            (0, "WARNING"),
            (2, "DEBUG"),
        ],
    )
    def test_setup_logging(self, cli_module: types.ModuleType, verbose_count: int, expected_level: str) -> None:
        import logging

        cli_module._setup_logging(verbose_count=verbose_count)
        logger = logging.getLogger(PROGRAM_NAME)
        assert logger.level == getattr(logging, expected_level)

    def test_a_second_call_relevels_the_handler_it_already_added(self, cli_module: types.ModuleType) -> None:
        """One handler per process. A second call carrying ``-vv`` re-levels
        the handler that is attached rather than adding one beside it, which
        would print every record twice."""
        import logging

        cli_module._setup_logging(verbose_count=0)

        cli_module._setup_logging(verbose_count=2)

        handlers = logging.getLogger(PROGRAM_NAME).handlers
        assert len(handlers) == 1
        assert handlers[0].level == logging.DEBUG


# =============================================================================
# main() Tests - Command Routing
# =============================================================================


def test_main_combine_command(tmp_path: Path) -> None:
    from gcmon.cli import main as cli
    from tests.helpers import create_jsonl_record

    input_file = tmp_path / "input.jsonl"
    input_file.write_bytes(msgspec.json.encode(create_jsonl_record()) + b"\n")

    assert cli.main([CMD_COMBINE, str(input_file), "-o", str(tmp_path / "output.pftrace")]) == 0


def test_main_no_subcommand_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    from gcmon.cli import main as cli

    with pytest.raises(SystemExit) as excinfo:
        cli.main([])

    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert "the following arguments are required: command" in captured.err
    for subcommand in (CMD_MONITOR, CMD_COMBINE, CMD_RUN):
        assert subcommand in captured.err


def test_main_invalid_subcommand_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    from gcmon.cli import main as cli

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["12345"])

    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert "invalid choice: '12345'" in captured.err


def test_main_version_exits_0(capsys: pytest.CaptureFixture[str]) -> None:
    from gcmon.cli import main as cli

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--version"])

    assert excinfo.value.code == 0


@pytest.mark.parametrize(
    "handler, argv",
    [
        ("gcmon.cli.monitor.monitor_cmd.cmd_monitor", [CMD_MONITOR, "12345"]),
        ("gcmon.cli.monitor.run_cmd.cmd_run", [CMD_RUN, "-m", "timeit"]),
        ("gcmon.cli.analyze.convert_cmd.cmd_combine", [CMD_COMBINE, "in.jsonl", "-o", "out.pftrace"]),
    ],
)
def test_main_subcommands_dispatch(monkeypatch: pytest.MonkeyPatch, handler: str, argv: list[str]) -> None:
    from gcmon.cli import main as cli

    calls: list[object] = []

    def mock_handler(args: object) -> int:
        calls.append(args)
        return 0

    monkeypatch.setattr(handler, mock_handler)

    assert cli.main(argv) == 0

    assert len(calls) == 1


# =============================================================================
# CLI Help Tests
# =============================================================================


class TestCliHelp:
    @pytest.mark.parametrize(
        "subcommand, expected_texts",
        [
            (
                "",
                [
                    "Monitor Python's garbage collector",
                    CMD_MONITOR,
                    CMD_COMBINE,
                    CMD_RUN,
                ],
            ),
            (
                CMD_MONITOR,
                [
                    PID,
                    "--output",
                    "--rate",
                    "--duration",
                    "--verbose",
                    "--stats",
                    "--control-name",
                    "--rss",
                    "--rss-interval",
                ],
            ),
            (CMD_COMBINE, ["Combine multiple JSONL captures", "inputs", "--output"]),
            (
                CMD_RUN,
                [
                    "Run a Python script or module",
                    "--module",
                    "--script",
                    "--stats",
                    "--control-name",
                    "--rss",
                    "--rss-interval",
                ],
            ),
        ],
    )
    def test_help_subcommand(self, gcmon_cli: list[str], subcommand: str, expected_texts: list[str]) -> None:
        cmd = gcmon_cli + ([subcommand] if subcommand else []) + ["--help"]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        for text in expected_texts:
            assert text in result.stdout

    def test_top_level_no_output_flag(self, gcmon_cli: list[str]) -> None:
        result = subprocess.run([*gcmon_cli, "--help"], capture_output=True, text=True, check=True)
        assert "--output" not in result.stdout


class TestCliVersion:
    def test_version_flag(self, gcmon_cli: list[str]) -> None:
        result = subprocess.run([*gcmon_cli, "--version"], capture_output=True, text=True)
        assert result.returncode == 0
        assert result.stdout.strip() == importlib.metadata.version(PROGRAM_NAME)

    def test_package_attribute_matches_cli(self, gcmon_cli: list[str]) -> None:
        import gcmon

        result = subprocess.run([*gcmon_cli, "--version"], capture_output=True, text=True, check=True)
        assert gcmon.__version__ == result.stdout.strip()

    def test_importing_gcmon_does_not_resolve_the_version(self) -> None:
        # Reading the metadata stats every sys.path entry (~35 ms here) and only `--version`
        # needs it. A fresh interpreter, so an earlier test cannot mask a regression by having
        # touched the attribute first.
        code = "import gcmon; print('__version__' in vars(gcmon))"
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
        assert result.stdout.strip() == "False"

    def test_no_fallback_under_a_normal_install(self) -> None:
        import gcmon

        assert gcmon.__version__ != "0.0.0+unknown"

    def test_fallback_when_gcmon_is_not_installed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import importlib.metadata

        import gcmon

        def not_installed(distribution_name: str) -> str:
            raise importlib.metadata.PackageNotFoundError(distribution_name)

        monkeypatch.setattr(importlib.metadata, "version", not_installed)
        assert gcmon.__version__ == "0.0.0+unknown"


class TestCliMonitor:
    def test_missing_pid(self, gcmon_cli: list[str]) -> None:
        result = subprocess.run([*gcmon_cli, CMD_MONITOR], capture_output=True, text=True)
        assert result.returncode != 0
        assert "the following arguments are required: pid" in result.stderr

    def test_explicit_command(self, gcmon_cli: list[str]) -> None:
        result = subprocess.run(
            [*gcmon_cli, CMD_MONITOR, "12345", "-d", "0.1"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode == 0
