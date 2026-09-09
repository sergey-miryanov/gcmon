"""The layering, which nothing else in the suite can observe.

Every arrangement of these modules produces the same behaviour, so a
dependency inversion cannot fail a behavioural test. It fails here instead.
The walk reads the imports without running them; the check answers whether an
import crosses a layer the wrong way. See spec 0041.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

pytestmark = pytest.mark.architecture

PACKAGE = "gcmon"
SRC = Path(__file__).resolve().parent.parent.parent / "src" / PACKAGE

ALLOWED: dict[str, frozenset[str]] = {
    # The base both towers stand on.
    "support": frozenset(),
    "model": frozenset({"support"}),
    "exporters": frozenset({"model", "support"}),
    "stats": frozenset({"model", "support"}),
    "cli.shared": frozenset(),
    # The monitor tower, which runs beside a live process.
    "control": frozenset({"model", "exporters", "support"}),
    "monitoring": frozenset({"model", "exporters", "stats", "control", "support"}),
    "cli.monitor": frozenset({"model", "exporters", "stats", "control", "monitoring", "support", "cli.shared"}),
    # The analysis tower, which reads a file gcmon already wrote.
    "analysis": frozenset({"model", "exporters", "support"}),
    "cli.analyze": frozenset({"model", "exporters", "stats", "analysis", "support", "cli.shared"}),
    # The one place both towers are reachable, because it assembles the parser
    # from both.
    "cli": frozenset(
        {
            "model",
            "exporters",
            "stats",
            "analysis",
            "control",
            "monitoring",
            "support",
            "cli.shared",
            "cli.monitor",
            "cli.analyze",
        }
    ),
}
"""What each layer may import. The table lives here because it is a statement
about the architecture, and this is where such a statement can fail.

`analysis` is denied `stats`: it reads and writes files and computes nothing,
and the fold from records into a table sits in `cli.analyze` above it. See
ADR-0026."""

ROOT_CLI = frozenset({"__init__", "__main__"})
"""The two modules that cannot live anywhere else.

One defines the package and the other is what `python -m gcmon` runs, and both
belong to `cli` by direction. The root cannot be a directory, so this is the
one membership a path cannot answer; enumerating it is what stops a new root
module from being handed the CLI's permissions by default."""

FOLDED: dict[str, str] = {"pyperf": "cli.monitor"}
"""A directory that is part of a layer named for somewhere else.

The pyperf hook is an entry point into gcmon exactly as the console script is,
and nothing below imports it, so it is not a layer of its own. It belongs to
the monitor tower because it runs inside the target (ADR-0023), which is the
side of the capture file a tower is defined by (ADR-0026)."""


@dataclass(frozen=True)
class Import:
    """One import from the package into itself, named as the walk sees it.

    ``module`` and ``imported`` are dotted paths relative to ``gcmon``, so
    ``monitor`` and ``exporters.exporter`` rather than ``gcmon.monitoring.monitor``.
    """

    module: str
    imported: str
    lineno: int


def layer_of(module: str) -> str | None:
    """The layer *module* belongs to.

    The directory answers: a module under `stats/` is `stats`, and one under
    `cli/monitor/` is `cli.monitor`. The two-segment name is tried before the
    head, because `cli` at the head would hand a tower every permission the
    CLI has. Two rules make the directory answer without exceptions in the
    tree. The package root, where `__init__.py` and `__main__.py` have to
    live, is `cli`, and `pyperf` is part of the monitor tower: both are entry
    points.

    Nothing else is placed. A directory that is not a layer and a module at
    the root that is neither the CLI's nor a shim both come back None, and
    `unplaced` is what turns that into a failure.
    """
    parts = module.split(".")
    if len(parts) > 1 and ".".join(parts[:2]) in ALLOWED:
        return ".".join(parts[:2])
    head = parts[0]
    if head in ALLOWED:
        return head
    if head in FOLDED:
        return FOLDED[head]
    if "." in module:
        return None
    return "cli" if head in ROOT_CLI else None


def import_graph(root: Path) -> list[Import]:
    """Every intra-package import under *root*, read with ``ast``.

    Parsing rather than importing keeps this fast, free of import side
    effects, and independent of whether an optional dependency is installed.
    """
    graph: list[Import] = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        module = ".".join(path.relative_to(root).with_suffix("").parts)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Import | ast.ImportFrom):
                continue
            for imported in _targets_of(node, module, root):
                graph.append(Import(module, imported, node.lineno))
    return graph


