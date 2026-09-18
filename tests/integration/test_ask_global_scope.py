"""ask_global community-path scope integration tests (T-I.1 coverage 2).

Two claims are proven:

1. The ask_global handler's *internal* scope handling is fail-closed against a
   real catalog-backed scope resolver: an invalid ``source_id`` is rejected with
   ``InvalidScopeError`` and a missing one with ``MissingScopeError``, both
   before the LLM-mediated use case is invoked.
2. The ``Neo4jCommunityAdapter`` community read path (``load_entity_graph``,
   ``get_summaries_by_level``, ``count_summaries``) is read-only: it never
   mutates the graph snapshot.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from book_graph_rag.application.global_query_use_case import GlobalQueryUseCase
from book_graph_rag.domain.mcp_security import InvalidScopeError, MissingScopeError
from book_graph_rag.infrastructure.catalog_loader import CatalogLoader
from book_graph_rag.infrastructure.catalog_scope_resolver import CatalogScopeResolver
from book_graph_rag.infrastructure.community_adapter import Neo4jCommunityAdapter
from book_graph_rag.infrastructure.mcp.mcp_server_adapter import McpServerAdapter
from book_graph_rag.ports.community_read_port import CommunityReadPort
from book_graph_rag.ports.graph_query_port import GraphQueryPort
from book_graph_rag.ports.llm_summary_port import LLMSummaryPort
from book_graph_rag.ports.query_logger_port import QueryLoggerPort
from book_graph_rag.ports.text2cypher_port import Text2CypherPort, Text2CypherResult

_NS_A = "askglobal:ns-a"
_NS_B = "askglobal:ns-b"

_CATALOG = """\
version: 1
corpora:
  askglobal:
    label: "Ask-global corpus"
    sources:
      ns-a:
        label: "Namespace A"
        file: "a.pdf"
        status: active
      ns-b:
        label: "Namespace B"
        file: "b.pdf"
        status: active
