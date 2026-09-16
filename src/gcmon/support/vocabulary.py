"""Words gcmon answers to, and the encoding it reads and writes in.

`support` is the layer everything may import (ADR-0026), which is what these
need: the program name reaches the CLI, the logger, the control plane's
socket prefix and the mark grammar, and the encoding reaches every file
gcmon opens.
"""

from typing import Final

__all__ = [
    "CMD_COMBINE",
    "CMD_MONITOR",
    "CMD_RUN",
    "DEFAULT_JSONL_FILE",
    "DEFAULT_TRACE_FILE",
    "ENCODING",
    "FORMAT_JSONL",
    "FORMAT_PERFETTO",
    "FORMAT_STDOUT",
    "PROGRAM_NAME",
]

# What the executable, the logger, the distribution and the mark grammar are
# all called. One word, so a rename cannot land in three of the four.
PROGRAM_NAME: Final = "gcmon"

# What `--format` and `--output-format` take. `cli` holds the tuple a parser
# offers; the words themselves are here because `analysis` and `exporters`
# branch on them and may not import `cli` (ADR-0026).
FORMAT_PERFETTO: Final = "perfetto"
FORMAT_JSONL: Final = "jsonl"
FORMAT_STDOUT: Final = "stdout"

# What a run writes when nobody names a file.
DEFAULT_TRACE_FILE: Final = f"{PROGRAM_NAME}.pftrace"
DEFAULT_JSONL_FILE: Final = f"{PROGRAM_NAME}.jsonl"

CMD_MONITOR: Final = "monitor"
CMD_RUN: Final = "run"
CMD_COMBINE: Final = "combine"

# Every file gcmon writes and reads back. JSONL is UTF-8 by specification,
# and a capture written on one machine is opened on another.
ENCODING: Final = "utf-8"
