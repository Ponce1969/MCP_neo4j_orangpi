"""Integration tests for per-tier budget exhaustion (T-D.3).

Prove typed exhaustion under load, no authorization weakening, and independent
tier budgets by composing the real scope resolver with the real budget adapter.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from book_graph_rag.domain.mcp_security import (
    ConcurrencyLimitExceededError,
    InvalidScopeError,
    RateLimitExceededError,
    ResourcePolicy,
    ToolRiskTier,
)
from book_graph_rag.infrastructure.catalog_loader import CatalogLoader
from book_graph_rag.infrastructure.catalog_scope_resolver import CatalogScopeResolver
from book_graph_rag.infrastructure.mcp_resource_budget_adapter import (
    InMemoryResourceBudgetAdapter,
)

_VALID_CATALOG = """\
version: 1
corpora:
  knowledge:
    label: "Knowledge Library"
    sources:
      agentic-architectural-patterns:
        label: "Agentic Architectural Patterns"
        file: "data/libro.pdf"
        status: active
"""

_VALID_SOURCE = "knowledge:agentic-architectural-patterns"


def _policy(
    *, tier: ToolRiskTier, concurrency_limit: int, rate_limit_calls: int
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
        rate_limit_window_ms=60_000,
    )


def _resolver(tmp_path: Path) -> CatalogScopeResolver:
    path = tmp_path / "catalog.yaml"
    path.write_text(_VALID_CATALOG, encoding="utf-8")
    return CatalogScopeResolver(CatalogLoader(path))


class _ScopedRequestPipeline:
    """Read pipeline: scope authorization first, then budget acquisition."""

    def __init__(
        self,
        scope_resolver: CatalogScopeResolver,
        budget: InMemoryResourceBudgetAdapter,
    ) -> None:
        self._scope_resolver = scope_resolver
        self._budget = budget
        self.scope_validations = 0

    async def run(self, source_id: str, *, tier: ToolRiskTier) -> object:
        self.scope_validations += 1
        scope = self._scope_resolver.resolve(source_id)  # raises InvalidScopeError
        lease = await self._budget.acquire(tier)
        try:
            return scope
        finally:
            await self._budget.release(lease)


async def test_concurrency_exhaustion_under_load_is_typed() -> None:
    """Concurrent acquires saturate at the limit and reject with a typed error."""
    budget = InMemoryResourceBudgetAdapter(
        {
            ToolRiskTier.MEDIUM: _policy(
                tier=ToolRiskTier.MEDIUM, concurrency_limit=2, rate_limit_calls=100
            )
        }
    )
    results = await asyncio.gather(
        *(budget.acquire(ToolRiskTier.MEDIUM) for _ in range(8)),
        return_exceptions=True,
    )
    grants = [r for r in results if not isinstance(r, BaseException)]
    rejections = [r for r in results if isinstance(r, BaseException)]

    assert len(grants) == 2
    assert len(rejections) == 6
    assert all(isinstance(r, ConcurrencyLimitExceededError) for r in rejections)

    for lease in grants:
        await budget.release(lease)


async def test_rate_exhaustion_under_load_is_typed() -> None:
    """A burst beyond the rolling-window budget rejects with a typed rate error."""
    budget = InMemoryResourceBudgetAdapter(
        {
            ToolRiskTier.HIGH: _policy(
                tier=ToolRiskTier.HIGH, concurrency_limit=10, rate_limit_calls=3
            )
        }
    )
    results = await asyncio.gather(
        *(budget.acquire(ToolRiskTier.HIGH) for _ in range(5)),
        return_exceptions=True,
    )
    grants = [r for r in results if not isinstance(r, BaseException)]
    rejections = [r for r in results if isinstance(r, BaseException)]

    assert len(grants) == 3
    assert len(rejections) == 2
    assert all(isinstance(r, RateLimitExceededError) for r in rejections)


async def test_invalid_scope_never_consumes_budget(tmp_path: Path) -> None:
    """A rejected scope request fails authorization without consuming a slot."""
    budget = InMemoryResourceBudgetAdapter(
        {
            ToolRiskTier.LOW: _policy(
                tier=ToolRiskTier.LOW, concurrency_limit=1, rate_limit_calls=100
            )
        }
    )
    pipeline = _ScopedRequestPipeline(_resolver(tmp_path), budget)

    with pytest.raises(InvalidScopeError, match="(?i)unknown.*source"):
        await pipeline.run("knowledge:missing-source", tier=ToolRiskTier.LOW)

    # The single slot is still available for an authorized request.
    await pipeline.run(_VALID_SOURCE, tier=ToolRiskTier.LOW)
    assert pipeline.scope_validations == 2


async def test_exhausted_budget_does_not_weaken_scope_validation(tmp_path: Path) -> None:
    """A full budget still validates scope first and rejects invalid scopes as scopes."""
    budget = InMemoryResourceBudgetAdapter(
        {
            ToolRiskTier.LOW: _policy(
                tier=ToolRiskTier.LOW, concurrency_limit=1, rate_limit_calls=100
            )
        }
    )
    pipeline = _ScopedRequestPipeline(_resolver(tmp_path), budget)

    blocker = await budget.acquire(ToolRiskTier.LOW)

    # Authorization runs first: an invalid scope raises the scope error even though
    # the budget is exhausted (never a budget error, never a pass-through).
    with pytest.raises(InvalidScopeError, match="(?i)unknown.*source"):
        await pipeline.run("knowledge:missing-source", tier=ToolRiskTier.LOW)
    assert pipeline.scope_validations == 1

    # A valid scope still passes authorization, then fails closed on the budget.
    with pytest.raises(ConcurrencyLimitExceededError, match="concurrency|in-flight"):
        await pipeline.run(_VALID_SOURCE, tier=ToolRiskTier.LOW)
    assert pipeline.scope_validations == 2

    await budget.release(blocker)


async def test_tiers_have_independent_budgets_under_exhaustion(tmp_path: Path) -> None:
    """Exhausting HIGH never blocks a LOW-tier request through the same pipeline."""
    budget = InMemoryResourceBudgetAdapter(
        {
            ToolRiskTier.HIGH: _policy(
                tier=ToolRiskTier.HIGH, concurrency_limit=1, rate_limit_calls=100
            ),
            ToolRiskTier.LOW: _policy(
                tier=ToolRiskTier.LOW, concurrency_limit=1, rate_limit_calls=100
            ),
        }
    )
    pipeline = _ScopedRequestPipeline(_resolver(tmp_path), budget)

    blocker = await budget.acquire(ToolRiskTier.HIGH)
    with pytest.raises(ConcurrencyLimitExceededError):
        await pipeline.run(_VALID_SOURCE, tier=ToolRiskTier.HIGH)

    await pipeline.run(_VALID_SOURCE, tier=ToolRiskTier.LOW)

    await budget.release(blocker)
