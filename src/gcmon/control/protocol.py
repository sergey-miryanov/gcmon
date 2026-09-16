"""The control plane's wire vocabulary.

A client sends one of these words and the server compares against it, in two
modules that need not change together. Spelling it twice is how a rename
becomes a client that talks to a server that never answers, with nothing
failing at import or type-check time.

The `*_EVENT` strings are what the server writes onto the trace, not what it
receives, so they are a separate pair.
"""

from typing import Final

__all__ = [
    "MSG",
    "MSG_START",
    "MSG_STOP",
    "PID",
    "START_EVENT",
    "STOP_EVENT",
    "TS",
]

from ..model.names import PID, TS

MSG: Final = "msg"

MSG_START: Final = "start"
MSG_STOP: Final = "stop"

START_EVENT: Final = "start GC monitor"
STOP_EVENT: Final = "stop GC monitor"
