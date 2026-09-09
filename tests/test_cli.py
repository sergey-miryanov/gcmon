import importlib.metadata
import subprocess
import sys
import types
from pathlib import Path

import msgspec
import pytest


@pytest.fixture
def gcmon_cli() -> list[str]:
    return [sys.executable, "-m", "gcmon"]


@pytest.fixture
def cli_module() -> types.ModuleType:
    from gcmon.cli import main

    return main


# =============================================================================
# _setup_logging Tests
# =============================================================================


class TestSetupLogging:
    @pytest.fixture(autouse=True)
    def reset_logging(self) -> None:
        import logging

        for handler in logging.root.handlers[:]:
            logging.root.removeHandler(handler)
        logging.getLogger("gcmon").handlers.clear()

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
        logger = logging.getLogger("gcmon")
        assert logger.level == getattr(logging, expected_level)


# =============================================================================
# main() Tests - Command Routing
# =============================================================================


def test_main_combine_command(tmp_path: Path) -> None:
    from gcmon.cli import main as cli
    from tests.helpers import create_jsonl_record

    input_file = tmp_path / "input.jsonl"
    input_file.write_bytes(msgspec.json.encode(create_jsonl_record()) + b"\n")

    assert cli.main(["combine", str(input_file), "-o", str(tmp_path / "output.pftrace")]) == 0


def test_main_no_subcommand_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    from gcmon.cli import main as cli

    with pytest.raises(SystemExit) as excinfo:
        cli.main([])

    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert "the following arguments are required: command" in captured.err
    for subcommand in ("monitor", "combine", "run"):
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


def test_main_subcommands_dispatch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from gcmon.cli import main as cli

    calls: list[str] = []

    def mock_monitor(args: object) -> int:
        calls.append("monitor")
        return 0

    def mock_run(args: object) -> int:
        calls.append("run")
        return 0

    def mock_combine(args: object) -> int:
        calls.append("combine")
        return 0

    monkeypatch.setattr("gcmon.cli.commands.monitor_cmd.cmd_monitor", mock_monitor)
    assert cli.main(["monitor", "12345"]) == 0
    assert calls == ["monitor"]

    monkeypatch.setattr("gcmon.cli.commands.run_cmd.cmd_run", mock_run)
    assert cli.main(["run", "-m", "timeit"]) == 0
    assert calls == ["monitor", "run"]

    monkeypatch.setattr("gcmon.cli.analyze.convert_cmd.cmd_combine", mock_combine)
    assert cli.main(["combine", str(tmp_path / "in.jsonl"), "-o", str(tmp_path / "out.pftrace")]) == 0
    assert calls == ["monitor", "run", "combine"]


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
                    "monitor",
                    "combine",
                    "run",
                ],
            ),
            (
                "monitor",
                [
                    "pid",
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
            ("combine", ["Combine multiple JSONL captures", "inputs", "--output"]),
            (
                "run",
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
        assert result.stdout.strip() == importlib.metadata.version("gcmon")

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
        result = subprocess.run([*gcmon_cli, "monitor"], capture_output=True, text=True)
        assert result.returncode != 0
        assert "the following arguments are required: pid" in result.stderr

    def test_explicit_command(self, gcmon_cli: list[str]) -> None:
        result = subprocess.run(
            [*gcmon_cli, "monitor", "12345", "-d", "0.1"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode == 0
