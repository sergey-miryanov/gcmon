"""The trace processor gcmon reads a trace with, pinned to Perfetto v58.2.

The ``perfetto`` package ships two halves rolled at different releases: protos
generated at v58.2, and a ``trace_processor_shell`` prebuilt still at v57.2. A
v57.2 reader skips ``TracePacket.zstd_compressed_packets`` (field 133) and
loads an empty trace rather than failing. gcmon writes that field wherever
``compression.zstd`` imports (ADR-0022).

The entries below are the shape ``get_perfetto_prebuilt`` takes, so the
download, the SHA-256 check and the cache are the package's own. A platform
absent from the list raises rather than falling back, since the fallback is
the reader this module exists to avoid.

``perfetto`` is the ``analysis`` extra's, so this module imports without it
and only asking for the shell needs it.
"""

from functools import cache

_BASE = "https://commondatastorage.googleapis.com/perfetto-luci-artifacts/v58.2"

TRACE_PROCESSOR_SHELL_MANIFEST = [
    {
        "arch": "mac-amd64",
        "file_name": "trace_processor_shell",
        "file_size": 14854504,
        "url": f"{_BASE}/mac-amd64/trace_processor_shell",
        "sha256": "3927a2767eadd140db3ff4fe0dfbf1bde35c1f56501149cd367f5cee898bef27",
        "platform": "darwin",
        "machine": ["x86_64"],
    },
    {
        "arch": "mac-arm64",
        "file_name": "trace_processor_shell",
        "file_size": 13597976,
        "url": f"{_BASE}/mac-arm64/trace_processor_shell",
        "sha256": "d29864d1ba3b36855527bb1b0ca3aa7f703cdce338b9680bb922c5c151b358fa",
        "platform": "darwin",
        "machine": ["arm64"],
    },
    {
        "arch": "linux-amd64",
        "file_name": "trace_processor_shell",
        "file_size": 14897560,
        "url": f"{_BASE}/linux-amd64/trace_processor_shell",
        "sha256": "58042408e6cc861fb1a731c26bb082dc222285561eaa4e12a48a8b2b90dca7b9",
        "platform": "linux",
        "machine": ["x86_64"],
    },
    {
        "arch": "windows-amd64",
        "file_name": "trace_processor_shell.exe",
        "file_size": 14439936,
        "url": f"{_BASE}/windows-amd64/trace_processor_shell.exe",
        "sha256": "adfa6bad3d72be3ba9b83fa2b17b69fa13b3ab1cad0f42e52b86188bd5f0f997",
        "platform": "win32",
        "machine": ["amd64"],
    },
]


class MissingExtraError(RuntimeError):
    """Reading a trace was asked of an install without the ``analysis`` extra."""

    def __init__(self) -> None:
        super().__init__("Reading a trace needs the analysis extra: pip install gcmon[analysis]")


@cache
def trace_processor_bin() -> str:
    """Path to the pinned binary, fetched and verified once per machine."""
    try:
        from perfetto.prebuilts.perfetto_prebuilts import get_perfetto_prebuilt
    except ImportError as error:
        raise MissingExtraError from error
    return str(get_perfetto_prebuilt(TRACE_PROCESSOR_SHELL_MANIFEST))
