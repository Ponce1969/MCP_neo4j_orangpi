"""Testcontainers integration tests for Neo4jVectorCandidateRetrieval.

Uses synthetic (FAKE) vectors written directly via upsert_entity_embedding; no
real sentence-transformers model is loaded in CI.
"""

from __future__ import annotations

from typing import Any

import pytest

from book_graph_rag.config import Settings
from book_graph_rag.domain.models import Entity
from book_graph_rag.infrastructure.neo4j_command_adapter import Neo4jCommandAdapter
from book_graph_rag.infrastructure.neo4j_vector_candidate_retrieval import (
    Neo4jVectorCandidateRetrieval,
)
from book_graph_rag.ports.candidate_retrieval_port import CandidateRetrievalRequest
from book_graph_rag.ports.embedding_provider_port import EmbeddingVector


def _make_settings(neo4j_settings: Settings) -> Settings:
    """Override resolution-specific settings for the vector adapter test."""
    data = neo4j_settings.model_dump()
    data["candidate_retrieval_strategy"] = "neo4j_vector"
    data["embedding_dim"] = 3
    data["vector_index_name"] = "entity_embedding_index"
    return Settings.model_validate(data)


def _vec(values: tuple[float, ...]) -> EmbeddingVector:
    return EmbeddingVector(values=values, model_id="fake")


@pytest.mark.neo4j_integration
async def test_vector_index_lifecycle_and_retrieval(
    neo4j_driver: Any, neo4j_settings: Settings
) -> None:
    """ensure_index creates the index; upsert + retrieve round-trip with FAKE vectors."""
    settings = _make_settings(neo4j_settings)
    adapter = Neo4jVectorCandidateRetrieval(neo4j_driver, settings)
    command_adapter = Neo4jCommandAdapter(settings)

    await adapter.ensure_index()

    await command_adapter.upsert_entities(
        [
            Entity(id="book:ch1:agent-alpha", name="Alpha", type="agent", description=""),
            Entity(id="book:ch1:agent-beta", name="Beta", type="agent", description=""),
            Entity(id="book:ch1:agent-gamma", name="Gamma", type="agent", description=""),
            Entity(id="book:ch1:concept-x", name="X", type="concept", description=""),
        ]
    )

    await adapter.upsert_entity_embedding("book:ch1:agent-alpha", _vec((1.0, 0.0, 0.0)))
    await adapter.upsert_entity_embedding("book:ch1:agent-beta", _vec((0.9, 0.1, 0.0)))
    await adapter.upsert_entity_embedding("book:ch1:agent-gamma", _vec((0.1, 0.9, 0.0)))
    await adapter.upsert_entity_embedding("book:ch1:concept-x", _vec((0.95, 0.05, 0.0)))

    request = CandidateRetrievalRequest(
        anchor_id="book:ch1:agent-alpha",
        anchor_text="Alpha",
        anchor_type="agent",
        anchor_namespace="book:ch1",
        top_k=5,
        min_similarity=0.5,
    )
    hits = await adapter.retrieve(request)

    assert len(hits) == 2
    assert hits[0].candidate_id == "book:ch1:agent-beta"
    assert hits[0].cosine_similarity == pytest.approx(0.997, abs=0.01)
    assert hits[0].candidate_type == "agent"
    assert hits[0].candidate_namespace == "book:ch1"
    assert hits[1].candidate_id == "book:ch1:agent-gamma"

    await command_adapter.close()


@pytest.mark.neo4j_integration
async def test_ensure_index_is_idempotent(
    neo4j_driver: Any, neo4j_settings: Settings
) -> None:
    """Calling ensure_index twice must not raise (IF NOT EXISTS semantics)."""
    settings = _make_settings(neo4j_settings)
    adapter = Neo4jVectorCandidateRetrieval(neo4j_driver, settings)

    await adapter.ensure_index()
    await adapter.ensure_index()


@pytest.mark.neo4j_integration
async def test_retrieval_respects_top_k(
    neo4j_driver: Any, neo4j_settings: Settings
) -> None:
    """Only top_k candidates are returned when more than top_k are above min_sim."""
    settings = _make_settings(neo4j_settings)
    adapter = Neo4jVectorCandidateRetrieval(neo4j_driver, settings)
    command_adapter = Neo4jCommandAdapter(settings)

    await adapter.ensure_index()

    await command_adapter.upsert_entities(
        [
            Entity(id="book:ch1:agent-a", name="A", type="agent", description=""),
            Entity(id="book:ch1:agent-b", name="B", type="agent", description=""),
            Entity(id="book:ch1:agent-c", name="C", type="agent", description=""),
        ]
    )

    await adapter.upsert_entity_embedding("book:ch1:agent-a", _vec((1.0, 0.0, 0.0)))
    await adapter.upsert_entity_embedding("book:ch1:agent-b", _vec((0.95, 0.05, 0.0)))
    await adapter.upsert_entity_embedding("book:ch1:agent-c", _vec((0.9, 0.1, 0.0)))

    request = CandidateRetrievalRequest(
        anchor_id="book:ch1:agent-a",
        anchor_text="A",
        anchor_type="agent",
        anchor_namespace="book:ch1",
        top_k=1,
        min_similarity=0.5,
    )
    hits = await adapter.retrieve(request)

    assert len(hits) == 1
    assert hits[0].candidate_id == "book:ch1:agent-b"

    await command_adapter.close()


@pytest.mark.neo4j_integration
async def test_retrieval_excludes_merged_entities(
    neo4j_driver: Any, neo4j_settings: Settings
) -> None:
    """Candidates with ``merged_into`` set are filtered out."""
    settings = _make_settings(neo4j_settings)
    adapter = Neo4jVectorCandidateRetrieval(neo4j_driver, settings)
    command_adapter = Neo4jCommandAdapter(settings)

    await adapter.ensure_index()

    await command_adapter.upsert_entities(
        [
            Entity(id="book:ch1:agent-active", name="Active", type="agent", description=""),
            Entity(id="book:ch1:agent-merged", name="Merged", type="agent", description=""),
        ]
    )

    await adapter.upsert_entity_embedding("book:ch1:agent-active", _vec((1.0, 0.0, 0.0)))
    await adapter.upsert_entity_embedding("book:ch1:agent-merged", _vec((0.99, 0.01, 0.0)))

    async with neo4j_driver.session() as session:
        await session.run(
            "MATCH (n:Entity {id: $id}) SET n.merged_into = $canonical",
            id="book:ch1:agent-merged",
            canonical="book:ch1:agent-active",
        )

    request = CandidateRetrievalRequest(
        anchor_id="book:ch1:agent-active",
        anchor_text="Active",
        anchor_type="agent",
        anchor_namespace="book:ch1",
        top_k=5,
        min_similarity=0.5,
    )
    hits = await adapter.retrieve(request)

    assert hits == []

    await command_adapter.close()