"""


class _FakeCommunityReadPort(CommunityReadPort):
    async def load_entity_graph(self) -> tuple[list[Any], list[Any]]:
        return [], []

    async def get_summaries_by_level(self, level: int) -> list[Any]:
        return []

    async def count_summaries(self) -> int:
        return 0


class _FakeLLMSummaryPort(LLMSummaryPort):
    async def generate_community_summary(
        self, entities: list[Any], relationships: list[Any], level: int
    ) -> str:
        return "summary"

    async def generate_summary_from_children(
        self, child_summaries: list[Any], level: int
    ) -> str:
        return "summary"

    async def score_community(self, question: str, summary: Any) -> int:
        return 50

    async def compose_answer(self, question: str, ranked: list[Any]) -> str:
        return "answer"


class _RecordingGlobalQueryUseCase(GlobalQueryUseCase):
    def __init__(self) -> None:
        super().__init__(read_port=_FakeCommunityReadPort(), llm_port=_FakeLLMSummaryPort())
        self.calls: list[tuple[str, int]] = []

    async def ask(self, question: str, detail_level: int) -> dict[str, Any]:
        self.calls.append((question, detail_level))
        return {"answer": "answer", "citations": []}


class _FakeGraphQueryPort(GraphQueryPort):
    async def find_entity(
        self, name: str, entity_type: Any | None = None, **kwargs: Any
    ) -> list[Any]:
        return []

    async def find_entities_batch(self, ids: list[str]) -> list[Any]:
        return []

    async def traverse_relationships(
        self, source_id: str, rel_type: Any | None = None, depth: int = 1, **kwargs: Any
    ) -> tuple[list[Any], list[Any]]:
        return [], []

    async def find_path(self, start_id: str, end_id: str, max_depth: int = 3) -> list[Any]:
        return []

    async def search_chunks(self, query: str, limit: int = 10, **kwargs: Any) -> list[Any]:
        return []

    async def count_entities(self, entity_type: str | None = None, **kwargs: Any) -> int:
        return 0

    async def list_entities(
        self, cursor: int = 0, page_size: int = 50, **kwargs: Any
    ) -> tuple[list[Any], int]:
        return [], 0

    async def ensure_indexes(self) -> None:
        pass


class _NoopQueryLogger(QueryLoggerPort):
    async def log_query(self, entry: Any) -> None:
        pass

    async def close(self) -> None:
        pass


class _ExplodingText2Cypher(Text2CypherPort):
    async def generate_and_run(self, question: str) -> Text2CypherResult:
        raise AssertionError("text2cypher must never run")


def _resolver(tmp_path: Path) -> CatalogScopeResolver:
    catalog_path = tmp_path / "catalog.yaml"
    catalog_path.write_text(_CATALOG, encoding="utf-8")
    return CatalogScopeResolver(CatalogLoader(catalog_path))


def _adapter(tmp_path: Path) -> tuple[McpServerAdapter, _RecordingGlobalQueryUseCase]:
    use_case = _RecordingGlobalQueryUseCase()
    adapter = McpServerAdapter(
        _FakeGraphQueryPort(),
        _NoopQueryLogger(),
        _ExplodingText2Cypher(),
        global_query_use_case=use_case,
        scope_resolver=_resolver(tmp_path),
        hmac_key_id="test-v1",
        hmac_key=SecretStr("test-hmac-secret"),
    )
    return adapter, use_case


async def test_ask_global_invalid_source_id_rejected_before_use_case(tmp_path: Path) -> None:
    """An unresolvable source_id is rejected by the real resolver before the use case."""
    adapter, use_case = _adapter(tmp_path)

    with pytest.raises(InvalidScopeError) as exc_info:
        await adapter.ask_global("question?", source_id="askglobal:unknown")

    assert exc_info.value.error_code == "invalid_scope"
    assert use_case.calls == []


async def test_ask_global_missing_scope_rejected_before_use_case(tmp_path: Path) -> None:
    """A missing source_id fails closed with MissingScopeError before the use case."""
    adapter, use_case = _adapter(tmp_path)

    with pytest.raises(MissingScopeError) as exc_info:
        await adapter.ask_global("question?")

    assert exc_info.value.error_code == "missing_scope"
    assert use_case.calls == []


def _snapshot_key_value(value: Any) -> str:
    return "" if value is None else str(value)


async def _graph_snapshot(driver: Any) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    async with driver.session() as session:
        nodes_result = await session.run(
            "MATCH (n) "
            "RETURN labels(n) AS labels, n.id AS id, n.name AS name, n.type AS type, "
            "n.level AS level, n.summary AS summary"
        )
        nodes: list[tuple[Any, ...]] = []
        async for record in nodes_result:
            nodes.append(
                (
                    tuple(sorted(record["labels"])),
                    _snapshot_key_value(record["id"]),
                    _snapshot_key_value(record["name"]),
                    _snapshot_key_value(record["type"]),
                    _snapshot_key_value(record["level"]),
                    _snapshot_key_value(record["summary"]),
                )
            )

        rels_result = await session.run(
            "MATCH ()-[r]->() RETURN type(r) AS t, r.type AS prop"
        )
        rels: list[tuple[Any, ...]] = []
        async for record in rels_result:
            rels.append(
                (_snapshot_key_value(record["t"]), _snapshot_key_value(record["prop"]))
            )

    return (sorted(nodes), sorted(rels))


@pytest.mark.neo4j_integration
async def test_community_read_path_is_read_only(
    neo4j_settings: Any,
    neo4j_driver: Any,
) -> None:
    """Community reads never mutate the base graph or the summary nodes."""
    async with neo4j_driver.session() as session:
        await session.run(
            """
            MERGE (a:Entity {id: $a})
            SET a.name = 'Alpha', a.type = 'concept',
                a.source_page = 1, a.description = ''
            MERGE (b:Entity {id: $b})
            SET b.name = 'Beta', b.type = 'concept',
                b.source_page = 2, b.description = ''
            """,
            a=f"{_NS_A}:alpha",
            b=f"{_NS_A}:beta",
        )
        await session.run(
            """
            MATCH (a:Entity {id: $a}) MATCH (b:Entity {id: $b})
            MERGE (a)-[r:RELATED]->(b)
            SET r.type = 'requires', r.source_page = 1,
                r.description = '', r.chunk_index = 0
            """,
            a=f"{_NS_A}:alpha",
            b=f"{_NS_A}:beta",
        )
        await session.run(
            """
            MERGE (c:CommunitySummary {id: $id})
            SET c.level = 0, c.summary = 'root', c.entity_ids = [$a], c.parent_id = null
            """,
            id=hashlib.sha1(
                f"0:{_NS_A}:alpha".encode()
            ).hexdigest()[:16],
            a=f"{_NS_A}:alpha",
        )

    adapter = Neo4jCommunityAdapter(neo4j_settings)
    try:
        await adapter.ensure_indexes()
        before = await _graph_snapshot(neo4j_driver)

        entities, relationships = await adapter.load_entity_graph()
        assert entities, "load_entity_graph returned no entities"
        assert relationships, "load_entity_graph returned no relationships"

        summaries = await adapter.get_summaries_by_level(0)
        assert len(summaries) == 1

        count = await adapter.count_summaries()
        assert count == 1

        after = await _graph_snapshot(neo4j_driver)
        assert before == after, "community reads mutated the graph"
    finally:
        await adapter.close()