def _targets_of(node: ast.Import | ast.ImportFrom, module: str, root: Path) -> list[str]:
    """What *node* imports from the package, relative to it, if anything."""
    if isinstance(node, ast.Import):
        return [name.name[len(PACKAGE) + 1 :] for name in node.names if name.name.startswith(f"{PACKAGE}.")]
    if node.level == 0:
        if node.module == PACKAGE:
            return _named(node, [], root)
        if node.module is not None and node.module.startswith(f"{PACKAGE}."):
            return [node.module[len(PACKAGE) + 1 :]]
        return []
    base = module.split(".")[:-1]
    if node.level > 1:
        base = base[: len(base) - (node.level - 1)]
    if node.module is not None:
        return [".".join([*base, node.module])]
    return _named(node, base, root)


def _named(node: ast.ImportFrom, base: list[str], root: Path) -> list[str]:
    """Resolve ``from <package> import name``, where *name* may be either.

    A name is a submodule if the file is there, and otherwise something the
    package's ``__init__`` exports, such as ``__version__``.
    """
    targets = []
    for name in node.names:
        dotted = [*base, name.name]
        is_module = root.joinpath(*dotted).with_suffix(".py").exists() or root.joinpath(*dotted, "__init__.py").exists()
        targets.append(".".join(dotted) if is_module else ".".join([*base, "__init__"]))
    return targets


def violations(
    graph: Sequence[Import],
    layer: Callable[[str], str | None],
    allowed: dict[str, frozenset[str]],
) -> list[str]:
    """The imports in *graph* that cross a layer the wrong way.

    One message per crossing, naming the importing module, the imported
    module and the edge that is not allowed. A bare "layering violation"
    costs more to diagnose than the rule saves.
    """
    found: list[str] = []
    for edge in graph:
        source, target = layer(edge.module), layer(edge.imported)
        if source is None or target is None or source == target:
            continue
        if target not in allowed[source]:
            found.append(f"{edge.module}:{edge.lineno} imports {edge.imported}: {source} may not import {target}")
    return found


def unplaced(root: Path, layer: Callable[[str], str | None]) -> list[str]:
    """The modules under *root* that no rule places in a layer.

    A module `violations` cannot place is one it silently passes over, so the
    package growing a directory that is not a layer has to fail here instead.
    """
    return sorted(module for module in _modules(root) if layer(module) is None)


def _modules(root: Path) -> list[str]:
    """Every module under *root*, named the way the walk names them."""
    return [
        ".".join(path.relative_to(root).with_suffix("").parts)
        for path in sorted(root.rglob("*.py"))
        if "__pycache__" not in path.parts
    ]


class TestThePackageAsItStandsToday:
    """Case 1: the day-one state, which the test's first job is to record."""

    def test_no_import_crosses_a_layer_the_wrong_way(self) -> None:
        assert violations(import_graph(SRC), layer_of, ALLOWED) == []

    def test_the_walk_finds_the_imports_that_are_there(self) -> None:
        """A walk that found nothing would also report no violations."""
        graph = import_graph(SRC)
        assert len(graph) > 50
        assert any(edge.module == "monitoring.monitor" and edge.imported.startswith("model.data") for edge in graph)

    def test_every_module_has_a_layer(self) -> None:
        """``violations`` skips what it cannot place, so a module in a
        directory that is not a layer would be guarded by nothing."""
        assert unplaced(SRC, layer_of) == []


