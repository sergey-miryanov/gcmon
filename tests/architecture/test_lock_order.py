"""The one lock edge, which nothing else in the suite can observe.

`ProcessRegistry.create` calls the exporter's command-line sink while it
holds the registry's lock, and the sink takes the exporter's ``_io_lock``
(ADR-0025). One edge cannot deadlock. A second edge the other way, anything
running under ``_io_lock`` reaching for the registry, closes the cycle, and
the hang it makes needs two threads and a flush landing in the same
microseconds to show itself.

The layer table stops an exporter importing `ProcessRegistry`. It does not
stop one taking a `ProcessLookup`: that protocol lives in `model`, which
every layer may import, and it is what a reader of the registry holds. The
import would look ordinary and pass every other test. It fails here instead.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from gcmon.support.vocabulary import ENCODING
from tests.architecture.test_layering import SRC

pytestmark = pytest.mark.architecture

THE_LOOKUP = "ProcessLookup"
"""The name an exporter may not use. The layer table puts `ProcessRegistry`
itself out of reach, so this is the whole of the hole it leaves."""


def uses(root: Path, name: str) -> list[str]:
    """Where under *root* the identifier *name* appears, as ``path:line``.

    Identifiers rather than imports: a module reaching the protocol through
    `model.process.ProcessLookup` imports no such name, and it can still
    call `at` under the lock.
    """
    found: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding=ENCODING), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name | ast.Attribute | ast.alias) and _identifier(node) == name:
                found.add(f"{path.relative_to(root).as_posix()}:{node.lineno}")
    return sorted(found)


def _identifier(node: ast.Name | ast.Attribute | ast.alias) -> str:
    """What *node* names."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return node.asname or node.name.rsplit(".", 1)[-1]


class TestNothingUnderTheIoLockCanReachTheRegistry:
    def test_no_exporter_names_the_process_lookup(self) -> None:
        assert uses(SRC / "exporters", THE_LOOKUP) == []

    def test_the_scan_finds_the_name_where_it_belongs(self) -> None:
        """A scan that found nothing would report no violation either. The
        control server is the one reader of the registry, and it resolves a
        pid before it touches the exporter, never under its lock."""
        seen = {use.rsplit(":", 1)[0] for use in uses(SRC / "control", THE_LOOKUP)}

        assert seen == {"control_server.py"}

    def test_the_scan_reads_an_attribute_as_a_use(self) -> None:
        """`from ..model import process` leaves the name on the attribute
        alone, and that module can still call `at`."""
        module = ast.parse("process.ProcessLookup")
        attribute = next(node for node in ast.walk(module) if isinstance(node, ast.Attribute))

        assert _identifier(attribute) == THE_LOOKUP
