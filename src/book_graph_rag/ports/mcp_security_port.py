"""Resource budget port: per-tier concurrency and rate limits (T-D.2).

Implementations must be deterministic and unit-testable: every clock read goes
through an injected :class:`MonotonicClock`; no sleeps or blocking inside the
accounting logic.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Protocol

from book_graph_rag.domain.mcp_security import ToolRiskTier


class MonotonicClock(Protocol):
    """Injectable monotonic time source for deterministic budget accounting."""

    def monotonic(self) -> float:
        ...


@dataclass(frozen=True)
class BudgetLease:
    """A granted in-flight slot that must be released when the request finishes."""

    tier: ToolRiskTier
    key: str
    token: int


class ResourceBudgetPort(abc.ABC):
    """Per-tier concurrency and rate budget contract.

    ``acquire`` reserves a slot for ``tier`` (optionally scoped to ``key`` such as a
    session id) and returns a :class:`BudgetLease`. Saturated concurrency raises
    ``ConcurrencyLimitExceededError``; a saturated rolling rate window raises
    ``RateLimitExceededError``. Both are ``ResourceExhaustedError`` subtypes, so
    exhaustion never bypasses authorization.
    """

    @abc.abstractmethod
    async def acquire(self, tier: ToolRiskTier, *, key: str = "") -> BudgetLease:
        """Reserve a slot for ``tier``/``key`` or raise a typed exhaustion error."""
        ...

    @abc.abstractmethod
    async def release(self, lease: BudgetLease) -> None:
        """Return the slot represented by ``lease``."""
        ...

    @asynccontextmanager
    async def budget(
        self, tier: ToolRiskTier, *, key: str = ""
    ) -> AsyncIterator[BudgetLease]:
        """Acquire a slot and guarantee its release."""
        lease = await self.acquire(tier, key=key)
        try:
            yield lease
        finally:
            await self.release(lease)
