"""No module re-spells a word `gcmon.model.names`,
`gcmon.support.vocabulary` or `gcmon.control.protocol` already owns.

The constants only help while everything reads them. A literal that creeps
back is invisible: it agrees with the constant on the day it is written and
drifts the day the constant changes, which is exactly the failure the
constants exist to prevent.

Some literals are allowed through, each for a reason the scan cannot infer:

- the vocabulary modules themselves, which are where the words are decided;
- `tests/model/test_names.py`, which spells them out on purpose so a rename
  has one place to pass through;
- the sites listed in ``ALLOWED`` below, each with its reason. Two forms
  recur there: an argument to something that writes down whatever it is
  handed, and a word that happens to read like one gcmon owns.

Only one literal is kept for a type checker: `sys.platform == "win32"` is
the form mypy and pyrefly narrow on. `hasattr` needs no such exemption,
because the narrowing lives in a `TypeGuard` signature rather than at the
call site: `gcmon.model.protocol` has a guard per field, and calling one
reads better than repeating its body.
"""

from __future__ import annotations

import ast
import io
import tokenize
from pathlib import Path

import pytest

from gcmon.support.vocabulary import ENCODING

ROOT = Path(__file__).resolve().parents[2]
TREES = (ROOT / "src" / "gcmon", ROOT / "tests")

OWNERS = (
    ROOT / "src" / "gcmon" / "model" / "names.py",
    ROOT / "src" / "gcmon" / "support" / "vocabulary.py",
    ROOT / "src" / "gcmon" / "control" / "protocol.py",
    ROOT / "tests" / "model" / "test_names.py",
    # This file lists the allowed literals, so it spells them too.
    Path(__file__).resolve(),
)

ALLOWED: dict[str, frozenset[str]] = {
    # The loss row and the bare loss slice happen to share a spelling. Tying
    # them would say that renaming the row renames the slice, which is two
    # decisions, not one.
    "src/gcmon/exporters/perfetto_format.py": frozenset({"GC Loss"}),
    # `sys.platform == "win32"` is the form mypy and pyrefly narrow on.
    "tests/control/test_control_server.py": frozenset({"win32"}),
    # A capture an earlier release wrote, not something gcmon writes now.
    "tests/cli/analyze/test_convert_cmd.py": frozenset({"GC Pause(0)"}),
    # Two labels a slice stack has to tell apart, not rows gcmon draws.
    "tests/exporters/test_perfetto_emission_order_fuzz.py": frozenset({"Process A", "Process B"}),
    # Arguments to builders that write down whatever they are handed.
    "tests/exporters/test_perfetto_builders.py": frozenset({"Process 100", "collected", "duration"}),
    # `get_env_duration` and `get_env_rss`, resolved by suffix.
    "tests/cli/monitor/test_env.py": frozenset({"duration", "rss"}),
    # A subprocess stream, not an output format.
    "tests/support/test_log_process_output.py": frozenset({"stdout"}),
    # Field names in Perfetto's own protobufs, read off `DESCRIPTOR`. The
    # spelling is the wire format's and happens to match gcmon's.
    "tests/exporters/test_perfetto_proto.py": frozenset({"cmdline", "pid"}),
    # A parameter of `MonitorLoop.__init__`, not the subcommand.
    "tests/monitoring/test_monitor_loop.py": frozenset({"monitor"}),
}


# Words gcmon owns that are also ordinary Python. `"name"` is a keyword
# argument, a `parametrize` argname and a dict key in a dozen helpers, so
# policing it reports plumbing rather than duplication. The constants stay;
# only the scan lets them through.
TOO_GENERIC: frozenset[str] = frozenset({"name", "type"})


def _watched() -> dict[str, str]:
    """``{literal: constant}`` for every word the vocabulary modules own."""
    from gcmon.control import protocol
    from gcmon.model import names
    from gcmon.support import vocabulary

    out: dict[str, str] = {}
    for module in (names, vocabulary, protocol):
        for attr in module.__all__:
            value = getattr(module, attr)
            if isinstance(value, str) and len(value) > 2 and value not in TOO_GENERIC:
                out.setdefault(value, f"{module.__name__}.{attr}")
    return out


def _literals(path: Path) -> set[str]:
    """Every non-docstring string constant in *path*."""
    text = path.read_text(encoding=ENCODING)
    out: set[str] = set()
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        if tok.type != tokenize.STRING or tok.string.startswith(('"""', "'''")):
            continue
        try:
            value = ast.literal_eval(tok.string)
        except ValueError, SyntaxError:
            continue
        if isinstance(value, str):
            out.add(value)
    return out


def _files() -> list[Path]:
    return sorted(p for tree in TREES for p in tree.rglob("*.py") if p not in OWNERS)


@pytest.mark.parametrize("path", _files(), ids=lambda p: str(p.relative_to(ROOT)).replace("\\", "/"))
def test_no_module_respells_a_word_the_vocabulary_owns(path: Path) -> None:
    watched = _watched()
    key = str(path.relative_to(ROOT)).replace("\\", "/")
    allowed = ALLOWED.get(key, frozenset())
    offenders = sorted(_literals(path) & watched.keys() - allowed)
    assert offenders == [], "\n".join(f"{key} spells {value!r}; import {watched[value]}" for value in offenders)
