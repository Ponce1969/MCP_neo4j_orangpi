"""Composition root and caller-facing surface for the namespace router.

Unit A of the caller integration moves the adapter wiring previously
duplicated in ``scripts/route_question.py`` behind one reusable factory and
adds a thin ``RoutingCaller`` adapter: ``route_for_caller`` returns ``None``
exactly while the ``router_enabled`` flag is off, so callers keep the existing
explicit-scope path unchanged. The MCP surface is untouched.
"""

from __future__ import annotations

from typing import Protocol

from book_graph_rag.application.route_question_use_case import RouteQuestionUseCase
from book_graph_rag.config import Settings
from book_graph_rag.domain.routing_models import (
    ResolvedRoute,
    hints_from_catalog,
    hmac_query_fingerprint,
)
from book_graph_rag.infrastructure.catalog_loader import CatalogLoader
from book_graph_rag.infrastructure.catalog_scope_resolver import CatalogScopeResolver
from book_graph_rag.infrastructure.json_namespace_profile_reader import (
    JsonNamespaceProfileReader,
)
from book_graph_rag.infrastructure.json_namespace_profile_store import (
    JsonNamespaceProfileStore,
)
from book_graph_rag.infrastructure.sentence_transformer_adapter import (
    SentenceTransformerAdapter,
)
from book_graph_rag.infrastructure.sqlite_routing_telemetry import (
    SqliteRoutingTelemetryAdapter,
)
from book_graph_rag.ports.routing_telemetry_port import (
    RoutingEvent,
    RoutingTelemetryPort,
)


class RouteExecutor(Protocol):
    """Minimal executor contract structurally satisfied by ``RouteQuestionUseCase``."""

    async def execute(self, question: str) -> ResolvedRoute:
        """Route ``question`` to a validated namespace, or abstain."""
        ...


def build_route_question_use_case(settings: Settings) -> RouteQuestionUseCase:
    """Compose the production router use case from ``settings``.

    The single composition point for the router's concrete adapters:
    catalog-backed scope resolver, local profile artifact, local
    sentence-transformer embeddings, and deterministic catalog hints.
    """
    catalog = CatalogLoader(settings.catalog_path)
    resolver = CatalogScopeResolver(catalog)
    return RouteQuestionUseCase(
        SentenceTransformerAdapter(settings),
        JsonNamespaceProfileReader(
            JsonNamespaceProfileStore(settings.namespace_profile_store_path)
        ),
        resolver,
        model_id=settings.embedding_model_id,
        lexical_hints=hints_from_catalog(catalog.load()),
    )


class RoutingTelemetryError(RuntimeError):
    """Raised when router telemetry would record without a keyed fingerprint."""


def fingerprint_query_for_telemetry(question: str, settings: Settings) -> str:
    """Return the keyed HMAC fingerprint for a routing telemetry event.

    Fails fast when ``router_telemetry_hmac_key`` is unset: the raw question
    is never stored and never hashed plainly.
    """
    secret = settings.router_telemetry_hmac_key.get_secret_value()
    if not secret:
        raise RoutingTelemetryError(
            "router_telemetry_hmac_key is empty or missing; refusing to record "
            "routing telemetry without a keyed fingerprint (raw questions are "
            "never stored plainly)."
        )
    return hmac_query_fingerprint(
        question,
        key_id=settings.router_telemetry_hmac_key_id,
        secret=secret,
    )


def build_routing_event(
    question: str,
    resolved: ResolvedRoute,
    settings: Settings,
    *,
    latency_ms: float,
    graph_snapshot: str = "",
) -> RoutingEvent:
    """Build the keyed telemetry event for one routing decision."""
    return RoutingEvent(
        query_fingerprint=fingerprint_query_for_telemetry(question, settings),
        predicted_namespace=(
            resolved.validated_namespace.source_id
            if resolved.validated_namespace is not None
            else None
        ),
        score=resolved.decision.top_score,
        margin=resolved.decision.margin,
        route_kind=resolved.decision.route_kind,
        model_id=settings.embedding_model_id,
        profile_version=settings.namespace_profile_version,
        catalog_version=str(CatalogLoader(settings.catalog_path).load().version),
        graph_snapshot=graph_snapshot,
        latency_ms=latency_ms,
    )


def build_routing_caller(settings: Settings) -> RoutingCaller:
    """Compose the caller adapter; telemetry is opt-in via ``router_telemetry_enabled``.

    Raises:
        RoutingTelemetryError: When telemetry is enabled without a configured
            HMAC key (fail closed; raw questions are never logged plainly).
    """
    if settings.router_telemetry_enabled and not (
        settings.router_telemetry_hmac_key.get_secret_value()
    ):
        raise RoutingTelemetryError(
            "router_telemetry_enabled requires router_telemetry_hmac_key "
            "(fail closed: routing telemetry fingerprints are keyed, never plain)."
        )
    telemetry = (
        SqliteRoutingTelemetryAdapter(settings.routing_telemetry_path, enable_raw=False)
        if settings.router_telemetry_enabled
        else None
    )
    return RoutingCaller(
        build_route_question_use_case(settings),
        enabled=settings.router_enabled,
        telemetry=telemetry,
    )


class RoutingCaller:
    """Caller-facing router adapter; ``None`` keeps the explicit-scope path.

    The router is a classifier, never an authorization mechanism: the MCP
    ``require_scope``/catalog boundary remains the security gate. While
    ``router_enabled`` is off, ``route_for_caller`` short-circuits without
    loading models, profiles, or telemetry.
    """

    def __init__(
        self,
        executor: RouteExecutor,
        *,
        enabled: bool,
        telemetry: RoutingTelemetryPort | None = None,
    ) -> None:
        self._executor = executor
        self._enabled = enabled
        self._telemetry = telemetry

    @property
    def enabled(self) -> bool:
        """Return whether routing is active for callers."""
        return self._enabled

    async def route_for_caller(self, question: str) -> ResolvedRoute | None:
        """Route ``question`` or return ``None`` while the flag is off.

        ``None`` means exactly one thing: the caller must keep the existing
        explicit-scope path. A returned ``ResolvedRoute`` may itself abstain
        (``validated_namespace is None``); never conflate the two.
        """
        if not self._enabled:
            return None
        return await self._executor.execute(question)

    async def close(self) -> None:
        """Release the telemetry adapter when one was composed."""
        if self._telemetry is not None:
            await self._telemetry.close()
