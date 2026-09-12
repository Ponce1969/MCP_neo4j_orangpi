"""Tests for MCP hardening domain security contracts (T-A.1 + T-A.2).

Covers ToolRiskTier, ScopeContext, ResourcePolicy, QueryFingerprint,
and typed security errors.
"""

import json
from typing import Any

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
    canonical_json,
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


def test_resource_policy_defaults_are_secure() -> None:
    """Default policies are positive, bounded, and tighten with risk."""
    low = ResourcePolicy.default_for(ToolRiskTier.LOW)
    medium = ResourcePolicy.default_for(ToolRiskTier.MEDIUM)
    high = ResourcePolicy.default_for(ToolRiskTier.HIGH)

    for policy in (low, medium, high):
        assert policy.timeout_ms > 0
        assert policy.max_rows > 0
        assert policy.max_nodes >= policy.max_rows
        assert policy.max_traversal_depth >= 0
        assert policy.max_query_retries >= 0
        assert policy.concurrency_limit > 0
        assert policy.rate_limit_calls > 0
        assert policy.rate_limit_window_ms > 0

    assert low.timeout_ms <= medium.timeout_ms
    assert high.timeout_ms <= medium.timeout_ms
    assert high.concurrency_limit <= medium.concurrency_limit <= low.concurrency_limit
    assert high.max_rows <= medium.max_rows <= low.max_rows


def test_query_fingerprint_basic() -> None:
    """Fingerprints are deterministic HMAC-SHA256 over a canonical payload."""
    fp = QueryFingerprint.from_canonical(
        key_id="dev-1",
        secret=b"secret-key",
        payload={"query": "MATCH (n) RETURN n", "scope": "book:agentic-patterns"},
    )
    assert fp.key_id == "dev-1"
    assert fp.algorithm == "hmac-sha256"
    assert len(fp.fingerprint_hex) == 64
    fp2 = QueryFingerprint.from_canonical(
        key_id="dev-1",
        secret=b"secret-key",
        payload={"scope": "book:agentic-patterns", "query": "MATCH (n) RETURN n"},
    )
    assert fp.fingerprint_hex == fp2.fingerprint_hex


def test_query_fingerprint_differentiates_keys_and_payload() -> None:
    """Different keys or payloads produce different fingerprints."""
    payload: dict[str, Any] = {"query": "MATCH (n) RETURN n"}
    fp_a = QueryFingerprint.from_canonical("k", b"key-a", payload)
    fp_b = QueryFingerprint.from_canonical("k", b"key-b", payload)
    fp_c = QueryFingerprint.from_canonical("k", b"key-a", {**payload, "extra": 1})
    assert fp_a.fingerprint_hex != fp_b.fingerprint_hex
    assert fp_a.fingerprint_hex != fp_c.fingerprint_hex


def test_query_fingerprint_from_canonical_rejects_empty_key_id() -> None:
    """from_canonical also rejects an empty key_id."""
    with pytest.raises(ValidationError, match="key_id"):
        QueryFingerprint.from_canonical(key_id="", secret=b"x", payload={"q": "x"})


def test_canonical_json_sorts_and_compacts() -> None:
    """canonical_json is deterministic and whitespace-free."""
    a = canonical_json({"z": 1, "a": [3, 1, 2], "s": {"b", "a"}})
    b = canonical_json({"a": [3, 1, 2], "z": 1, "s": {"b", "a"}})
    assert a == b
    parsed = json.loads(a)
    assert parsed == {"a": [3, 1, 2], "s": ["a", "b"], "z": 1}


def test_scope_context_canonical_serialization() -> None:
    """ScopeContext serializes to a canonical key independent of input ordering."""
    scope1 = ScopeContext(
        source=SourceNamespace(corpus="book", source="agentic-patterns"),
        book_ids=("z", "a"),
        entity_types=("pattern", "agent"),
    )
    scope2 = ScopeContext(
        source=SourceNamespace(corpus="book", source="agentic-patterns"),
        book_ids=("a", "z"),
        entity_types=("agent", "pattern"),
    )
    assert scope1.to_canonical_key() == scope2.to_canonical_key()
    assert "book:agentic-patterns" in scope1.to_canonical_key()


def test_resource_policy_canonical_serialization() -> None:
    """ResourcePolicy serializes deterministically for tier comparison and audit."""
    policy = ResourcePolicy.default_for(ToolRiskTier.MEDIUM)
    raw = json.loads(policy.to_canonical_json())
    assert raw["tier"] == "medium"
    assert raw["max_rows"] == policy.max_rows
