"""In-memory implementation of the per-tier resource budget port (T-D.2)."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Mapping

from book_graph_rag.domain.mcp_security import (
    ConcurrencyLimitExceededError,
    RateLimitExceededError,
    ResourcePolicy,
    ToolRiskTier,
)
from book_graph_rag.ports.mcp_security_port import (
    BudgetLease,
    MonotonicClock,
    ResourceBudgetPort,
)


class SystemMonotonicClock:
    """Default monotonic clock backed by :func:`time.monotonic`."""

    def monotonic(self) -> float:
        return time.monotonic()


class InMemoryResourceBudgetAdapter(ResourceBudgetPort):
    """Per-(tier, key) concurrency and rate limiter with bounded in-process state.

    Concurrency is tracked as outstanding lease tokens; rate limiting uses a rolling
    window pruned on each acquire. Time comes from the injected :class:`MonotonicClock`
    (default :class:`SystemMonotonicClock`). ``acquire`` check-and-commits without
    awaiting, so it is atomic within a single event loop.
    """

    def __init__(
        self,
        policies: Mapping[ToolRiskTier, ResourcePolicy] | None = None,
        *,
        clock: MonotonicClock | None = None,
    ) -> None:
        resolved: dict[ToolRiskTier, ResourcePolicy] = dict(policies or {})
        self._policies: dict[ToolRiskTier, ResourcePolicy] = {
            tier: resolved.get(tier, ResourcePolicy.default_for(tier))
            for tier in ToolRiskTier
        }
        self._clock: MonotonicClock = clock if clock is not None else SystemMonotonicClock()
        self._active: dict[tuple[ToolRiskTier, str], set[int]] = {}
        self._rate_timestamps: dict[tuple[ToolRiskTier, str], deque[float]] = {}
        self._next_token = 0

    @staticmethod
    def _slot(tier: ToolRiskTier, key: str) -> tuple[ToolRiskTier, str]:
        return (tier, key)

    def _prune(
        self, tier: ToolRiskTier, key: str, now: float, window_seconds: float
    ) -> deque[float]:
        slot = self._slot(tier, key)
        timestamps = self._rate_timestamps.setdefault(slot, deque[float]())
        cutoff = now - window_seconds
        while timestamps and timestamps[0] <= cutoff:
            timestamps.popleft()
        return timestamps

    async def acquire(self, tier: ToolRiskTier, *, key: str = "") -> BudgetLease:
        policy = self._policies[tier]
        slot = self._slot(tier, key)
        now = self._clock.monotonic()

        active = self._active.setdefault(slot, set[int]())
        if len(active) >= policy.concurrency_limit:
            raise ConcurrencyLimitExceededError(
                f"Concurrency limit exceeded for tier {tier.value!r}: "
                f"{len(active)} in-flight, limit {policy.concurrency_limit}",
                tier=tier.value,
            )

        timestamps = self._prune(tier, key, now, policy.rate_limit_window_ms / 1000)
        if len(timestamps) >= policy.rate_limit_calls:
            raise RateLimitExceededError(
                f"Rate limit exceeded for tier {tier.value!r}: "
                f"{len(timestamps)} calls in window, limit {policy.rate_limit_calls}",
                tier=tier.value,
            )

        token = self._next_token
        self._next_token += 1
        active.add(token)
        timestamps.append(now)
        return BudgetLease(tier=tier, key=key, token=token)

    async def release(self, lease: BudgetLease) -> None:
        slot = self._slot(lease.tier, lease.key)
        active = self._active.get(slot)
        if active is None or lease.token not in active:
            raise ValueError(
                f"Unknown or already-released budget lease token {lease.token} "
                f"for tier {lease.tier.value!r}"
            )
        active.discard(lease.token)
        if not active:
            self._active.pop(slot, None)
