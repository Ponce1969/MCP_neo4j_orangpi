"""Scope-resolution contract for the enabled ``query_cypher`` tool (R8, T-2.7).

Plain pytest (no container): the dynamic path is exercised against a recording
``Text2CypherPort`` and an in-memory scope resolver. The disabled-by-default path
and the fail-closed scope boundary are asserted without touching Neo4j.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr

from book_graph_rag.domain.mcp_security import (
    InvalidScopeError,
    MissingScopeError,
    ScopeContext,
    ToolRiskTier,
)
from book_graph_rag.domain.namespaces import SourceNamespace
from book_graph_rag.infrastructure.mcp.mcp_server_adapter import McpServerAdapter
from book_graph_rag.ports.graph_query_port import GraphQueryPort
from book_graph_rag.ports.mcp_security_port import BudgetLease, ResourceBudgetPort
from book_graph_rag.ports.query_logger_port import QueryLoggerPort
from book_graph_rag.ports.scope_resolver_port import ScopeResolverPort
from book_graph_rag.ports.text2cypher_port import Text2CypherPort, Text2CypherResult


class _RecordingText2CypherPort(Text2CypherPort):
    def __init__(self) -> None:
        self.calls: list[tuple[str, ScopeContext | None]] = []

    async def generate_and_run(
        self, question: str, *, scope: ScopeContext | None = None
    ) -> Text2CypherResult:
        self.calls.append((question, scope))
        return Text2CypherResult(
            question=question,
            cypher="MATCH (c:Chunk) WHERE c.book_id = $book_id RETURN c LIMIT 100",
            rows=[],
            schema_source="hardcoded",
            retries=0,
        )


class _ExplodingText2CypherPort(Text2CypherPort):
    async def generate_and_run(
        self, question: str, *, scope: ScopeContext | None = None
    ) -> Text2CypherResult:
        raise AssertionError("text2cypher must never run")


class _FakeGraphQueryPort(GraphQueryPort):
    async def find_entity(self, name: str, entity_type: Any | None = None, **kw: Any) -> list[Any]:
        return []

    async def find_entities_batch(self, ids: list[str]) -> list[Any]:
        return []

    async def traverse_relationships(
        self, source_id: str, rel_type: Any | None = None, depth: int = 1, **kw: Any
    ) -> tuple[list[Any], list[Any]]:
        return [], []

    async def find_path(self, start_id: str, end_id: str, max_depth: int = 3) -> list[Any]:
        return []

    async def search_chunks(self, query: str, limit: int = 10, **kw: Any) -> list[Any]:
        return []

    async def count_entities(self, entity_type: Any | None = None, **kw: Any) -> int:
        return 0

    async def list_entities(self, cursor: int = 0, page_size: int = 50, **kw: Any) -> tuple[list[Any], int]:
        return [], 0

    async def ensure_indexes(self) -> None:
        pass


class _NoopQueryLogger(QueryLoggerPort):
    async def log_query(self, entry: Any) -> None:
        pass

    async def close(self) -> None:
        pass


class _FakeScopeResolver(ScopeResolverPort):
    def __init__(self, *, reject: frozenset[str] = frozenset()) -> None:
        self._reject = reject

    def resolve(
        self,
        source_id: str,
        *,
        book_ids: tuple[str, ...] = (),
        entity_types: tuple[str, ...] = (),
        relationship_types: tuple[str, ...] = (),
    ) -> ScopeContext:
        if source_id in self._reject:
            raise InvalidScopeError(f"unknown scope source {source_id!r}", scope_id=source_id)
        return ScopeContext(
            source=SourceNamespace(corpus="book", source="default"),
            book_ids=book_ids,
            entity_types=entity_types,
            relationship_types=relationship_types,
        )


class _RecordingBudget(ResourceBudgetPort):
    def __init__(self) -> None:
        self.acquire_calls: list[tuple[ToolRiskTier, str]] = []
        self.release_calls: list[BudgetLease] = []
        self._token = 0

    async def acquire(self, tier: ToolRiskTier, *, key: str = "") -> BudgetLease:
        self.acquire_calls.append((tier, key))
        lease = BudgetLease(tier=tier, key=key, token=self._token)
        self._token += 1
        return lease

    async def release(self, lease: BudgetLease) -> None:
        self.release_calls.append(lease)


def _adapter(
    *,
    text2cypher_port: Text2CypherPort,
    scope_resolver: ScopeResolverPort | None = None,
    budget: ResourceBudgetPort | None = None,
    enable: bool = False,
) -> McpServerAdapter:
    return McpServerAdapter(
        _FakeGraphQueryPort(),
        _NoopQueryLogger(),
        text2cypher_port,
        scope_resolver=scope_resolver,
        enable_query_cypher=enable,
        budget_port=budget,
        hmac_key_id="test-v1",
        hmac_key=SecretStr("test-hmac-secret"),
    )


async def test_query_cypher_disabled_returns_policy_violation_unchanged() -> None:
    adapter = _adapter(text2cypher_port=_ExplodingText2CypherPort(), enable=False)

    result = await adapter.query_cypher("what patterns mitigate security risks?")

    assert result["error_code"] == "policy_violation"
    assert result["cypher"] is None
    assert result["rows"] == []


async def test_query_cypher_enabled_missing_source_id_raises_before_budget() -> None:
    text2cypher = _RecordingText2CypherPort()
    budget = _RecordingBudget()
    adapter = _adapter(
        text2cypher_port=text2cypher,
        scope_resolver=_FakeScopeResolver(),
        budget=budget,
        enable=True,
    )

    with pytest.raises(MissingScopeError):
        await adapter.query_cypher("question")

    assert budget.acquire_calls == []
    assert text2cypher.calls == []


async def test_query_cypher_enabled_unresolvable_source_id_raises_before_budget() -> None:
    text2cypher = _RecordingText2CypherPort()
    budget = _RecordingBudget()
    adapter = _adapter(
        text2cypher_port=text2cypher,
        scope_resolver=_FakeScopeResolver(reject=frozenset({"unknown"})),
        budget=budget,
        enable=True,
    )

    with pytest.raises(InvalidScopeError):
        await adapter.query_cypher("question", source_id="unknown")

    assert budget.acquire_calls == []
    assert text2cypher.calls == []


async def test_query_cypher_enabled_resolves_scope_and_forwards_to_port() -> None:
    text2cypher = _RecordingText2CypherPort()
    budget = _RecordingBudget()
    adapter = _adapter(
        text2cypher_port=text2cypher,
        scope_resolver=_FakeScopeResolver(),
        budget=budget,
        enable=True,
    )

    await adapter.query_cypher(
        "find chunks", source_id="book:default", book_ids=["book:one", "book:two"]
    )

    assert text2cypher.calls == [
        (
            "find chunks",
            ScopeContext(
                source=SourceNamespace(corpus="book", source="default"),
                book_ids=("book:one", "book:two"),
            ),
        )
    ]
    assert budget.acquire_calls == [(ToolRiskTier.HIGH, "query_cypher")]
    assert len(budget.release_calls) == 1
