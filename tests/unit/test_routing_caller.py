"""Behavior tests for the caller-facing router adapter (router Unit A).

The contract under test: ``route_for_caller`` returns ``None`` exactly when the
``router_enabled`` flag is off (callers keep the existing explicit-scope
path); with the flag on it returns the use case outcome, including an
abstaining ``ResolvedRoute`` (never conflated with the off-flag ``None``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from book_graph_rag.application.route_question_use_case import RouteQuestionUseCase
from book_graph_rag.config import Settings
from book_graph_rag.domain.namespaces import SourceNamespace
from book_graph_rag.domain.routing_models import (
    ResolvedRoute,
    RouteDecision,
    hmac_query_fingerprint,
)
from book_graph_rag.infrastructure.json_namespace_profile_store import (
    NamespaceProfileStoreError,
)
from book_graph_rag.infrastructure.routing_caller import (
    RoutingCaller,
    RoutingTelemetryError,
    build_route_question_use_case,
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


def test_build_routing_caller_honors_settings_flag(tmp_path: Path) -> None:
    settings = Settings.model_validate(
        {
            "neo4j_uri": "bolt://localhost:7687",
            "neo4j_user": "neo4j",
            "neo4j_password": "secret",
            "router_enabled": True,
            "router_telemetry_enabled": False,
            # Absent artifact: composition (S4) only fails on a *stale* one.
            "namespace_profile_store_path": str(tmp_path / "absent-profiles.json"),
        }
    )

    caller = build_routing_caller(settings)

    assert caller.enabled is True


def test_build_routing_caller_flag_off_by_default(tmp_path: Path) -> None:
    # The declared default, independent of any ambient .env a developer may keep.
    assert Settings.model_fields["router_enabled"].default is False

    settings = Settings.model_validate(
        {
            "neo4j_uri": "bolt://localhost:7687",
            "neo4j_user": "neo4j",
            "neo4j_password": "secret",
            "router_enabled": False,
            "router_telemetry_enabled": False,
            "namespace_profile_store_path": str(tmp_path / "absent-profiles.json"),
        }
    )

    caller = build_routing_caller(settings)

    assert caller.enabled is False


# ── S4: stale-artifact gates run at composition, not at first question ───────


def _composition_settings(artifact: Path) -> Settings:
    """Settings pinned explicitly so no ambient ``.env`` can move the gates."""
    return Settings.model_validate(
        {
            "neo4j_uri": "bolt://localhost:7687",
            "neo4j_user": "neo4j",
            "neo4j_password": "secret",
            "router_enabled": False,
            "router_telemetry_enabled": False,
            "namespace_profile_store_path": str(artifact),
            "namespace_profile_version": "2.0.0",
            "embedding_model_id": "test-model",
            "allow_extra_embedding_model": True,
        }
    )


def _write_artifact(path: Path, entries: list[dict[str, Any]]) -> None:
    path.write_bytes(json.dumps(entries).encode("utf-8"))


def _artifact_entry(
    *,
    profile_version: str = "2.0.0",
    model_id: str = "test-model",
    with_prototypes: bool = True,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "namespace": {"corpus": "knowledge", "source": "book-a"},
        "centroid": [1.0, 0.0],
        "model_id": model_id,
        "dimension": 2,
        "profile_version": profile_version,
    }
    if with_prototypes:
        entry.update(
            {
                "prototypes": [[1.0, 0.0]],
                "k": 1,
                "prototype_sources": ["chunk:0"],
            }
        )
    return entry


def test_build_route_question_use_case_rejects_v1_artifact(tmp_path: Path) -> None:
    """A v1 artifact (no ``prototypes``) fails composition; no abstaining caller."""
    artifact = tmp_path / "profiles.json"
    _write_artifact(artifact, [_artifact_entry(with_prototypes=False, profile_version="1.0.0")])

    with pytest.raises(NamespaceProfileStoreError, match="shape v1") as excinfo:
        build_route_question_use_case(_composition_settings(artifact))

    message = str(excinfo.value)
    assert "missing field 'prototypes'" in message
    assert "profile_version 2.0.0" in message


def test_build_route_question_use_case_rejects_profile_version_mismatch(tmp_path: Path) -> None:
    """S4: a v2-shaped artifact built for another version names the field."""
    artifact = tmp_path / "profiles.json"
    _write_artifact(artifact, [_artifact_entry(profile_version="0.9.0")])

    with pytest.raises(NamespaceProfileStoreError, match="profile_version mismatch") as excinfo:
        build_route_question_use_case(_composition_settings(artifact))

    message = str(excinfo.value)
    assert "expected '2.0.0'" in message
    assert "got '0.9.0'" in message


def test_build_route_question_use_case_rejects_model_id_mismatch(tmp_path: Path) -> None:
    """S4: an artifact embedded with another model names the field."""
    artifact = tmp_path / "profiles.json"
    _write_artifact(artifact, [_artifact_entry(model_id="some-other-model")])

    with pytest.raises(NamespaceProfileStoreError, match="model_id mismatch") as excinfo:
        build_route_question_use_case(_composition_settings(artifact))

    message = str(excinfo.value)
    assert "expected 'test-model'" in message
    assert "got 'some-other-model'" in message


def test_build_route_question_use_case_composes_on_matching_v2_artifact(tmp_path: Path) -> None:
    """Positive control: the gates reject drift, not a healthy artifact."""
    artifact = tmp_path / "profiles.json"
    _write_artifact(artifact, [_artifact_entry()])

    use_case = build_route_question_use_case(_composition_settings(artifact))

    assert isinstance(use_case, RouteQuestionUseCase)


def test_build_route_question_use_case_composes_on_absent_artifact(tmp_path: Path) -> None:
    """Absence is not drift: no artifact yet still composes (abstains later)."""
    use_case = build_route_question_use_case(_composition_settings(tmp_path / "absent.json"))

    assert isinstance(use_case, RouteQuestionUseCase)


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
            "router_telemetry_hmac_key": "very-secret-para-tests-0123456789abcdef",
        }
    )

    event = build_routing_event("what is a tool?", _single_route(), settings, latency_ms=12.5)

    expected = hmac_query_fingerprint(
        "what is a tool?",
        key_id="router-telemetry-v1",
        secret="very-secret-para-tests-0123456789abcdef",
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


def test_a_short_router_telemetry_key_is_not_a_key() -> None:
    """The routing telemetry key gets the same floor as the other two secrets.

    It is the third secret this repo fingerprints with, and it was read with a bare
    truthiness test, so a one-character key passed while the other two refused.
    """
    from pydantic import SecretStr

    from book_graph_rag.config import Settings
    from book_graph_rag.infrastructure.routing_caller import (
        RoutingTelemetryError,
        fingerprint_query_for_telemetry,
    )

    settings = Settings(
        neo4j_uri="bolt://localhost:7687",
        neo4j_user="neo4j",
        neo4j_password=SecretStr("fake-password"),  # pragma: allowlist secret
        router_telemetry_hmac_key=SecretStr("corta"),
    )

    with pytest.raises(RoutingTelemetryError, match="router_telemetry_hmac_key"):
        fingerprint_query_for_telemetry("una pregunta", settings)
