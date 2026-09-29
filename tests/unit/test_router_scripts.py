"""Smoke tests for the routing integration CLIs."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from click.testing import CliRunner

from book_graph_rag.domain.namespaces import SourceNamespace
from book_graph_rag.domain.routing_models import ResolvedRoute, RouteDecision
from book_graph_rag.ports.routing_telemetry_port import RoutingEvent

_ROOT = Path(__file__).parents[2]


def _load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        name,
        _ROOT / f"scripts/{name}.py",
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_route_question_help_exits_zero() -> None:
    runner = CliRunner()
    module = _load_script("route_question")

    result = runner.invoke(module.route_question, ["--help"])

    assert result.exit_code == 0
    assert "--telemetry" in result.output


def test_evaluate_namespace_routing_help_exits_zero() -> None:
    runner = CliRunner()
    module = _load_script("evaluate_namespace_routing")

    result = runner.invoke(module.evaluate_namespace_routing, ["--help"])

    assert result.exit_code == 0
    assert "--json-only" in result.output


def test_catalog_hints_are_deterministic() -> None:
    import yaml

    from book_graph_rag.domain.namespaces import Catalog
    from book_graph_rag.domain.routing_models import hints_from_catalog

    catalog = Catalog.model_validate(
        yaml.safe_load((_ROOT / "catalog.yaml").read_text(encoding="utf-8"))
    )

    hints = hints_from_catalog(catalog)

    assert len(hints) == 3
    assert all(hint.terms for hint in hints)
    assert {hint.namespace.source_id for hint in hints} == {
        "knowledge:agentic-architectural-patterns",
        "knowledge:graphrag-agentic",
        "knowledge:essential-graphrag",
    }


def test_telemetry_event_build_is_shape_stable() -> None:
    module = _load_script("route_question")
    settings = module.Settings.model_validate(
        {
            "neo4j_uri": "bolt://localhost:7687",
            "neo4j_user": "neo4j",
            "neo4j_password": "secret",
            "catalog_path": str(_ROOT / "catalog.yaml"),
        }
    )
    resolved = ResolvedRoute(
        decision=RouteDecision(route_kind="abstain", reason="low_margin"),
        fanout_namespaces=(
            SourceNamespace(corpus="knowledge", source="book-a"),
            SourceNamespace(corpus="knowledge", source="book-b"),
        ),
    )

    event = module._telemetry_event("question", resolved, settings, 12.5)

    assert isinstance(event, RoutingEvent)
    assert event.predicted_namespace is None
    assert event.route_kind == "abstain"
    assert event.latency_ms == 12.5
