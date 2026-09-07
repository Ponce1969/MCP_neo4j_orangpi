"""Composition root for semantic entity resolution adapters (Slice F1).

Wires the brute-force retrieval + sentence-transformer embedding path used by
the hybrid CLI and pipeline. All dependencies are concrete infrastructure
adapters so the use case itself stays port-driven and testable.
"""

from __future__ import annotations

from typing import Any

from neo4j import AsyncGraphDatabase

from book_graph_rag.application.resolve_entities_use_case import ResolveEntitiesUseCase
from book_graph_rag.config import Settings
from book_graph_rag.domain.models import Entity
from book_graph_rag.domain.s4_band_assignment import BandThresholds
from book_graph_rag.infrastructure.brute_force_candidate_retrieval import (
    BruteForceCandidateRetrieval,
)
from book_graph_rag.infrastructure.jsonl_quarantine_writer import JSONLQuarantineWriter
from book_graph_rag.infrastructure.neo4j_command_adapter import Neo4jCommandAdapter
from book_graph_rag.infrastructure.neo4j_neighborhood_query_adapter import (
    Neo4jNeighborhoodQueryAdapter,
)
from book_graph_rag.infrastructure.sentence_transformer_adapter import (
    SentenceTransformerAdapter,
)
from book_graph_rag.ports.embedding_provider_port import EmbeddingRequest


def _make_driver(settings: Settings) -> Any:
    """Create an ephemeral bolt driver for resolution adapters."""
    return AsyncGraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value()),
    )


def _anchor_text(entity: Entity, input_variant: str) -> str:
    """Build the embedding input text for ``entity`` (variant A or B)."""
    parts = [entity.name] + list(entity.aliases)
    if input_variant == "B":
        parts.append(entity.description or "")
    return " ".join(p for p in parts if p)


def _namespace_from_id(entity_id: str) -> str:
    """Extract ``corpus:source`` from ``corpus:source:slug-type``."""
    parts = entity_id.split(":")
    if len(parts) >= 2:
        return f"{parts[0]}:{parts[1]}"
    return entity_id


async def build_resolve_entities_use_case(
    settings: Settings,
) -> tuple[ResolveEntitiesUseCase, list[Any]]:
    """Wire a hybrid ``ResolveEntitiesUseCase`` with brute-force retrieval.

    Pre-computes embeddings for every active entity and populates the in-memory
    retrieval cache. Returns the use case plus a list of closable adapters
    (entity loader + neighborhood query) that the caller must close.
    """
    embedding = SentenceTransformerAdapter(settings)
    entity_loader = Neo4jCommandAdapter(settings)
    neighborhood = Neo4jNeighborhoodQueryAdapter(_make_driver(settings))

    entities = await entity_loader.load_active_entities(batch_size=500)
    retrieval = BruteForceCandidateRetrieval()
    if entities:
        texts = tuple(_anchor_text(e, settings.embedding_input_variant) for e in entities)
        batch = await embedding.embed(
            EmbeddingRequest(texts=texts, model_id=settings.embedding_model_id)
        )
        for entity, vector in zip(entities, batch.vectors, strict=True):
            await retrieval.upsert_entity_embedding(entity.id, vector)
            retrieval.upsert_entity_metadata(
                entity.id, entity.type, _namespace_from_id(entity.id)
            )

    thresholds = BandThresholds(
        high_cosine=settings.band_high_cosine,
        high_context=settings.band_high_context,
        medium_cosine=settings.band_medium_cosine,
        conflict_floor=settings.band_conflict_floor,
    )

    use_case = ResolveEntitiesUseCase(
        retrieval=retrieval,
        embedding=embedding,
        entity_loader=entity_loader,
        neighborhood=neighborhood,
        quarantine_writer=JSONLQuarantineWriter(settings.quarantine_path),
        thresholds=thresholds,
        input_variant=settings.embedding_input_variant,
        model_id=settings.embedding_model_id,
        top_k=settings.embedding_top_k,
        min_similarity=settings.embedding_min_similarity,
    )

    closables: list[Any] = [entity_loader, neighborhood]
    return use_case, closables

