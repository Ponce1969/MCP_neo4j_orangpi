"""Behavior tests for the caller-facing router adapter (router Unit A).

The contract under test: ``route_for_caller`` returns ``None`` exactly when the
``router_enabled`` flag is off (callers keep the existing explicit-scope
path); with the flag on it returns the use case outcome, including an
abstaining ``ResolvedRoute`` (never conflated with the off-flag ``None``).
"""

from __future__ import annotations

import pytest

from book_graph_rag.config import Settings
from book_graph_rag.domain.namespaces import SourceNamespace
from book_graph_rag.domain.routing_models import (
    ResolvedRoute,
    RouteDecision,
    hmac_query_fingerprint,
)
from book_graph_rag.infrastructure.routing_caller import (
    RoutingCaller,
    RoutingTelemetryError,
    build_routing_caller,
    build_routing_event,
    fingerprint_query_for_telemetry,
)
from book_graph_rag.ports.routing_telemetry_port import (
    CacheEntry,
    RoutingEvent,
    RoutingTelemetryPort,
)


class _StubExecutor:
    """Fake router executor: records calls and returns a configured route."""

    def __init__(self, route: ResolvedRoute) -> None:
        self.route = route
        self.calls: list[str] = []

    async def execute(self, question: str) -> ResolvedRoute:
        self.calls.append(question)
        return self.route


class _StubTelemetry(RoutingTelemetryPort):
    """Fake telemetry port that only records ``close()``."""

    def __init__(self) -> None:
        self.closed = False

    async def put_cache(self, entry: CacheEntry) -> None:
        return None

    async def get_cache(self, key: str) -> CacheEntry | None:
        return None

    async def record_event(self, event: RoutingEvent) -> None:
        return None

    async def list_events(
        self,
        *,
        limit: int = 100,
        golden_eligible_only: bool = False,
    ) -> tuple[RoutingEvent, ...]:
        return ()

    async def close(self) -> None:
        self.closed = True


def _namespace(namespace_id: str) -> SourceNamespace:
    corpus, source = namespace_id.split(":", maxsplit=1)
    return SourceNamespace(corpus=corpus, source=source)


def _single_route() -> ResolvedRoute:
    namespace = _namespace("knowledge:essential-graphrag")
    return ResolvedRoute(
        decision=RouteDecision(
            selected=namespace,
            candidates=(),
            top_score=0.42,
            margin=0.10,
            confidence=0.42,
            route_kind="single",
            reason="clear",
        ),
        validated_namespace=namespace,
    )


def _abstaining_route() -> ResolvedRoute:
    return ResolvedRoute(
        decision=RouteDecision(
            selected=None,
            candidates=(),
            top_score=0.03,
            margin=0.0,
            confidence=0.0,
            route_kind="abstain",
            reason="low_score",
        ),
        validated_namespace=None,
        fanout_namespaces=(),
    )


# ── flag-off short-circuit ────────────────────────────────────────────────────


async def test_route_for_caller_returns_none_when_disabled() -> None:
    executor = _StubExecutor(_single_route())
    caller = RoutingCaller(executor, enabled=False)

    result = await caller.route_for_caller("what is a tool?")

    assert result is None
    assert executor.calls == []


async def test_route_for_caller_does_not_touch_telemetry_or_models_when_disabled() -> None:
    executor = _StubExecutor(_single_route())
    telemetry = _StubTelemetry()
    caller = RoutingCaller(executor, enabled=False, telemetry=telemetry)

    assert await caller.route_for_caller("anything") is None
    assert telemetry.closed is False


# ── flag-on delegation ────────────────────────────────────────────────────────


async def test_route_for_caller_delegates_when_enabled() -> None:
    executor = _StubExecutor(_single_route())
    caller = RoutingCaller(executor, enabled=True)

    result = await caller.route_for_caller("what is a tool?")

    assert result == _single_route()
    assert executor.calls == ["what is a tool?"]


async def test_abstaining_route_is_not_none() -> None:
    """An abstention is a real outcome; ``None`` means flag-off, never abstain."""
    executor = _StubExecutor(_abstaining_route())
    caller = RoutingCaller(executor, enabled=True)

    result = await caller.route_for_caller("unclear question")

    assert result is not None
    assert result.validated_namespace is None
    assert result.decision.route_kind == "abstain"


