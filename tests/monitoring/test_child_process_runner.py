import os
import subprocess
import sys
from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

from gcmon.monitoring.child_process_runner import ChildProcess, ChildProcessRunner, ProcessStdoutReader


@pytest.fixture
def mock_popen() -> Mock:
    popen = Mock(spec=subprocess.Popen)
    popen.pid = 99999
    popen.poll.return_value = None
    popen.stdout = None
    return popen


@pytest.fixture
def runner(tmp_path: Path) -> ChildProcessRunner:
    script = tmp_path / "test_script.py"
    script.write_text("print('hello')")
    return ChildProcessRunner(str(script))


@pytest.fixture
def mock_popen_and_reader(mock_popen: Mock) -> Generator[Mock]:
    with (
        patch("subprocess.Popen", return_value=mock_popen) as mock_popen_cls,
        patch("gcmon.monitoring.child_process_runner.ProcessStdoutReader"),
    ):
        yield mock_popen_cls


@pytest.fixture
def mock_popen_os_error() -> Generator[None]:
    with patch("subprocess.Popen", side_effect=OSError("permission denied")):
        yield


@pytest.fixture
def mock_popen_immediate_exit(mock_popen: Mock) -> Generator[None]:
    mock_popen.poll.return_value = 0
    mock_popen.communicate.return_value = (b"output", None)
    with patch("subprocess.Popen", return_value=mock_popen):
        yield


@pytest.fixture
def mock_terminate_process() -> Generator[Mock]:
    with patch(
        "gcmon.monitoring.child_process_runner.terminate_process",
        return_value=(b"stdout", b"stderr"),
    ) as mock_term:
        yield mock_term


@pytest.fixture
def mock_log_process_output() -> Generator[None]:
    with patch("gcmon.monitoring.child_process_runner.log_process_output"):
        yield


@pytest.fixture
def mock_runner_terminate(runner: ChildProcessRunner) -> Generator[Mock]:
    with patch.object(runner, "terminate") as mock_term:
        yield mock_term


@pytest.fixture
def module_runner() -> ChildProcessRunner:
    return ChildProcessRunner("my_module", is_module=True)


@pytest.fixture
def runner_with_args(runner: ChildProcessRunner) -> ChildProcessRunner:
    runner._passthrough_args = ["--verbose", "--output=file.json"]
    return runner


class TestChildProcessRunnerInit:
    def test_stores_target(self, runner: ChildProcessRunner) -> None:
        assert runner._target.endswith("test_script.py")

    def test_module_mode(self, module_runner: ChildProcessRunner) -> None:
        assert module_runner._target == "my_module"
        assert module_runner._is_module

    def test_passthrough_args(self, runner_with_args: ChildProcessRunner) -> None:
        assert runner_with_args._passthrough_args == ["--verbose", "--output=file.json"]

    def test_custom_env(self) -> None:
        runner = ChildProcessRunner("script.py", env={"VAR": "val"})
        assert runner._env == {"VAR": "val"}

    def test_default_values(self) -> None:
        runner = ChildProcessRunner("script.py")
        assert runner._is_module is False
        assert runner._passthrough_args == []
        assert runner._process is None


class TestValidateTarget:
    def test_script_exists(self, runner: ChildProcessRunner) -> None:
        runner._validate_target()

    def test_script_not_found(self) -> None:
        runner = ChildProcessRunner("/nonexistent/script.py")
        with pytest.raises(FileNotFoundError, match="Script not found"):
            runner._validate_target()

    def test_not_a_file(self, tmp_path: Path) -> None:
        d = tmp_path / "a_directory"
        d.mkdir()
        runner = ChildProcessRunner(str(d))
        with pytest.raises(ValueError, match="not a file"):
            runner._validate_target()

    def test_module_valid(self, module_runner: ChildProcessRunner) -> None:
        module_runner._validate_target()

    def test_module_empty(self) -> None:
        runner = ChildProcessRunner("  ", is_module=True)
        with pytest.raises(ValueError, match="cannot be empty"):
            runner._validate_target()


class TestBuildCommand:
    def test_script_mode(self, runner: ChildProcessRunner, tmp_path: Path) -> None:
        cmd = runner._build_command()
        assert cmd[0] == sys.executable
        assert "-u" in cmd
        script_path = str((tmp_path / "test_script.py").resolve())
        assert script_path in cmd
        assert "-m" not in cmd

    def test_module_mode(self, module_runner: ChildProcessRunner) -> None:
        cmd = module_runner._build_command()
        assert "-m" in cmd
        assert "my_module" in cmd

    def test_with_passthrough_args(self, runner_with_args: ChildProcessRunner) -> None:
        cmd = runner_with_args._build_command()
        assert "--verbose" in cmd
        assert "--output=file.json" in cmd

    def test_module_mode_with_args(self) -> None:
        runner = ChildProcessRunner("http.server", is_module=True, passthrough_args=["8080"])
        cmd = runner._build_command()
        assert "-m" in cmd
        assert cmd[cmd.index("-m") + 1] == "http.server"
        assert cmd[-1] == "8080"


