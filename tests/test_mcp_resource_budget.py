"""Deterministic unit tests for the per-tier resource budget port (T-D.2).

Covers the contract, typed exhaustion errors, per-tier/key budgets, and lease
lifecycle. All time is injected via ``_FakeClock``.
"""

from __future__ import annotations

import pytest

from book_graph_rag.domain.mcp_security import (
    ConcurrencyLimitExceededError,
    RateLimitExceededError,
    ResourceExhaustedError,
    ResourcePolicy,
    ToolRiskTier,
)
from book_graph_rag.infrastructure.mcp_resource_budget_adapter import (
    InMemoryResourceBudgetAdapter,
)
from book_graph_rag.ports.mcp_security_port import ResourceBudgetPort


class _FakeClock:
    """Deterministic monotonic clock advanced explicitly by tests."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def monotonic(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def _policy(
    *,
    tier: ToolRiskTier = ToolRiskTier.LOW,
    concurrency_limit: int,
    rate_limit_calls: int,
    rate_limit_window_ms: int = 60_000,
) -> ResourcePolicy:
    return ResourcePolicy(
        tier=tier,
        timeout_ms=1000,
        max_rows=10,
        max_nodes=10,
        max_traversal_depth=2,
        max_query_retries=0,
        concurrency_limit=concurrency_limit,
        rate_limit_calls=rate_limit_calls,
        rate_limit_window_ms=rate_limit_window_ms,
    )


def test_budget_errors_are_resource_exhausted_subtypes() -> None:
    """Concurrency and rate exhaustion are distinguishable ResourceExhaustedError types."""
    assert issubclass(ConcurrencyLimitExceededError, ResourceExhaustedError)
    assert issubclass(RateLimitExceededError, ResourceExhaustedError)
    assert ConcurrencyLimitExceededError("x").error_code == "concurrency_limit_exceeded"
    assert RateLimitExceededError("x").error_code == "rate_limit_exceeded"


def test_adapter_implements_resource_budget_port() -> None:
    """The in-memory adapter exposes the ResourceBudgetPort contract."""
    adapter = InMemoryResourceBudgetAdapter()
    assert isinstance(adapter, ResourceBudgetPort)


async def test_concurrency_limit_rejects_when_tier_is_full() -> None:
    """A saturated tier rejects new acquires with a typed error until a slot frees."""
    adapter = InMemoryResourceBudgetAdapter(
        {ToolRiskTier.LOW: _policy(concurrency_limit=2, rate_limit_calls=100)}
    )
    first = await adapter.acquire(ToolRiskTier.LOW)
    second = await adapter.acquire(ToolRiskTier.LOW)

    with pytest.raises(ConcurrencyLimitExceededError, match="concurrency|in-flight"):
        await adapter.acquire(ToolRiskTier.LOW)

    await adapter.release(first)
    third = await adapter.acquire(ToolRiskTier.LOW)

    await adapter.release(second)
    await adapter.release(third)


async def test_rate_limit_rejects_within_window_and_rolls_over() -> None:
    """Calls beyond the window budget fail, and succeed again once the window rolls."""
    clock = _FakeClock()
    adapter = InMemoryResourceBudgetAdapter(
        {
            ToolRiskTier.LOW: _policy(
                concurrency_limit=8,
                rate_limit_calls=2,
                rate_limit_window_ms=1000,
            )
        },
        clock=clock,
    )
    await adapter.acquire(ToolRiskTier.LOW)
    await adapter.acquire(ToolRiskTier.LOW)

    with pytest.raises(RateLimitExceededError, match="(?i)rate limit exceeded"):
        await adapter.acquire(ToolRiskTier.LOW)

    clock.advance(1.1)
    await adapter.acquire(ToolRiskTier.LOW)


async def test_tiers_have_independent_concurrency_budgets() -> None:
    """Saturating one tier never consumes another tier's concurrency capacity."""
    adapter = InMemoryResourceBudgetAdapter(
        {
            ToolRiskTier.HIGH: _policy(
                tier=ToolRiskTier.HIGH, concurrency_limit=1, rate_limit_calls=100
            ),
            ToolRiskTier.LOW: _policy(concurrency_limit=8, rate_limit_calls=100),
        }
    )
    high = await adapter.acquire(ToolRiskTier.HIGH)
    with pytest.raises(ConcurrencyLimitExceededError):
        await adapter.acquire(ToolRiskTier.HIGH)

    low = await adapter.acquire(ToolRiskTier.LOW)

    await adapter.release(high)
    await adapter.release(low)


