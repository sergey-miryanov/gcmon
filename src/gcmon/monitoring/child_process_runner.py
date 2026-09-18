import logging
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Self, override

from ..control.control_server import set_control_env
from ..support.process_terminator import log_process_output, terminate_process
from ..support.vocabulary import ENCODING, PROGRAM_NAME
from .target_process import TargetProcess

__all__ = ["ChildProcess", "ChildProcessRunner"]

logger = logging.getLogger(PROGRAM_NAME)


class ChildProcess(TargetProcess):
    def __init__(self, pid: int):
        self._pid = pid

    @property
    @override
    def pid(self) -> int:
        return self._pid


class ChildProcessRunner:
    def __init__(
        self,
        target: str,
        is_module: bool = False,
        passthrough_args: list[str] | None = None,
        env: dict[str, str] | None = None,
        control_address: str | None = None,
    ) -> None:
        self._target = target
        self._is_module = is_module
        self._passthrough_args = passthrough_args or []
        self._env = env
        self._control_address = control_address
        self._process: subprocess.Popen[bytes] | None = None
        self._stdout_thread: ProcessStdoutReader | None = None

    def _validate_target(self) -> None:
        if self._is_module:
            # Module mode: validate module name is not empty
            if not self._target.strip():
                raise ValueError("Module name cannot be empty")
        else:
            # Script mode: validate file exists and is readable
            script_path = Path(self._target)
            if not script_path.exists():
                raise FileNotFoundError(f"Script not found: {self._target}")
            if not script_path.is_file():
                raise ValueError(f"Target is not a file: {self._target}")

    def _build_command(self) -> list[str]:
        cmd = [sys.executable, "-u"]

        if self._is_module:
            # Module mode: python -m module_name [args...]
            cmd.append("-m")
            cmd.append(self._target)
        else:
            # Script mode: python script_path [args...]
            # Resolve to absolute path to ensure correct execution
            script_path = str(Path(self._target).resolve())
            cmd.append(script_path)

        # Add passthrough arguments
        cmd.extend(self._passthrough_args)

        return cmd

    def _build_env(self) -> dict[str, str]:
        """Build the environment for the subprocess.

        Returns:
            Environment dictionary for subprocess
        """
        # Start with current environment
        env = os.environ.copy()

        # Merge custom environment variables
        if self._env:
            env.update(self._env)

        # Inject control plane address for child processes
        if self._control_address is not None:
            set_control_env(env, self._control_address)

        return env

    def start(self) -> ChildProcess:
        """Spawn the subprocess and return its PID.

        Returns:
            Process ID of spawned subprocess

        Raises:
            FileNotFoundError: If target script doesn't exist (script mode)
            ValueError: If target is invalid
            RuntimeError: If subprocess fails to start
        """
        # Validate target before spawning
        self._validate_target()

        # Build command and environment
        cmd = self._build_command()
        env = self._build_env()

        logger.debug("Subprocess cmd: %s", " ".join(cmd))

        # Configure subprocess creation flags for cross-platform compatibility
        creationflags = 0
        if sys.platform == "win32":
            # Windows: Create new process group for proper signal handling
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
                env=env,
            )
        except OSError as e:
            raise RuntimeError(f"Failed to start subprocess: {e}") from e

        if self._process.poll() is not None:
            stdout_data, _ = self._process.communicate()
            stdout_str = stdout_data.decode(ENCODING, errors="replace").strip()

            logger.debug("Subprocess exited immediately: %s", stdout_str)
            raise RuntimeError("Subprocess exited immediately.")

        self._stdout_thread = ProcessStdoutReader(self._process)
        self._stdout_thread.start()
        return ChildProcess(self._process.pid)

    @property
    def process(self) -> subprocess.Popen[bytes] | None:
        """Return the subprocess handle, or None if not started."""
        return self._process

    @property
    def pid(self) -> int | None:
        """Return the process ID, or None if not started."""
        return self._process.pid if self._process is not None else None

    @property
    def is_running(self) -> bool:
        """Check if the subprocess is still running."""
        if self._process is not None:
            return self._process.poll() is None

        return False

    @property
    def returncode(self) -> int | None:
        """Return the subprocess exit code, or None if still running."""
        if self._process is not None:
            return self._process.poll()

        return None

    def wait(self, timeout: float | None = None) -> None:
        """Wait for the subprocess to terminate.

        Args:
            timeout: Maximum time to wait in seconds, or None to wait indefinitely.
        """
        if self._process is None:
            return
        try:
            self._process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            logger.warning(
                "Timed out waiting for subprocess (PID %s) to exit after %s seconds",
                self._process.pid,
                timeout,
            )

    def terminate(
        self,
        graceful_timeout: float = 5.0,
        force_timeout: float = 2.0,
    ) -> bytes:
        """Terminate the subprocess gracefully.

        Uses escalating signals for graceful shutdown:
        - SIGINT → SIGTERM → SIGKILL
        """
        if self._stdout_thread is not None:
            self._stdout_thread.stop()
            self._stdout_thread = None

        if self._process is None:
            return b""

        # Use the shared terminate_process utility
        stdout_data, _ = terminate_process(
            process=self._process,
            graceful_timeout=graceful_timeout,
            force_timeout=force_timeout,
        )

        # Log process output
        log_process_output(
            process=self._process,
            stdout_data=stdout_data,
        )

        return stdout_data

    def close(self) -> None:
        self.terminate()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.terminate()


class ProcessStdoutReader:
    def __init__(self, process: subprocess.Popen[bytes]):
        self._process = process
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=type(self).__qualname__,
            daemon=True,
        )

    def start(self) -> None:
        self._stop_event.clear()
        self._thread.start()

    def stop(self, timeout: float = 1.0) -> None:
        self._stop_event.set()
        self._thread.join(timeout=timeout)

    def _run(self) -> None:
        pipe = self._process.stdout
        if pipe is None:
            return

        for line in iter(pipe.readline, b""):
            assert line, "iter() stops on the empty line"
            print(line.decode(ENCODING, errors="replace"), end="", flush=True)
            if self._stop_event.is_set():
                break
