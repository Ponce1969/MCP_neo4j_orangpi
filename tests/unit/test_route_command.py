"""Behaviour of the read-only ``route`` CLI command.

The command is the namespace router's first production call site. These tests
exercise it through Click only: the composition factory and settings
construction are replaced, so no embedding model, profile artifact, Neo4j
connection or developer ``.env`` is ever touched.
"""

from __future__ import annotations

import json
from typing import Any

from click.testing import CliRunner

from book_graph_rag import main as cli_module
from book_graph_rag.domain.namespaces import SourceNamespace
from book_graph_rag.domain.routing_models import (
    ResolvedRoute,
    RouteDecision,
    ScoredCandidate,
)

QUESTION = "¿qué es un router de consultas?"


def _namespace(source: str) -> SourceNamespace:
    return SourceNamespace(corpus="knowledge", source=source)


class _FakeCaller:
    """Stand-in for ``RoutingCaller``; records whether it was closed."""

    def __init__(
        self,
        *,
        result: ResolvedRoute | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.closed = False

    async def route_for_caller(self, question: str) -> ResolvedRoute | None:
        assert question == QUESTION
        if self.error is not None:
            raise self.error
        return self.result

    async def close(self) -> None:
        self.closed = True


class _FakeSettings:
    """Minimal stand-in so the command never reads the ambient environment."""

    @staticmethod
    def model_validate(_payload: dict[str, Any]) -> _FakeSettings:
        return _FakeSettings()


def _install(monkeypatch: Any, caller: _FakeCaller) -> None:
    def _factory(_settings: object) -> _FakeCaller:
        return caller

    monkeypatch.setattr(cli_module, "build_routing_caller", _factory)
    monkeypatch.setattr(cli_module, "Settings", _FakeSettings)


def _invoke() -> Any:
    return CliRunner().invoke(cli_module.cli, ["route", QUESTION])


def _payload(result: Any) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(result.output)
    return parsed


def _error_text(result: Any) -> str:
    try:
        return str(result.stderr)
    except ValueError:  # click < 8.2 without a separate stderr stream
        return str(result.output)


def test_disabled_router_is_reported_and_not_confused_with_abstention(
    monkeypatch: Any,
) -> None:
    caller = _FakeCaller(result=None)
    _install(monkeypatch, caller)

    result = _invoke()

    assert result.exit_code == 0
    assert _payload(result) == {
        "candidates": [],
        "enabled": False,
        "fanout_namespaces": [],
        "margin": None,
        "question": QUESTION,
        "reason": None,
        "route_kind": None,
        "top_score": None,
        "validated_namespace": None,
    }
    assert caller.closed is True


def test_single_route_exposes_the_validated_namespace(monkeypatch: Any) -> None:
    chosen = _namespace("graphrag-agentic")
    decision = RouteDecision(
        selected=chosen,
        candidates=(
            ScoredCandidate(namespace=chosen, score=0.42),
            ScoredCandidate(namespace=_namespace("ai-engineering-huyen"), score=0.12),
            ScoredCandidate(namespace=_namespace("agentic-architectural-patterns"), score=0.05),
            ScoredCandidate(namespace=_namespace("fourth-book"), score=0.01),
        ),
        top_score=0.42,
        margin=0.30,
        confidence=0.58,
        route_kind="single",
        reason="clear",
    )
    caller = _FakeCaller(result=ResolvedRoute(decision=decision, validated_namespace=chosen))
    _install(monkeypatch, caller)

    result = _invoke()

    assert result.exit_code == 0
    payload = _payload(result)
    assert payload["enabled"] is True
    assert payload["route_kind"] == "single"
    assert payload["validated_namespace"] == "knowledge:graphrag-agentic"
    assert payload["fanout_namespaces"] == []
    assert payload["top_score"] == 0.42
    assert payload["margin"] == 0.3
    assert payload["reason"] == "clear"
    assert payload["candidates"] == [
        {"namespace": "knowledge:graphrag-agentic", "score": 0.42},
        {"namespace": "knowledge:ai-engineering-huyen", "score": 0.12},
        {"namespace": "knowledge:agentic-architectural-patterns", "score": 0.05},
    ]
    assert caller.closed is True


def test_abstention_is_data_not_a_failure(monkeypatch: Any) -> None:
    decision = RouteDecision(
        candidates=(ScoredCandidate(namespace=_namespace("graphrag-agentic"), score=0.03),),
        top_score=0.03,
        margin=0.03,
        route_kind="abstain",
        reason="low_score",
    )
    caller = _FakeCaller(result=ResolvedRoute(decision=decision))
    _install(monkeypatch, caller)

    result = _invoke()

    assert result.exit_code == 0
    payload = _payload(result)
    assert payload["enabled"] is True
    assert payload["validated_namespace"] is None
    assert payload["reason"] == "low_score"
    assert payload["route_kind"] == "abstain"
    assert caller.closed is True


def test_ambiguity_fans_out_to_both_validated_namespaces(monkeypatch: Any) -> None:
    first = _namespace("graphrag-agentic")
    second = _namespace("ai-engineering-huyen")
    decision = RouteDecision(
        candidates=(
            ScoredCandidate(namespace=first, score=0.21),
            ScoredCandidate(namespace=second, score=0.20),
        ),
        top_score=0.21,
        margin=0.01,
        route_kind="multi",
        reason="low_margin",
    )
    caller = _FakeCaller(
        result=ResolvedRoute(
            decision=decision,
            fanout_namespaces=(first, second),
        )
    )
    _install(monkeypatch, caller)

    result = _invoke()

    assert result.exit_code == 0
    payload = _payload(result)
    assert payload["route_kind"] == "multi"
    assert payload["validated_namespace"] is None
    assert payload["fanout_namespaces"] == [
        "knowledge:graphrag-agentic",
        "knowledge:ai-engineering-huyen",
    ]
    assert caller.closed is True


def test_routing_failure_exits_non_zero_without_dumping_json(
    monkeypatch: Any,
) -> None:
    caller = _FakeCaller(error=RuntimeError("profile artifact is unreadable"))
    _install(monkeypatch, caller)

    result = _invoke()

    assert result.exit_code == 1
    assert "profile artifact is unreadable" in _error_text(result)
    assert caller.closed is True


def test_configuration_failure_exits_non_zero_before_building_the_caller(
    monkeypatch: Any,
) -> None:
    def _broken(_payload: dict[str, Any]) -> _FakeSettings:
        raise ValueError("NEO4J_URI is not set")

    monkeypatch.setattr(
        cli_module,
        "Settings",
        type("_Broken", (), {"model_validate": staticmethod(_broken)}),
    )
    monkeypatch.setattr(
        cli_module,
        "build_routing_caller",
        lambda _settings: (_ for _ in ()).throw(AssertionError("must not compose")),
    )

    result = _invoke()

    assert result.exit_code == 1
    assert "NEO4J_URI is not set" in _error_text(result)
