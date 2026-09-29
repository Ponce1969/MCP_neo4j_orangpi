"""Ports and value objects for namespace-routing telemetry and cache.

This surface is deliberately separate from ``QueryLoggerPort``/``QueryLogEntry``:
the MCP query log stays metadata-only and HMAC-fingerprinted, while routing
telemetry is an explicit, bounded, opt-in store for router decisions.
"""

from __future__ import annotations

import abc
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CorrectionLabel = Literal[
    "correct",
    "corrected",
    "wrong_namespace",
    "insufficient_answer",
    "wrong_evidence",
]

_GOLDEN_ELIGIBLE = frozenset(
    {"correct", "corrected", "wrong_namespace", "insufficient_answer", "wrong_evidence"}
)


class CacheEntry(BaseModel):
    """One cached routing decision bound to its producing context."""

    model_config = ConfigDict(frozen=True)

    key: str
    question_fingerprint: str
    predicted_namespace: str | None = None
    top_score: float = 0.0
    confidence: float = 0.0
    model_id: str
    profile_version: str
    catalog_version: str
    graph_snapshot: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime | None = None


class RoutingEvent(BaseModel):
    """One routing decision record for cache/telemetry and later goldens."""

    model_config = ConfigDict(frozen=True)

    query_fingerprint: str
    predicted_namespace: str | None = None
    corrected_namespace: str | None = None
    score: float = 0.0
    margin: float = 0.0
    route_kind: str
    fallback_used: bool = False
    model_id: str
    profile_version: str
    catalog_version: str
    graph_snapshot: str
    latency_ms: float = 0.0
    answer_reference: str | None = None
    evidence_ids: tuple[str, ...] = ()
    correction: CorrectionLabel | None = None
    raw_question: str | None = None
    raw_answer: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def is_golden_eligible(self) -> bool:
        """Return whether this event carries an explicit validation label."""
        return self.correction in _GOLDEN_ELIGIBLE


class RoutingTelemetryPort(abc.ABC):
    """Contract for the bounded routing cache/telemetry store."""

    @abc.abstractmethod
    async def put_cache(self, entry: CacheEntry) -> None:
        """Upsert ``entry`` by its bound key."""
        ...

    @abc.abstractmethod
    async def get_cache(self, key: str) -> CacheEntry | None:
        """Return the entry for ``key``, or ``None`` when absent or expired."""
        ...

    @abc.abstractmethod
    async def record_event(self, event: RoutingEvent) -> None:
        """Append ``event`` to the telemetry log."""
        ...

    @abc.abstractmethod
    async def list_events(
        self,
        *,
        limit: int = 100,
        golden_eligible_only: bool = False,
    ) -> tuple[RoutingEvent, ...]:
        """Return recent events, optionally restricted to golden-eligible ones."""
        ...

    @abc.abstractmethod
    async def close(self) -> None:
        """Release any resources held by the adapter."""
        ...
