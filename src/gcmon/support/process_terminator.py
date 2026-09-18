"""Process termination utilities for graceful subprocess shutdown.

This module provides cross-platform process termination functionality
with escalating signals and timeout handling.
"""

import logging
import signal
import subprocess
import sys

from .vocabulary import ENCODING, PROGRAM_NAME

__all__ = ["log_process_output", "terminate_process"]

# Timeout constants
DEFAULT_GRACEFUL_TIMEOUT = 5.0  # seconds: timeout for graceful shutdown
DEFAULT_FORCE_TIMEOUT = 2.0  # seconds: timeout for forceful termination

_logger = logging.getLogger(PROGRAM_NAME)


def _send_sigint(process: subprocess.Popen[bytes]) -> None:
    """
    Send a signal to a process, catching and logging any errors.
    """
    if sys.platform == "win32":
        signal_value, name = (signal.CTRL_BREAK_EVENT, "CTRL_BREAK_EVENT")
    else:
        signal_value, name = (signal.SIGINT, "SIGINT")

    try:
        _logger.debug("Sending %s to process: %s", name, process)
        process.send_signal(signal_value)
    except (ProcessLookupError, OSError) as e:
        _logger.warning("Failed to send %s to process: %s", name, e)


def _communicate_with_timeout(
    process: subprocess.Popen[bytes],
    timeout: float | None,
    timeout_description: str,
) -> tuple[bytes, bytes]:
    """
    Call process.communicate() with timeout, handling TimeoutExpired.

    Returns:
        Tuple of (stdout_data, stderr_data), or (b"", b"") on timeout or if already communicated
    """
    try:
        _logger.debug(
            "Waiting for process to exit (%s, timeout=%s)",
            timeout_description,
            timeout if timeout is not None else "indefinite",
        )
        result = process.communicate(timeout=timeout)
        # Handle case where communicate() returns (None, None) on second call
        return (result[0] or b"", result[1] or b"")
    except subprocess.TimeoutExpired:
        _logger.warning("Process did not exit within %s timeout", timeout_description)
        return b"", b""


def terminate_process(
    process: subprocess.Popen[bytes],
    graceful_timeout: float = DEFAULT_GRACEFUL_TIMEOUT,
    force_timeout: float = DEFAULT_FORCE_TIMEOUT,
) -> tuple[bytes, bytes]:
    """
    Gracefully terminate a subprocess with escalating signals.

    Signal escalation flow:
    1. Send graceful signal (SIGINT on Unix, CTRL_BREAK_EVENT on Windows)
    2. Wait for graceful_timeout
    3. On timeout:
       - Send SIGTERM via terminate(), wait, then SIGKILL via kill()
    4. Final wait (indefinite if needed) to prevent zombie processes

    All exceptions from signal operations are caught and logged internally.
    The function always returns normally with whatever output could be collected.

    Args:
        process: The subprocess to terminate
        graceful_timeout: Timeout for graceful shutdown in seconds
            (default: 5.0)
        force_timeout: Timeout for forceful termination in seconds
            (default: 2.0)

    Returns:
        Tuple of (stdout_data, stderr_data) from the process
    """
    if process.poll() is not None:
        return b"", b""

    # Step 1: Send graceful shutdown signal
    _send_sigint(process)
    stdout_data, stderr_data = _communicate_with_timeout(
        process=process,
        timeout=graceful_timeout,
        timeout_description="graceful shutdown",
    )

    # Check if process exited gracefully
    if process.poll() is not None:
        return stdout_data, stderr_data

    _logger.debug("Process did not exit gracefully, escalating to forceful termination")

    process.terminate()
    stdout_data, stderr_data = _communicate_with_timeout(
        process=process,
        timeout=force_timeout,
        timeout_description="terminate",
    )

    # Check if process terminates
    if process.poll() is not None:
        return stdout_data, stderr_data

    _logger.debug("Process did not terminate, escalating to kill")

    process.kill()
    stdout_data, stderr_data = _communicate_with_timeout(
        process=process,
        timeout=force_timeout,
        timeout_description="final cleanup",
    )

    # If still running (shouldn't happen), wait indefinitely
    if process.poll() is None:
        _logger.debug("Process still running after kill, waiting indefinitely")
        stdout_data, stderr_data = _communicate_with_timeout(
            process=process,
            timeout=None,
            timeout_description="indefinite cleanup",
        )

    return stdout_data, stderr_data


def _is_signal_exit_code(returncode: int) -> bool:
    """
    Check if exit code represents a signal-based shutdown (normal on Windows/Unix).

    Windows NTSTATUS codes for signal exits:
    - 0xC000013A (-1073741510): STATUS_CONTROL_C_EXIT (Ctrl+C or CTRL_BREAK_EVENT)

    Unix: Negative exit codes indicate signal termination
    - -SIGINT (-2): Interrupt signal
    - -SIGTERM (-15): Termination signal

    Returns:
        True if exit code represents a signal-based shutdown
    """
    # Windows STATUS_CONTROL_C_EXIT
    if returncode == 0xC000013A:
        return True

    # Unix negative signal codes
    return returncode in (-signal.SIGINT, -signal.SIGTERM)


def log_process_output(process: subprocess.Popen[bytes], stdout_data: bytes) -> None:
    """
    Log process output based on exit code.

    Signal-based exit codes (Ctrl+C, SIGINT, SIGTERM) are treated as normal
    shutdown and logged at DEBUG level. Non-zero exit codes are logged at
    WARNING level. Successful exits (code 0) produce no output.
    """
    # Decode output (handle None from terminate_process)
    stdout_str = stdout_data.decode(ENCODING, errors="replace").strip()

    # Check if process has exited
    if process.poll() is None:
        _logger.warning(
            "Process (PID %s) has not terminated",
            process.pid,
        )
        return

    # Log output based on exit code
    # Signal-based exits (Ctrl+C, SIGINT, SIGTERM) are normal shutdown
    if process.returncode != 0 and not _is_signal_exit_code(process.returncode):
        # Log with warning level on error (non-signal exit)
        if stdout_str:
            _logger.warning(
                "Process (PID %s) exited with code %s. stdout:\n%s",
                process.pid,
                process.returncode,
                stdout_str,
            )
    elif _is_signal_exit_code(process.returncode):
        # Log with debug level for signal-based exit
        exit_description = "terminated by signal"
        if stdout_str:
            _logger.debug(
                "Process (PID %s) %s. stdout:\n%s",
                process.pid,
                exit_description,
                stdout_str,
            )
