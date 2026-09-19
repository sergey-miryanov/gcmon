"""The order of the hand-rolled encoder's layers, which ADR-0001 states and
nothing else in the suite can observe.

`test_layering` places packages, and these modules share one. An import that
points down the list behaves the same as one that points up it, so it fails
here instead.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from tests.architecture.test_layering import SRC, Import, import_graph

pytestmark = pytest.mark.architecture

ENCODER_LAYERS: tuple[str, ...] = (
    "exporters.protobuf_encoder",
    "exporters.perfetto_proto",
    "exporters.perfetto_track_state",
    "exporters.perfetto_builders",
    "exporters.perfetto_process_lifetime",
    "exporters.perfetto_format",
)
"""ADR-0001's layers, top to bottom. A layer imports only from ones above it."""


def reaches_below(graph: Sequence[Import], layers: Sequence[str]) -> list[str]:
    """The layers that reach one at or below their own place in *layers*.

    Reached rather than imported: a layer importing a module outside the list
    that imports a lower layer has turned the direction round all the same.
    One message per pair, naming the first import on the way.
    """
    rank = {module: place for place, module in enumerate(layers)}
    edges: dict[str, list[Import]] = {}
    for edge in graph:
        edges.setdefault(edge.module, []).append(edge)

    found: list[str] = []
    for layer in layers:
        seen: set[str] = set()
        # Each entry is a module to visit and the layer's own import that led to it.
        pending = [(edge.imported, edge) for edge in edges.get(layer, [])]
        while pending:
            module, first = pending.pop()
            if module in seen:
                continue
            seen.add(module)
            if module in rank and rank[module] >= rank[layer]:
                found.append(f"{layer}:{first.lineno} reaches {module} through {first.imported}")
                continue
            pending.extend((edge.imported, first) for edge in edges.get(module, []))
    return sorted(found)


class TestTheEncoderAsItStandsToday:
    def test_no_layer_reaches_one_below_it(self) -> None:
        assert reaches_below(import_graph(SRC), ENCODER_LAYERS) == []

    def test_every_layer_is_a_module_that_exists(self) -> None:
        """A layer renamed away would be ranked and never met."""
        for layer in ENCODER_LAYERS:
            assert SRC.joinpath(*layer.split(".")).with_suffix(".py").exists(), layer

    def test_the_walk_finds_the_imports_between_the_layers(self) -> None:
        """A walk that found nothing would also report nothing."""
        between = {
            (edge.module, edge.imported)
            for edge in import_graph(SRC)
            if edge.module in ENCODER_LAYERS and edge.imported in ENCODER_LAYERS
        }

        assert ("exporters.perfetto_builders", "exporters.perfetto_proto") in between
        assert ("exporters.perfetto_format", "exporters.perfetto_process_lifetime") in between


class TestAnImportThatPointsDownTheList:
    """The shapes the real tree cannot supply: it holds none."""

    def test_a_builder_may_not_reach_for_layout_policy(self) -> None:
        graph = [Import("exporters.perfetto_builders", "exporters.perfetto_format", 9)]

        assert reaches_below(graph, ENCODER_LAYERS) == [
            "exporters.perfetto_builders:9 reaches exporters.perfetto_format through exporters.perfetto_format"
        ]

    def test_a_detour_through_a_module_outside_the_list_is_still_caught(self) -> None:
        graph = [
            Import("exporters.perfetto_proto", "exporters.helper", 4),
            Import("exporters.helper", "exporters.perfetto_builders", 2),
        ]

        assert reaches_below(graph, ENCODER_LAYERS) == [
            "exporters.perfetto_proto:4 reaches exporters.perfetto_builders through exporters.helper"
        ]

    def test_a_layer_may_not_import_itself_round_a_loop(self) -> None:
        graph = [
            Import("exporters.perfetto_format", "exporters.helper", 3),
            Import("exporters.helper", "exporters.perfetto_format", 5),
        ]

        assert reaches_below(graph, ENCODER_LAYERS) == [
            "exporters.perfetto_format:3 reaches exporters.perfetto_format through exporters.helper"
        ]

    def test_an_import_that_points_up_the_list_is_allowed(self) -> None:
        graph = [
            Import("exporters.perfetto_format", "exporters.perfetto_builders", 6),
            Import("exporters.perfetto_builders", "exporters.protobuf_encoder", 7),
        ]

        assert reaches_below(graph, ENCODER_LAYERS) == []

    def test_a_module_outside_the_list_may_import_any_layer(self) -> None:
        assert reaches_below([Import("exporters.encoder", "exporters.perfetto_format", 8)], ENCODER_LAYERS) == []