async def test_tiers_have_independent_rate_budgets() -> None:
    """Saturating one tier's rate window never blocks another tier's rate budget."""
    clock = _FakeClock()
    adapter = InMemoryResourceBudgetAdapter(
        {
            ToolRiskTier.HIGH: _policy(
                tier=ToolRiskTier.HIGH, concurrency_limit=8, rate_limit_calls=1
            ),
            ToolRiskTier.LOW: _policy(concurrency_limit=8, rate_limit_calls=100),
        },
        clock=clock,
    )
    await adapter.acquire(ToolRiskTier.HIGH)
    with pytest.raises(RateLimitExceededError):
        await adapter.acquire(ToolRiskTier.HIGH)

    await adapter.acquire(ToolRiskTier.LOW)


async def test_default_policies_match_domain_tier_defaults() -> None:
    """The default adapter enforces the domain tier defaults behaviorally."""
    adapter = InMemoryResourceBudgetAdapter()

    high = await adapter.acquire(ToolRiskTier.HIGH)
    with pytest.raises(ConcurrencyLimitExceededError):
        await adapter.acquire(ToolRiskTier.HIGH)

    medium = [await adapter.acquire(ToolRiskTier.MEDIUM) for _ in range(4)]
    with pytest.raises(ConcurrencyLimitExceededError):
        await adapter.acquire(ToolRiskTier.MEDIUM)

    low = [await adapter.acquire(ToolRiskTier.LOW) for _ in range(8)]
    with pytest.raises(ConcurrencyLimitExceededError):
        await adapter.acquire(ToolRiskTier.LOW)

    await adapter.release(high)
    for lease in medium + low:
        await adapter.release(lease)


async def test_key_partitions_budget_per_session() -> None:
    """An optional key scopes the budget to a session, keeping sessions independent."""
    adapter = InMemoryResourceBudgetAdapter(
        {ToolRiskTier.LOW: _policy(concurrency_limit=1, rate_limit_calls=100)}
    )
    first = await adapter.acquire(ToolRiskTier.LOW, key="session-a")
    with pytest.raises(ConcurrencyLimitExceededError):
        await adapter.acquire(ToolRiskTier.LOW, key="session-a")

    second = await adapter.acquire(ToolRiskTier.LOW, key="session-b")

    await adapter.release(first)
    await adapter.release(second)


async def test_unknown_or_foreign_lease_is_rejected() -> None:
    """Double-releasing or releasing another adapter's lease is a programming error."""
    adapter_a = InMemoryResourceBudgetAdapter()
    adapter_b = InMemoryResourceBudgetAdapter()
    lease = await adapter_a.acquire(ToolRiskTier.LOW)
    await adapter_a.release(lease)

    with pytest.raises(ValueError, match="Unknown|released"):
        await adapter_a.release(lease)  # double release
    with pytest.raises(ValueError, match="Unknown|released"):
        await adapter_b.release(lease)  # foreign lease


async def test_context_manager_releases_slot_on_error() -> None:
    """The budget context manager guarantees release even when the body raises."""
    adapter = InMemoryResourceBudgetAdapter(
        {ToolRiskTier.LOW: _policy(concurrency_limit=1, rate_limit_calls=100)}
    )
    with pytest.raises(RuntimeError, match="boom"):
        async with adapter.budget(ToolRiskTier.LOW):
            raise RuntimeError("boom")

    lease = await adapter.acquire(ToolRiskTier.LOW)
    await adapter.release(lease)
