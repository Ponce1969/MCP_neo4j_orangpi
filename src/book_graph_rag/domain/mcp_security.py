"""Domain security contracts for the MCP hardening phase (Slice A, T-A.1).

These models are intentionally pure: they depend only on the Python stdlib and
Pydantic. No Neo4j, OpenAI, MCP or infrastructure references are allowed here.
"""

from __future__ import annotations

from enum import Enum, unique
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from book_graph_rag.domain.namespaces import SourceNamespace

# ── Typed security errors ────────────────────────────────────────────────────


class McpSecurityError(Exception):
    """Base class for MCP hardening security failures."""

    _error_code: str = "security_error"

    def __init__(
        self,
        message: str,
        *,
        scope_id: str | None = None,
        tier: str | None = None,
    ) -> None:
        super().__init__(message)
        self.scope_id = scope_id
        self.tier = tier

    @property
    def error_code(self) -> str:
        """Stable machine-readable code for typed handling."""
        return self._error_code


class InvalidScopeError(McpSecurityError):
    """Raised when a request lacks a valid, resolvable namespace scope."""

    _error_code = "invalid_scope"


class UnsupportedQueryError(McpSecurityError):
    """Raised when a dynamic query falls outside the allowlisted subset."""

    _error_code = "unsupported_query"


class PolicyViolationError(McpSecurityError):
    """Raised when a request violates an active security policy."""

    _error_code = "policy_violation"


class ResourceExhaustedError(McpSecurityError):
    """Raised when a request exhausts timeout, row, node, or budget limits."""

    _error_code = "resource_exhausted"


class QueryFingerprintError(McpSecurityError):
    """Raised when fingerprint canonicalization or validation fails."""

    _error_code = "query_fingerprint"


# ── Risk tiers ───────────────────────────────────────────────────────────────


@unique
class ToolRiskTier(str, Enum):  # noqa: UP042
    """Risk tier for MCP tools.

    Low: deterministic structured reads.
    Medium: LLM-mediated reads.
    High: dynamic query execution (disabled by default in the MCP boundary).
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    def __lt__(self, other: object) -> bool:
        """Total ordering: low < medium < high."""
        order = {ToolRiskTier.LOW: 0, ToolRiskTier.MEDIUM: 1, ToolRiskTier.HIGH: 2}
        if not isinstance(other, ToolRiskTier):
            return NotImplemented
        return order[self] < order[other]

    def __le__(self, other: object) -> bool:
        if not isinstance(other, ToolRiskTier):
            return NotImplemented
        return self == other or self < other

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, ToolRiskTier):
            return NotImplemented
        return not self <= other

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, ToolRiskTier):
            return NotImplemented
        return not self < other


# ── Resource policy ──────────────────────────────────────────────────────────


class ResourcePolicy(BaseModel):
    """Server-side resource limits for a tool tier.

    All limits are positive or zero and enforce fail-closed validation:
    ``max_nodes`` must be at least ``max_rows`` because a query cannot materialise
    more rows than graph nodes it is allowed to visit.
    """

    model_config = ConfigDict(frozen=True, strict=True)

    tier: ToolRiskTier
    timeout_ms: int = Field(gt=0)
    max_rows: int = Field(gt=0)
    max_nodes: int = Field(gt=0)
    max_traversal_depth: int = Field(ge=0)
    max_query_retries: int = Field(ge=0)
    concurrency_limit: int = Field(gt=0)
    rate_limit_calls: int = Field(gt=0)
    rate_limit_window_ms: int = Field(gt=0)

    @model_validator(mode="after")
    def _nodes_cover_rows(self) -> ResourcePolicy:
        if self.max_nodes < self.max_rows:
            raise ValueError("max_nodes must be >= max_rows")
        return self


# ── Scope context ────────────────────────────────────────────────────────────


class ScopeContext(BaseModel):
    """Validated namespace scope for a structured or dynamic request.

    The ``book_ids``, ``entity_types`` and ``relationship_types`` tuples are kept
    sorted and deduplicated so that equality is deterministic.
    """

    model_config = ConfigDict(frozen=True, strict=True)

    source: SourceNamespace
    book_ids: tuple[str, ...] = ()
    entity_types: tuple[str, ...] = ()
    relationship_types: tuple[str, ...] = ()

    @staticmethod
    def _canonical_tuple(values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not isinstance(v, str) or v == "" for v in values):
            raise ValueError("scope identifiers must be non-empty strings")
        return tuple(sorted(set(values)))

    @model_validator(mode="before")
    @classmethod
    def _canonicalize(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        for key in ("book_ids", "entity_types", "relationship_types"):
            if key in data:
                data[key] = cls._canonical_tuple(data[key])
        return data

    @model_validator(mode="after")
    def _source_parts_non_empty(self) -> ScopeContext:
        if not self.source.corpus or not self.source.source:
            raise ValueError("scope source corpus and source must be non-empty")
        return self


# ── Query fingerprint ────────────────────────────────────────────────────────


class QueryFingerprint(BaseModel):
    """Keyed HMAC-SHA256 fingerprint for a canonical query/prompt payload.

    Only ``key_id`` and the hex digest are stored; the secret and raw payload are
    not retained so logs can correlate requests without leaking text or keys.
    """

    model_config = ConfigDict(frozen=True, strict=True)

    key_id: str = Field(min_length=1)
    fingerprint_hex: str = Field(min_length=1)
    algorithm: str = "hmac-sha256"

    @field_validator("fingerprint_hex")
    @classmethod
    def _hex_lowercase(cls, value: str) -> str:
        if len(value) % 2 != 0 or not all(c in "0123456789abcdef" for c in value.lower()):
            raise ValueError("fingerprint_hex must be a lowercase hex string")
        return value.lower()