class TestBuildEnv:
    def test_inherits_os_environ(self, runner: ChildProcessRunner) -> None:
        with patch.dict(os.environ, {"EXISTING": "value"}, clear=True):
            env = runner._build_env()
            assert env["EXISTING"] == "value"

    def test_merges_custom_env(self) -> None:
        runner = ChildProcessRunner("script.py", env={"CUSTOM": "val"})
        with patch.dict(os.environ, {"BASE": "base_val"}, clear=True):
            env = runner._build_env()
            assert env["BASE"] == "base_val"
            assert env["CUSTOM"] == "val"


class TestProperties:
    def test_process_none_before_start(self, runner: ChildProcessRunner) -> None:
        assert runner.process is None

    def test_pid_none_before_start(self, runner: ChildProcessRunner) -> None:
        assert runner.pid is None

    def test_is_running_false_before_start(self, runner: ChildProcessRunner) -> None:
        assert runner.is_running is False

    def test_returncode_none_before_start(self, runner: ChildProcessRunner) -> None:
        assert runner.returncode is None

    def test_is_running_true_when_running(self, runner: ChildProcessRunner, mock_popen: Mock) -> None:
        runner._process = mock_popen
        assert runner.is_running is True

    def test_returncode_after_terminate(self, runner: ChildProcessRunner, mock_popen: Mock) -> None:
        mock_popen.poll.return_value = 0
        runner._process = mock_popen
        assert runner.returncode == 0


class TestStart:
    def test_spawns_subprocess(self, runner: ChildProcessRunner, mock_popen_and_reader: Mock) -> None:
        result = runner.start()

        assert isinstance(result, ChildProcess)
        assert result.pid == 99999
        mock_popen_and_reader.assert_called_once()

    def test_immediate_exit_raises(
        self, runner: ChildProcessRunner, mock_popen_immediate_exit: Generator[None]
    ) -> None:
        with pytest.raises(RuntimeError, match="exited immediately"):
            runner.start()

    def test_os_error_raises(self, runner: ChildProcessRunner, mock_popen_os_error: Generator[None]) -> None:
        with pytest.raises(RuntimeError, match="Failed to start"):
            runner.start()


class TestTerminate:
    def test_stdout_thread_stopped(
        self, runner: ChildProcessRunner, mock_terminate_process: Mock, mock_log_process_output: Generator[None]
    ) -> None:
        thread = Mock(spec=ProcessStdoutReader)
        runner._process = Mock(spec=subprocess.Popen)
        runner._stdout_thread = thread

        runner.terminate()

        thread.stop.assert_called_once()
        assert runner._stdout_thread is None  # type: ignore[comparison-overlap]

    def test_terminates_process(
        self, runner: ChildProcessRunner, mock_terminate_process: Mock, mock_log_process_output: Generator[None]
    ) -> None:
        runner._process = Mock(spec=subprocess.Popen)

        runner.terminate()

        mock_terminate_process.assert_called_once()

    def test_no_process_returns_empty(self, runner: ChildProcessRunner) -> None:
        result = runner.terminate()
        assert result == b""


class TestWait:
    def test_wait_no_process(self, runner: ChildProcessRunner) -> None:
        runner.wait(timeout=1.0)
        assert runner.returncode is None

    def test_wait_exits_normally(self, runner: ChildProcessRunner, mock_popen: Mock) -> None:
        runner._process = mock_popen
        runner.wait(timeout=1.0)
        mock_popen.wait.assert_called_once_with(timeout=1.0)

    def test_wait_timeout_logs_warning(
        self, runner: ChildProcessRunner, mock_popen: Mock, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_popen.wait.side_effect = subprocess.TimeoutExpired(cmd="test", timeout=0.1)

        runner._process = mock_popen
        runner.wait(timeout=0.1)

        assert "Timed out waiting for subprocess (PID 99999) to exit after 0.1 seconds" in caplog.text


class TestClose:
    def test_close_delegates_to_terminate(self, runner: ChildProcessRunner, mock_runner_terminate: Mock) -> None:
        runner._process = Mock()

        runner.close()
        mock_runner_terminate.assert_called_once()


class TestContextManager:
    def test_enters_and_exits(self, runner: ChildProcessRunner, mock_popen: Mock, mock_runner_terminate: Mock) -> None:
        runner._process = mock_popen
        with runner:
            assert runner.is_running
        mock_runner_terminate.assert_called_once()

    def test_cleanup_on_exception(
        self, runner: ChildProcessRunner, mock_popen: Mock, mock_runner_terminate: Mock
    ) -> None:
        runner._process = mock_popen
        try:
            with runner:
                raise ValueError("test error")
        except ValueError:
            pass
        mock_runner_terminate.assert_called_once()


class TestProcessStdoutReader:
    def test_start(self) -> None:
        process = Mock(spec=subprocess.Popen)
        process.stdout = MagicMock()
        process.stdout.readline.return_value = b"data\n"
        reader = ProcessStdoutReader(process)
        reader.start()
        assert reader._thread.is_alive()
        reader.stop()

    def test_stop(self) -> None:
        process = Mock(spec=subprocess.Popen)
        process.stdout = MagicMock()
        process.stdout.readline.return_value = b"data\n"
        reader = ProcessStdoutReader(process)
        reader.start()
        reader.stop()
        assert not reader._thread.is_alive()
