"""Integration tests for the run command."""

import os
import subprocess
import sys
import tempfile
import threading
import time
from argparse import Namespace
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gcmon.analysis.jsonl_io import read_jsonl
from tests.helpers import assert_valid_perfetto_trace

# =============================================================================
# Unit Tests for cmd_run
# =============================================================================


def assert_jsonl_format(output_file: Path) -> None:
    assert output_file.exists()
    assert read_jsonl(output_file)


def assert_stdout_format(output: str) -> None:
    assert "pid" in output
    assert "gen" in output
    assert "ts_start" in output
    assert "ts_stop" in output
    assert "heap_size" in output
    assert "collections" in output
    assert "collected" in output
    assert "uncollectable" in output
    assert "candidates" in output
    assert "duration" in output


def get_long_running_script(*args: str) -> str:
    script: str = """
import gc
import sys
import time
gc.collect()
n = 1000
d: dict[tuple[int, int], int] = {}
t1 = time.monotonic()
for i in range(n):
    for j in range(n):
        d[(i, j)] = i * n + j
t2 = time.monotonic()
gc.collect()
print('')
print(f'ts={(t2-t1)/1_000.0}')
sys.stdout.flush()

"""
    return script + "\n".join([str(s) for s in args]) + "\nsys.stdout.flush()\nsys.exit(0)"


def _print_output(tool: str, pid: int, result: subprocess.CompletedProcess[str] | subprocess.TimeoutExpired) -> None:
    print(f"--- {tool} PID {pid} ---")
    out = getattr(result, "stdout", None) or getattr(result, "output", "")
    err = getattr(result, "stderr", None) or ""
    if out:
        print("STDOUT")
        print(out)
    if err:
        print("STDERR")
        print(err)
    print(f"--- end {tool} ---")


@contextmanager
def print_on_failure(result: subprocess.CompletedProcess[str]) -> Iterator[None]:
    try:
        yield
    except AssertionError:
        _print_output("test", -1, result)
        raise


def _check_process_alive(pid: int) -> bool:
    try:
        result = subprocess.run(
            ["kill", "-0", str(pid)],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode != 0:
            print(f"PID {pid}: process not found (exit code {result.returncode})")
            return False
    except subprocess.TimeoutExpired as exc:
        _print_output("kill", pid, exc)
    except Exception as e:
        print(f"PID {pid}: kill -0 failed ({e}), assuming alive")

    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "state="],
            capture_output=True,
            text=True,
            timeout=3,
        )
        state = result.stdout.strip()
        if state == "Z":
            print(f"PID {pid}: zombie state, skipping diagnostic tools")
            return False
        print(f"PID {pid}: state={state}")
    except subprocess.TimeoutExpired as exc:
        _print_output("ps", pid, exc)
    except Exception as e:
        print(f"PID {pid}: ps failed ({e})")

    return True


def _sample_process(pid: int) -> None:
    if sys.platform == "darwin":
        if not _check_process_alive(pid):
            return

        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                spindump_path = os.path.join(tmpdir, "spindump.txt")
                subprocess.run(
                    ["sudo", "spindump", str(pid), "1", "-file", spindump_path],
                    timeout=15,
                )
                if os.path.exists(spindump_path):
                    with open(spindump_path) as f:
                        print(f"--- spindump PID {pid} ---")
                        print(f.read())
                        print("--- end spindump ---")
        except subprocess.TimeoutExpired as exc:
            _print_output("spindump", pid, exc)
        except Exception as e:
            print(f"spindump PID {pid} failed: {e}")


