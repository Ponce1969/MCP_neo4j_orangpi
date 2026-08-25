"""Tests for Neo4jRetrievalSmokeAdapter."""
from __future__ import annotations

from typing import Any

import pytest

from book_graph_rag.domain.models import (
    Entity,
    EntityWithContext,
    GraphPath,
    Relationship,
)
from book_graph_rag.domain.validation_models import (
    SmokeCase,
    SmokeOutcome,
)
from book_graph_rag.infrastructure.neo4j_retrieval_smoke_adapter import (
    Neo4jRetrievalSmokeAdapter,
)
from book_graph_rag.ports.graph_query_port import GraphQueryPort


class _FakeQueryPort(GraphQueryPort):
    def __init__(
        self,
        entities: list[EntityWithContext] | None = None,
        relationships: list[Relationship] | None = None,
        paths: list[GraphPath] | None = None,
        chunks: list[dict[str, Any]] | None = None,
    ) -> None:
        self._entities = entities or []
        self._relationships = relationships or []
        self._paths = paths or []
        self._chunks = chunks or []

    async def find_entity(self, name: str, entity_type: Any) -> list[EntityWithContext]:
        return self._entities

    async def find_entities_batch(self, ids: list[str]) -> list[EntityWithContext]:
        return []

    async def traverse_relationships(
        self, source_id: str, rel_type: Any, depth: int
    ) -> tuple[list[EntityWithContext], list[Relationship]]:
        return [], self._relationships

    async def find_path(self, start_id: str, end_id: str, max_depth: int) -> list[GraphPath]:
        return self._paths

    async def search_chunks(self, query: str, limit: int) -> list[dict[str, Any]]:
        return self._chunks

    async def count_entities(self, entity_type: str | None) -> int:
        return 0

    async def list_entities(
        self, cursor: int, page_size: int
    ) -> tuple[list[EntityWithContext], int]:
        return [], 0

    async def ensure_indexes(self) -> None:
        return None


def _entity(entity_id: str) -> Entity:
    return Entity(id=entity_id, name=entity_id, type="pattern")


def _case(kind: str, request: dict[str, Any], assertion: dict[str, Any]) -> SmokeCase:
    return SmokeCase(
        case_id=kind + "-1",
        kind=kind,  # type: ignore[arg-type]
        request=request,
        book_scope="book-1",
        assertion=assertion,
        required_provenance=True,
    )


@pytest.mark.asyncio
async def test_relationship_lookup_maps_triples() -> None:
    rel = Relationship(source_entity_id="a", target_entity_id="b", type="requires")
    adapter = Neo4jRetrievalSmokeAdapter(_FakeQueryPort(relationships=[rel]))
    case = _case(
        "relationship_lookup",
        {"source_entity_id": "a", "target_entity_id": "b"},
        {"expected_triples": [["a", "requires", "b"]]},
    )
    result = await adapter.run_case(case)
    assert result.status == SmokeOutcome.PASS
    assert result.matched_relationship_triples == (("a", "requires", "b"),)


@pytest.mark.asyncio
async def test_two_hop_path_maps_paths() -> None:
    path = GraphPath(
        nodes=[_entity("a"), _entity("b"), _entity("c")],
        relationships=[
            Relationship(source_entity_id="a", target_entity_id="b", type="requires"),
            Relationship(source_entity_id="b", target_entity_id="c", type="alternative_to"),
        ],
    )
    adapter = Neo4jRetrievalSmokeAdapter(_FakeQueryPort(paths=[path]))
    case = _case(
        "two_hop_path",
        {"source_entity_id": "a", "target_entity_id": "c"},
        {"expected_paths": [[["a", "requires", "b"], ["b", "alternative_to", "c"]]]},
    )
    result = await adapter.run_case(case)
    assert result.status == SmokeOutcome.PASS
    assert result.matched_paths == (
        (("a", "requires", "b"), ("b", "alternative_to", "c")),
    )


@pytest.mark.asyncio
async def test_chunk_search_maps_matched_chunks() -> None:
    chunks = [
        {
            "chunk_id": "book-1:1",
            "book_id": "book-1",
            "chapter_id": "1:Intro",
            "section_id": None,
            "page_start": 1,
            "page_end": 2,
            "text": "x",
            "score": 0.9,
        }
    ]
    adapter = Neo4jRetrievalSmokeAdapter(_FakeQueryPort(chunks=chunks))
    case = _case(
        "chunk_search",
        {"query": "definition"},
        {"expected_chunk_ids": ["book-1:1"]},
    )
    result = await adapter.run_case(case)
    assert result.status == SmokeOutcome.PASS
    assert result.matched_chunks[0].chunk_id == "book-1:1"
    assert result.matched_chunks[0].chapter_id == "1:Intro"


@pytest.mark.asyncio
async def test_chunk_search_without_identity_fails_closed() -> None:
    adapter = Neo4jRetrievalSmokeAdapter(_FakeQueryPort(chunks=[]))
    case = _case(
        "chunk_search",
        {"query": "definition"},
        {"expected_chunk_ids": ["chunk-1"]},
    )
    result = await adapter.run_case(case)
    assert result.status == SmokeOutcome.FAIL
