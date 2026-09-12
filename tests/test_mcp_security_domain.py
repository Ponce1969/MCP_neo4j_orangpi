"""Tests for MCP hardening domain security contracts (T-A.1).

Covers ToolRiskTier, ScopeContext, ResourcePolicy, QueryFingerprint,
and typed security errors.
"""

import pytest
from pydantic import ValidationError

from book_graph_rag.domain.mcp_security import (
    InvalidScopeError,
    McpSecurityError,
    PolicyViolationError,
    QueryFingerprint,
    ResourceExhaustedError,
    ResourcePolicy,
    ScopeContext,
    ToolRiskTier,
    UnsupportedQueryError,
)
from book_graph_rag.domain.namespaces import SourceNamespace


def test_tool_risk_tier_values_and_ordering() -> None:
    """Tiers are deterministic comparable values."""
    assert ToolRiskTier.LOW.value == "low"
    assert ToolRiskTier.MEDIUM.value == "medium"
    assert ToolRiskTier.HIGH.value == "high"
    assert ToolRiskTier.LOW < ToolRiskTier.MEDIUM < ToolRiskTier.HIGH


def test_scope_context_basic() -> None:
    """A scope context captures the namespace and ordered book id filters."""
    scope = ScopeContext(
        source=SourceNamespace(corpus="book", source="agentic-patterns"),
        book_ids=("book:agentic-patterns", "book:agentic-patterns:extra"),
    )
    assert scope.source.source_id == "book:agentic-patterns"
    assert scope.book_ids == ("book:agentic-patterns", "book:agentic-patterns:extra")
    assert scope.entity_types == ()
    assert scope.relationship_types == ()


def test_scope_context_sorts_and_dedupes_book_ids() -> None:
    """ScopeContext canonicalizes book ids so equality does not depend on input order."""
    scope = ScopeContext(
        source=SourceNamespace(corpus="book", source="agentic-patterns"),
        book_ids=("z", "a", "a", "m"),
    )
    assert scope.book_ids == ("a", "m", "z")


def test_scope_context_rejects_empty_book_id() -> None:
    """Empty book ids are not a valid scope proof."""
    with pytest.raises(ValidationError, match="book_ids"):
        ScopeContext(
            source=SourceNamespace(corpus="book", source="agentic-patterns"),
            book_ids=("",),
        )


def test_scope_context_rejects_empty_namespace_part() -> None:
    """ScopeContext rejects empty corpus or source."""
    with pytest.raises(ValidationError, match="corpus|source"):
        ScopeContext(
            source=SourceNamespace(corpus="", source="agentic-patterns"),
            book_ids=("book:agentic-patterns",),
        )


def test_scope_context_frozen_cannot_mutate() -> None:
    """ScopeContext is immutable to prevent accidental policy bypass."""
    scope = ScopeContext(
        source=SourceNamespace(corpus="book", source="agentic-patterns"),
        book_ids=("book:agentic-patterns",),
    )
    with pytest.raises(ValidationError, match="frozen"):
        scope.book_ids = ("other",)


def test_resource_policy_validation_rejects_zero_timeout() -> None:
    """Zero or negative resource limits fail closed at construction."""
    with pytest.raises(ValidationError, match="timeout_ms"):
        ResourcePolicy(
            tier=ToolRiskTier.LOW,
            timeout_ms=0,
            max_rows=10,
            max_nodes=10,
            max_traversal_depth=2,
            max_query_retries=0,
            concurrency_limit=1,
            rate_limit_calls=1,
            rate_limit_window_ms=1000,
        )


def test_resource_policy_validation_rejects_rows_gt_nodes() -> None:
    """A policy that returns more rows than nodes it may visit is incoherent."""
    with pytest.raises(ValidationError, match="max_nodes"):
        ResourcePolicy(
            tier=ToolRiskTier.LOW,
            timeout_ms=1000,
            max_rows=100,
            max_nodes=50,
            max_traversal_depth=2,
            max_query_retries=0,
            concurrency_limit=1,
            rate_limit_calls=1,
            rate_limit_window_ms=1000,
        )


def test_resource_policy_frozen_cannot_mutate() -> None:
    """ResourcePolicy is immutable so limits cannot be weakened at runtime."""
    policy = ResourcePolicy(
        tier=ToolRiskTier.LOW,
        timeout_ms=1000,
        max_rows=10,
        max_nodes=10,
        max_traversal_depth=2,
        max_query_retries=0,
        concurrency_limit=1,
        rate_limit_calls=1,
        rate_limit_window_ms=1000,
    )
    with pytest.raises(ValidationError, match="frozen"):
        policy.max_rows = 999_999


def test_query_fingerprint_rejects_empty_key_id() -> None:
    """A fingerprint without a key id cannot support key rotation or audit."""
    with pytest.raises(ValidationError, match="key_id"):
        QueryFingerprint(key_id="", fingerprint_hex="00")


def test_query_fingerprint_rejects_malformed_hex() -> None:
    """The model rejects non-hex fingerprints to preserve integrity."""
    with pytest.raises(ValidationError, match="fingerprint_hex"):
        QueryFingerprint(key_id="k", fingerprint_hex="not-hex")


def test_typed_security_errors_are_distinguishable() -> None:
    """Each security error carries a stable code for typed handling."""
    assert issubclass(InvalidScopeError, McpSecurityError)
    assert issubclass(UnsupportedQueryError, McpSecurityError)
    assert issubclass(PolicyViolationError, McpSecurityError)
    assert issubclass(ResourceExhaustedError, McpSecurityError)

    err = InvalidScopeError("bad scope", scope_id="book:other")
    assert err.error_code == "invalid_scope"
    assert err.scope_id == "book:other"