class TestTheLayerOfAModule:
    """The directory answers, with the package root standing for `cli`."""

    def test_a_module_in_a_layer_directory_belongs_to_that_layer(self) -> None:
        assert layer_of("support.set_on_exit") == "support"
        assert layer_of("exporters.exporter") == "exporters"

    def test_a_directory_under_the_cli_that_is_not_a_tower_is_the_cli(self) -> None:
        """The table knows three names under `cli/`. Anything else there falls
        back to the head, and `main.py` reaches every layer."""
        assert layer_of("cli.helpers.thing") == "cli"

    def test_the_two_modules_the_root_must_hold_are_cli(self) -> None:
        assert layer_of("__init__") == "cli"
        assert layer_of("__main__") == "cli"

    def test_a_tower_under_the_cli_answers_for_itself(self) -> None:
        """The two-segment name is tried first. `cli` at the head would
        otherwise hand a tower every permission the CLI has."""
        assert layer_of("cli.monitor.run_cmd") == "cli.monitor"
        assert layer_of("cli.analyze.convert_cmd") == "cli.analyze"
        assert layer_of("cli.shared.parser_factory") == "cli.shared"

    def test_the_cli_itself_is_not_a_tower(self) -> None:
        assert layer_of("cli.main") == "cli"

    def test_the_pyperf_hook_is_monitor_tower_code(self) -> None:
        """It runs inside the target, beside a live process."""
        assert layer_of("pyperf.hook") == "cli.monitor"

    def test_a_directory_that_is_not_a_layer_places_nothing(self) -> None:
        assert layer_of("newthing.module") is None

    def test_a_module_that_moved_out_of_the_root_is_not_still_there(self) -> None:
        """The old flat paths are gone rather than shimmed: `data` belongs to
        `model` now, and nothing at the root answers for it."""
        assert layer_of("data") is None

    def test_a_new_module_at_the_root_places_nothing_either(self) -> None:
        """Otherwise it inherits the CLI's permissions by sitting still, and
        the directory that was supposed to decide never gets asked."""
        assert layer_of("cache") is None


class TestAnImportThatCrossesTheWrongWay:
    """Cases 2 and 3, which the real tree cannot supply: it holds no violation."""

    def test_the_record_model_may_not_import_the_cli(self) -> None:
        graph = [Import("model.data", "cli", 3)]
        assert violations(graph, layer_of, ALLOWED) == ["model.data:3 imports cli: model may not import cli"]

    def test_the_exporters_and_stats_are_siblings_in_both_directions(self) -> None:
        assert violations([Import("exporters.exporter", "stats", 7)], layer_of, ALLOWED) == [
            "exporters.exporter:7 imports stats: exporters may not import stats"
        ]
        assert violations([Import("stats", "exporters.exporter", 7)], layer_of, ALLOWED) == [
            "stats:7 imports exporters.exporter: stats may not import exporters"
        ]

    def test_an_import_that_goes_down_is_allowed(self) -> None:
        assert violations([Import("monitoring.monitor", "model.data", 11)], layer_of, ALLOWED) == []

    def test_an_import_inside_one_layer_is_allowed(self) -> None:
        assert violations([Import("exporters.exporter", "exporters.encoder", 4)], layer_of, ALLOWED) == []


class TestAnImportThatCrossesBetweenTheTowers:
    """The permission the split exists to withdraw, which the tree cannot
    supply either: no module has moved into a tower yet."""

    def test_the_analysis_tower_may_not_reach_a_live_process(self) -> None:
        assert violations([Import("analysis.combine", "monitoring.monitor", 5)], layer_of, ALLOWED) == [
            "analysis.combine:5 imports monitoring.monitor: analysis may not import monitoring"
        ]
        assert violations([Import("cli.analyze.report_cmd", "control.control_client", 6)], layer_of, ALLOWED) == [
            "cli.analyze.report_cmd:6 imports control.control_client: cli.analyze may not import control"
        ]

    def test_neither_tower_may_import_the_cli(self) -> None:
        """`cli.main` reaches both, so importing it is how one tower would
        reach the other."""
        assert violations([Import("cli.monitor.monitor_cmd", "cli.main", 8)], layer_of, ALLOWED) == [
            "cli.monitor.monitor_cmd:8 imports cli.main: cli.monitor may not import cli"
        ]
        assert violations([Import("cli.analyze.convert_cmd", "cli.main", 8)], layer_of, ALLOWED) == [
            "cli.analyze.convert_cmd:8 imports cli.main: cli.analyze may not import cli"
        ]

    def test_the_analysis_layer_computes_nothing(self) -> None:
        """`stats` is reached from `cli.analyze` above it, so that the fold
        from records into a table has one home."""
        assert violations([Import("analysis.jsonl_io", "stats.streaming_stats", 4)], layer_of, ALLOWED) == [
            "analysis.jsonl_io:4 imports stats.streaming_stats: analysis may not import stats"
        ]

    def test_both_towers_may_take_what_the_shared_base_holds(self) -> None:
        assert violations([Import("cli.monitor.monitor_cmd", "cli.shared.parser_factory", 2)], layer_of, ALLOWED) == []
        assert violations([Import("cli.analyze.convert_cmd", "cli.shared.parser_factory", 2)], layer_of, ALLOWED) == []
