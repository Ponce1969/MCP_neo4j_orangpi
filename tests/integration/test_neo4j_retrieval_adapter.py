"""Read-only contract test for Neo4jRetrievalAdapter (Slice B, T-B.8)."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio

from book_graph_rag.infrastructure.neo4j_query_adapter import Neo4jQueryAdapter
from book_graph_rag.infrastructure.neo4j_retrieval_adapter import Neo4jRetrievalAdapter

pytestmark = pytest.mark.neo4j_integration


@pytest_asyncio.fixture
async def retrieval_adapter(
    neo4j_settings: Any,
) -> AsyncGenerator[Neo4jRetrievalAdapter]:
    """Build a Neo4jRetrievalAdapter against the testcontainer."""
    adapter = Neo4jRetrievalAdapter(neo4j_settings)
    try:
        yield adapter
    finally:
        await adapter.close()


async def _count_nodes(query_adapter: Neo4jQueryAdapter) -> int:
    result = await query_adapter.execute_read("MATCH (n) RETURN count(n) AS c")
    return int(result[0]["c"])


@pytest.mark.neo4j_integration
async def test_read_only_against_testcontainers(
    retrieval_adapter: Neo4jRetrievalAdapter,
) -> None:
    """No graph mutation occurs after using the read-only adapter."""
    before = await _count_nodes(retrieval_adapter._query_adapter)

    _ = await retrieval_adapter.fetch_contexts(
        question="what is MCP?",
        qtype="local",
        detail_level=1,
    )

    after = await _count_nodes(retrieval_adapter._query_adapter)
    assert before == after


@pytest.mark.neo4j_integration
async def test_fetch_contexts_global_vs_local(
    retrieval_adapter: Neo4jRetrievalAdapter,
) -> None:
    """fetch_contexts returns a tuple for both global and local question types."""
    global_ctxs = await retrieval_adapter.fetch_contexts(
        question="what is MCP?",
        qtype="global",
        detail_level=1,
    )
    assert isinstance(global_ctxs, tuple)

    local_ctxs = await retrieval_adapter.fetch_contexts(
        question="what is MCP?",
        qtype="local",
        detail_level=1,
    )
    assert isinstance(local_ctxs, tuple)


class _FakeProxyAnswer:
    answer = "MCP is a protocol for context exchange."


@pytest.mark.neo4j_integration
async def test_compose_answer_with_stub_llm(
    retrieval_adapter: Neo4jRetrievalAdapter,
) -> None:
    """compose_answer uses the injected LLM and returns a string answer."""

    async def fake_create(*args: Any, **kwargs: Any) -> _FakeProxyAnswer:
        return _FakeProxyAnswer()

    # Monkeypatch the private query client create for the test.
    retrieval_adapter._llm_adapter._query_client.chat.completions.create = fake_create  # type: ignore[method-assign]  # noqa: SLF001

    answer = await retrieval_adapter.compose_answer(
        question="what is MCP?",
        contexts=("MCP is a protocol.",),
    )
    assert answer == "MCP is a protocol for context exchange."
