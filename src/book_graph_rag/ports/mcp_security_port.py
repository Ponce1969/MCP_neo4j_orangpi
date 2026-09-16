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

from book_graph_rag.domain.mcp_security import ScopeProof, ToolRiskTier


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


@dataclass(frozen=True)
class StructuralValidationResult:
    """Structured outcome of a structural validation (T-E.2).

    ``explain_required`` is always ``True`` for the dynamic-query path: execution
    requires ``EXPLAIN`` first (05 §7, 07 R3). ``scope_bound`` is ``True`` only when
    the query carries at least one proven scope predicate. ``scope_proofs`` lists each
    proven scope predicate in source order.
    """

    explain_required: bool
    scope_bound: bool
    scope_proofs: tuple[ScopeProof, ...]


class StructuralCypherValidator(abc.ABC):
    """Structural allowlist validator for dynamic Cypher (T-E.1 / T-E.2).

    This port is the security decision for the dynamic-query path. Implementations
    parse a query into a restricted, structured IR and reject anything outside the
    approved read-only subset. They never rely on regex denylists: unknown or
    unparseable input must fail closed via ``StructuralPolicyViolationError``.
    """

    @abc.abstractmethod
    def validate(
        self,
        query: str,
        *,
        require_scope_proof: bool = False,
    ) -> StructuralValidationResult:
        """Validate ``query`` against the structural allowlist.

        Raises ``StructuralPolicyViolationError`` when the query cannot be proven
        to lie inside the approved subset (writes, procedures, subqueries,
        unbounded paths, dynamic labels/types, literal injection points, or any
        input the parser cannot prove). Returns a
        :class:`StructuralValidationResult` on success.

        When ``require_scope_proof`` is ``True`` the query must additionally carry
        at least one WHERE predicate that proves a scope binding; a query with no
        scope predicate is rejected.
        """
        ...

    @abc.abstractmethod
    def require_explain(self, query: str, *, explain_applied: bool) -> None:
        """Enforce the EXPLAIN-before-execute gate (05 §7, 07 R3).

        The dynamic-query path must run ``EXPLAIN`` before execution. This check is
        distinct from structural validation: it rejects ``query`` when
        ``explain_applied`` is ``False``.
        """
        ...