# ── composition ───────────────────────────────────────────────────────────────


async def test_close_releases_composed_telemetry() -> None:
    telemetry = _StubTelemetry()
    caller = RoutingCaller(_StubExecutor(_single_route()), enabled=True, telemetry=telemetry)

    await caller.close()

    assert telemetry.closed is True


def test_enabled_property_reflects_flag() -> None:
    assert RoutingCaller(_StubExecutor(_single_route()), enabled=False).enabled is False
    assert RoutingCaller(_StubExecutor(_single_route()), enabled=True).enabled is True


def test_build_routing_caller_honors_settings_flag() -> None:
    settings = Settings.model_validate(
        {
            "neo4j_uri": "bolt://localhost:7687",
            "neo4j_user": "neo4j",
            "neo4j_password": "secret",
            "router_enabled": True,
            "router_telemetry_enabled": False,
        }
    )

    caller = build_routing_caller(settings)

    assert caller.enabled is True


def test_build_routing_caller_flag_off_by_default() -> None:
    settings = Settings.model_validate(
        {
            "neo4j_uri": "bolt://localhost:7687",
            "neo4j_user": "neo4j",
            "neo4j_password": "secret",
        }
    )

    caller = build_routing_caller(settings)

    assert caller.enabled is False


# ── telemetry fingerprinting (Unit B: keyed HMAC, no placeholders) ───────────


def test_hmac_query_fingerprint_is_deterministic_and_keyed() -> None:
    first = hmac_query_fingerprint("what is a tool?", key_id="router-telemetry-v1", secret="k1")
    again = hmac_query_fingerprint("what is a tool?", key_id="router-telemetry-v1", secret="k1")
    other_key = hmac_query_fingerprint("what is a tool?", key_id="router-telemetry-v1", secret="k2")
    other_question = hmac_query_fingerprint(
        "what is a memory?", key_id="router-telemetry-v1", secret="k1"
    )

    assert first == again
    assert len(first) == 64
    assert set(first) <= set("0123456789abcdef")
    assert other_key != first
    assert other_question != first


def test_fingerprint_query_for_telemetry_fails_fast_without_key() -> None:
    settings = Settings.model_validate(
        {
            "neo4j_uri": "bolt://localhost:7687",
            "neo4j_user": "neo4j",
            "neo4j_password": "secret",
        }
    )

    with pytest.raises(RoutingTelemetryError):
        fingerprint_query_for_telemetry("what is a tool?", settings)


def test_build_routing_caller_fails_fast_when_telemetry_enabled_without_key() -> None:
    settings = Settings.model_validate(
        {
            "neo4j_uri": "bolt://localhost:7687",
            "neo4j_user": "neo4j",
            "neo4j_password": "secret",
            "router_telemetry_enabled": True,
        }
    )

    with pytest.raises(RoutingTelemetryError):
        build_routing_caller(settings)


def test_build_routing_event_uses_keyed_fingerprint_and_never_raw_question() -> None:
    settings = Settings.model_validate(
        {
            "neo4j_uri": "bolt://localhost:7687",
            "neo4j_user": "neo4j",
            "neo4j_password": "secret",
            "router_telemetry_hmac_key": "very-secret",
        }
    )

    event = build_routing_event("what is a tool?", _single_route(), settings, latency_ms=12.5)

    expected = hmac_query_fingerprint(
        "what is a tool?",
        key_id="router-telemetry-v1",
        secret="very-secret",
    )
    assert event.query_fingerprint == expected
    assert "what is a tool?" not in event.model_dump_json()
    assert event.predicted_namespace == "knowledge:essential-graphrag"
    assert event.latency_ms == 12.5


def test_build_routing_event_enforces_key_even_when_constructed_directly() -> None:
    settings = Settings.model_validate(
        {
            "neo4j_uri": "bolt://localhost:7687",
            "neo4j_user": "neo4j",
            "neo4j_password": "secret",
        }
    )

    with pytest.raises(RoutingTelemetryError):
        build_routing_event("what is a tool?", _single_route(), settings, latency_ms=1.0)
