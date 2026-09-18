"""A live Python process for the tests that drive the real `_remote_debugging`."""

import subprocess
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from gcmon.monitoring.events_reader import RemoteEventsReader, TargetUnavailable

# A target that collects, so its rings are not empty.
_TARGET = "\n".join(
    [
        "import time",
        "keep = []",
        "while True:",
        "    keep.append([object() for _ in range(500)])",
        "    keep = keep[-20:]",
        "    time.sleep(0.001)",
    ]
)


def target_executable() -> str:
    """The interpreter a target is spawned with.

    On Windows a virtual environment's ``python.exe`` is a launcher that runs
    the real interpreter as a *child*, so the pid it hands back holds no Python
    runtime and cannot be attached to. ``sys._base_executable`` is the
    interpreter itself, on every platform.
    """
    base: Any = getattr(sys, "_base_executable", None)
    return str(base) if base else sys.executable


class Target:
    """A live Python process, attachable by the time ``pid`` is read."""

    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        self._proc = proc

    @property
    def pid(self) -> int:
        return self._proc.pid

    def kill(self) -> None:
        # The pid stays pinned while anything holds a handle to it, so a reader
        # under test still resolves it; what changes is that the reads fail.
        self._proc.kill()
        self._proc.wait()


@contextmanager
def running_target(timeout: float = 20.0) -> Generator[Target]:
    proc = subprocess.Popen(
        [target_executable(), "-c", _TARGET],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    target = Target(proc)
    try:
        probe = RemoteEventsReader()
        deadline = time.monotonic() + timeout
        while True:
            try:
                probe.read(target.pid)
                break
            except TargetUnavailable:
                if time.monotonic() >= deadline:
                    raise AssertionError(f"target {target.pid} never became readable") from None
                time.sleep(0.05)
        yield target
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