def _popen_with_timeout(cmd: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    buf: list[str] = []

    stdout = proc.stdout
    assert stdout is not None

    def reader() -> None:
        for line in iter(stdout.readline, ""):
            buf.append(line)

    reader_thread = threading.Thread(target=reader, daemon=True)
    reader_thread.start()

    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _sample_process(proc.pid)
            proc.kill()
            proc.wait()
            reader_thread.join(timeout=5)
            raise subprocess.TimeoutExpired(cmd, timeout, output="".join(buf))
        try:
            ret = proc.wait(timeout=min(0.1, remaining))
        except subprocess.TimeoutExpired:
            continue
        reader_thread.join(timeout=5)
        return subprocess.CompletedProcess(args=cmd, returncode=ret, stdout="".join(buf), stderr="")


def run_script(
    script_file: Path, *script_args: str, gc_args: list[str] | None = None
) -> subprocess.CompletedProcess[str]:
    gc_opts = gc_args or []
    cmd = [
        sys.executable,
        "-u",
        "-m",
        "gcmon",
        "run",
        *gc_opts,
        "-s",
        str(script_file.as_posix()),
        *script_args,
    ]
    try:
        return _popen_with_timeout(cmd, timeout=30)
    except subprocess.TimeoutExpired as exc:
        _print_output("run_script", -1, exc)
        raise


def run_module(
    module_name: str, *script_args: str, gc_args: list[str] | None = None
) -> subprocess.CompletedProcess[str]:
    gc_opts = gc_args or []
    cmd = [
        sys.executable,
        "-u",
        "-m",
        "gcmon",
        "run",
        *gc_opts,
        "-m",
        str(module_name),
        *script_args,
    ]
    try:
        return _popen_with_timeout(cmd, timeout=30)
    except subprocess.TimeoutExpired as exc:
        _print_output("run_module", -1, exc)
        raise


class TestCmdRunUnit:
    """Unit tests for cmd_run function."""

    @pytest.fixture
    def mock_monitoring_loop_and_runner(self) -> Generator[tuple[MagicMock, MagicMock, MagicMock]]:

        with (
            patch("gcmon.cli.monitor.run_cmd.run_monitoring_loop", return_value=0) as mock_loop,
            patch("gcmon.cli.monitor.run_cmd.ChildProcessRunner") as mock_runner_cls,
        ):
            mock_runner = MagicMock()
            mock_runner.returncode = 0
            mock_runner_cls.return_value = mock_runner
            mock_runner.start.return_value = MagicMock(pid=999)
            yield mock_loop, mock_runner_cls, mock_runner

    def _make_run_args(self, **overrides: object) -> Namespace:
        defaults: dict[str, str | Path | float | int | bool | list[str] | None] = {
            "module_name": None,
            "script": None,
            "script_args": [],
            "output": Path("test.pftrace"),
            "rate": 0.1,
            "duration": 0.05,
            "verbose": 1,
            "format": "perfetto",
            "flush_threshold": 100,
            "stats": None,
            "table_format": None,
            "control_name": None,
            "rss": False,
            "rss_interval": 1.0,
        }
        return Namespace(**{**defaults, **overrides})

    def test_cmd_run_both_targets(self, caplog: pytest.LogCaptureFixture) -> None:
        """Test cmd_run rejects both -m and -s specified."""
        from gcmon.cli.monitor import run_cmd

        args = self._make_run_args(module_name="timeit", script="script.py")

        result = run_cmd.cmd_run(args)

        assert result == 1
        assert "Cannot specify both" in caplog.text

    def test_cmd_run_no_target(self, caplog: pytest.LogCaptureFixture) -> None:
        """Test cmd_run rejects neither -m nor -s specified."""
        from gcmon.cli.monitor import run_cmd

        args = self._make_run_args()

        result = run_cmd.cmd_run(args)

        assert result == 1
        assert "Must specify either" in caplog.text

    def test_cmd_run_module_mode(self, mock_monitoring_loop_and_runner: tuple[MagicMock, MagicMock, MagicMock]) -> None:
        """Test cmd_run passes factory with correct params for module mode."""
        mock_loop, mock_runner_cls, mock_runner = mock_monitoring_loop_and_runner
        from gcmon.cli.monitor import run_cmd

        args = self._make_run_args(module_name="timeit", script_args=["-n", "1"])

        run_cmd.cmd_run(args)

        factory_fn = mock_loop.call_args[1]["factory"]
        runner = factory_fn("test-addr")

        mock_runner_cls.assert_called_once_with(
            target="timeit",
            is_module=True,
            passthrough_args=["-n", "1"],
            control_address="test-addr",
        )
        assert runner is mock_runner

    def test_cmd_run_script_mode(self, mock_monitoring_loop_and_runner: tuple[MagicMock, MagicMock, MagicMock]) -> None:
        """Test cmd_run passes factory with correct params for script mode."""
        mock_loop, mock_runner_cls, mock_runner = mock_monitoring_loop_and_runner
        from gcmon.cli.monitor import run_cmd

        args = self._make_run_args(script="myscript.py", script_args=["arg1"])

        run_cmd.cmd_run(args)

        factory_fn = mock_loop.call_args[1]["factory"]
        runner = factory_fn("test-addr")

        mock_runner_cls.assert_called_once_with(
            target="myscript.py",
            is_module=False,
            passthrough_args=["arg1"],
            control_address="test-addr",
        )
        assert runner is mock_runner

    def test_cmd_run_subprocess_returncode(self) -> None:
        """Test non-zero subprocess returncode is propagated from run_monitoring_loop."""
        from gcmon.cli.monitor import run_cmd

        args = self._make_run_args(module_name="timeit")

        with patch("gcmon.cli.monitor.run_cmd.run_monitoring_loop", return_value=42):
            result = run_cmd.cmd_run(args)
            assert result == 42

    def test_cmd_run_returns_monitoring_loop_failure(self) -> None:
        """Test monitoring loop failure (1) is propagated."""
        from gcmon.cli.monitor import run_cmd

        args = self._make_run_args(module_name="timeit")

        with patch("gcmon.cli.monitor.run_cmd.run_monitoring_loop", return_value=1):
            result = run_cmd.cmd_run(args)
            assert result == 1

    def test_cmd_run_validation_failure(self, caplog: pytest.LogCaptureFixture) -> None:
        """Test cmd_run returns 1 when get_monitoring_options fails."""
        from gcmon.cli.monitor import run_cmd

        args = self._make_run_args(module_name="timeit", rate=0)

        result = run_cmd.cmd_run(args)

        assert result == 1
        assert "Rate must be at least 0.001 seconds" in caplog.text


class TestRunCommandScriptMode:
    """Integration tests for run command in script mode."""

    def test_run_script_no_args(self, tmp_path: Path) -> None:
        """Test running a simple script with GC monitoring."""
        output_file = tmp_path / "trace.pftrace"
        script_file = tmp_path / "test_script.py"
        script_file.write_text(get_long_running_script("print('Hello')", "sys.exit(42)"))

        result = run_script(script_file, gc_args=["-vvv", "-o", str(output_file)])

        with print_on_failure(result):
            assert result.returncode == 42
            assert "Hello" in result.stdout

            assert_valid_perfetto_trace(output_file)

    def test_run_script_with_args_perfetto_format(self, tmp_path: Path) -> None:
        """Test running a script with arguments."""
        output_file = tmp_path / "trace.pftrace"
        script_file = tmp_path / "test_script.py"
        script_file.write_text(get_long_running_script("print('Args: ', sys.argv[1:])"))

        gc_args = ["-vvv", "--format", "perfetto", "-o", str(output_file)]
        result = run_script(script_file, "arg1", "arg2", "--flag", gc_args=gc_args)

        with print_on_failure(result):
            assert result.returncode == 0
            assert "Args:  ['arg1', 'arg2', '--flag']" in result.stdout

            assert_valid_perfetto_trace(output_file)

    def test_run_script_with_args_jsonl_format(self, tmp_path: Path) -> None:
        """Test running a script with JSONL format."""
        output_file = tmp_path / "trace.jsonl"
        script_file = tmp_path / "test_script.py"
        script_file.write_text(get_long_running_script("print('Args: ', sys.argv[1:])"))

        gc_args = ["-vvv", "--format", "jsonl", "-o", str(output_file)]
        result = run_script(script_file, "arg1", "arg2", "--flag", gc_args=gc_args)

        with print_on_failure(result):
            assert result.returncode == 0
            assert "Args:  ['arg1', 'arg2', '--flag']" in result.stdout

            assert_jsonl_format(output_file)

    def test_run_script_with_args_stdout_format(self, tmp_path: Path) -> None:
        """Test running a script with stdout format."""
        script_file = tmp_path / "test_script.py"
        script_file.write_text(get_long_running_script("print('Args: ', sys.argv[1:])"))

        gc_args = ["-vvv", "--format", "stdout"]
        result = run_script(script_file, "arg1", "arg2", "--flag", gc_args=gc_args)

        with print_on_failure(result):
            output = (result.stdout + result.stderr).lower()

            assert result.returncode == 0
            assert "Args:  ['arg1', 'arg2', '--flag']" in result.stdout

            assert_stdout_format(output)

    def test_run_script_with_overlapping_args(self, tmp_path: Path) -> None:
        """Script args after -s are passed verbatim, even if they overlap with gcmon options."""
        output_file = tmp_path / "trace.pftrace"
        script_file = tmp_path / "test_script.py"
        script_file.write_text(get_long_running_script("print('Args: ', sys.argv[1:])"))

        # gcmon options BEFORE -s, script args AFTER (including overlapping --format, -v)
        gc_args = ["-vvv", "--format", "perfetto", "-o", str(output_file)]
        result = run_script(script_file, "--format", "json", "-v", "--format", "csv", gc_args=gc_args)

        with print_on_failure(result):
            assert result.returncode == 0
            assert "Args:  ['--format', 'json', '-v', '--format', 'csv']" in result.stdout

            assert_valid_perfetto_trace(output_file)


class TestRunCommandModuleMode:
    """Integration tests for run command in module mode."""

    def test_run_module_short(self, tmp_path: Path) -> None:
        """The short `-m` form reaches the module. `timeit -n 1 pass` never
        collects, and a Perfetto trace with nothing in it is no file at all, so
        the trace itself is what the long-running test below checks."""
        output_file = tmp_path / "trace.pftrace"

        result = run_module("timeit", "-n", "1", "pass", gc_args=["-vvv", "-o", str(output_file)])

        with print_on_failure(result):
            assert result.returncode == 0

    def test_run_module_long_running_perfetto_format(self, tmp_path: Path) -> None:
        output_file = tmp_path / "trace.pftrace"

        gc_args = ["-vvv", "--format", "perfetto", "-o", str(output_file)]
        result = run_module("test", "test_gc", "-v", gc_args=gc_args)

        with print_on_failure(result):
            assert result.returncode == 0
            assert_valid_perfetto_trace(output_file)

    def test_run_module_long_running_jsonl_format(self, tmp_path: Path) -> None:
        output_file = tmp_path / "trace.jsonl"

        gc_args = ["-vvv", "--format", "jsonl", "-o", str(output_file)]
        result = run_module("test", "test_gc", "-v", gc_args=gc_args)

        with print_on_failure(result):
            assert result.returncode == 0
            assert_jsonl_format(output_file)

    def test_run_module_long_running_stdout_format(self) -> None:
        gc_args = ["-vvv", "--format", "stdout"]
        result = run_module("test", "test_gc", "-v", gc_args=gc_args)

        with print_on_failure(result):
            output = (result.stdout + result.stderr).lower()

            assert result.returncode == 0
            assert_stdout_format(output)

    def test_run_module_with_overlapping_args(self, tmp_path: Path) -> None:
        """Script args after -m are passed verbatim, even if they overlap with gcmon options."""
        output_file = tmp_path / "trace.pftrace"

        # gcmon options BEFORE -m, script args AFTER (including overlapping --format, -v)
        gc_args = ["-v", "--format", "perfetto", "-o", str(output_file)]
        result = run_module("test", "test_gc", "-v", gc_args=gc_args)

        with print_on_failure(result):
            assert output_file.exists()
            assert_valid_perfetto_trace(output_file)


class TestRunCommandErrors:
    """Integration tests for run command error handling."""

    def test_run_script_not_found(self, tmp_path: Path) -> None:
        """Test running non-existent script."""
        output_file = tmp_path / "trace.pftrace"

        result = run_script(Path("/nonexistent/script.py"), gc_args=["-vvv", "-o", str(output_file)])

        with print_on_failure(result):
            output = (result.stdout + result.stderr).lower()

            assert result.returncode != 0
            assert "script not found" in output

    def test_run_module_not_found(self, tmp_path: Path) -> None:
        """Test running non-existent module."""
        output_file = tmp_path / "trace.pftrace"

        result = run_module("nonexistent_module_xyz", gc_args=["-vvv", "-o", str(output_file)])

        with print_on_failure(result):
            output = (result.stdout + result.stderr).lower()

            assert result.returncode != 0
            assert "no module named nonexistent_module_xyz" in output

    def test_run_script_syntax_error(self, tmp_path: Path) -> None:
        """Test running script with syntax error."""
        output_file = tmp_path / "trace.pftrace"
        script_file = tmp_path / "bad_script.py"
        script_file.write_text("invalid !!!")

        result = run_script(script_file, gc_args=["-vvv", "-o", str(output_file)])

        with print_on_failure(result):
            output = (result.stdout + result.stderr).lower()

            assert result.returncode != 0
            assert "syntax" in output or "error" in output


class TestRunCommandHelp:
    """Tests for run command help."""

    def test_run_help(self) -> None:
        """Test run command help output."""
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "gcmon",
                "run",
                "--help",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )

        # Help should be displayed
        assert result.returncode == 0
        assert "Run a Python script or module" in result.stdout
        assert "-m" in result.stdout
        assert "--module" in result.stdout
        assert "--format" in result.stdout
        assert "-s" in result.stdout
        assert "--script" in result.stdout

    def test_mutually_exclusive_target(self) -> None:
        """Test that script and -m are mutually exclusive.

        Note: This is tested via unit test (TestCmdRunUnit.test_cmd_run_both_targets)
        because the CLI arg splitting makes subprocess testing impossible.
        This test verifies the help text mentions both options.
        """
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "gcmon",
                "run",
                "--help",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode == 0
        assert "--module" in result.stdout
        assert "--script" in result.stdout
