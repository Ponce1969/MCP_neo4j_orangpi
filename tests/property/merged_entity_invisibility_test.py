"""Property: merged entities are invisible to the resolution pipeline.

``load_active_entities`` filters out entities marked as merged.  Therefore,
``ResolveEntitiesUseCase.analyze`` must never anchor a candidate retrieval on a
merged id, and no merge group / quarantine record / no-merge candidate may
reference a merged entity id.

These tests are pure: they use an in-memory fake loader that mirrors the real
``merged_into IS NULL`` query semantics, with no Neo4j or testcontainers.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from book_graph_rag.application.resolve_entities_use_case import (
    ResolveEntitiesResult,
    ResolveEntitiesUseCase,
)
from book_graph_rag.domain.checkpoint_models import Checkpoint
from book_graph_rag.domain.models import (
    Chapter,
    Entity,
    EntityType,
    KnowledgeGraphChunk,
    Relationship,
    Section,
)
from book_graph_rag.domain.quarantine_models import QuarantineDecision, QuarantineRecord
from book_graph_rag.domain.s4_band_assignment import BandThresholds
from book_graph_rag.ports.candidate_retrieval_port import (
    CandidateHit,
    CandidateRetrievalPort,
    CandidateRetrievalRequest,
)
from book_graph_rag.ports.embedding_provider_port import (
    EmbeddingBatch,
    EmbeddingProviderPort,
    EmbeddingRequest,
    EmbeddingVector,
)
from book_graph_rag.ports.graph_db_port import GraphDatabasePort
from book_graph_rag.ports.neighborhood_query_port import NeighborhoodQueryPort
from book_graph_rag.ports.quarantine_writer_port import QuarantineWriterPort

_ENTITY_TYPES: list[EntityType] = [
    "pattern",
    "agent",
    "component",
    "concept",
    "tool",
    "framework",
    "mcp",
    "llmops",
    "risk",
]


class _EntityWithMerged(Entity):
    """Test-only entity that carries the soft-delete marker used by the loader."""

    merged_into: str | None = None


class _FakeLoader(GraphDatabasePort):
    """In-memory loader that mirrors ``merged_into IS NULL`` filtering."""

    def __init__(self, entities: list[_EntityWithMerged]) -> None:
        self._entities = entities
        self.loaded_calls: list[int] = []

    async def load_active_entities(self, *, batch_size: int = 500) -> list[Entity]:
        self.loaded_calls.append(batch_size)
        return [e for e in self._entities if not e.merged_into]

    async def upsert_book(self, book: object) -> None:
        raise NotImplementedError

    async def upsert_entities(self, entities: list[Entity]) -> None:
        raise NotImplementedError

    async def upsert_relationships(self, relationships: list[Relationship]) -> None:
        raise NotImplementedError

    async def upsert_mentions(
        self, chunk_index: int, book_id: str | None, entity_ids: list[str]
    ) -> None:
        raise NotImplementedError

    async def upsert_editorial_structure(
        self, chapter: Chapter | None, sections: list[Section], chunk: KnowledgeGraphChunk
    ) -> None:
        raise NotImplementedError

    async def commit_chunk_atomic(
        self,
        chunk: object,
        entity_ids: list[str],
        versions: object,
        *,
        attempt: int = 1,
        lease_owned: bool = True,
    ) -> Checkpoint:
        raise NotImplementedError

    async def clear_index(self) -> None:
        raise NotImplementedError

    async def count_chunks(self) -> int:
        return 0

    async def count_entities(self) -> int:
        return 0

    async def count_mentions(self) -> int:
        return 0


class _FakeRetrieval(CandidateRetrievalPort):
    """Returns every entity in the corpus as a candidate, including merged ones."""

    def __init__(self, entities: list[_EntityWithMerged]) -> None:
        self._entities = entities
        self.requests: list[CandidateRetrievalRequest] = []

    async def retrieve(self, request: CandidateRetrievalRequest) -> list[CandidateHit]:
        self.requests.append(request)
        hits: list[CandidateHit] = []
        for candidate in self._entities:
            if candidate.id == request.anchor_id:
                continue
            hits.append(
                CandidateHit(
                    candidate_id=candidate.id,
                    cosine_similarity=0.95,
                    candidate_type=candidate.type,
                    candidate_namespace=_namespace_from_id(candidate.id),
                )
            )
        return hits

    async def upsert_entity_embedding(self, entity_id: str, vector: EmbeddingVector) -> None:
        pass

    async def ensure_index(self) -> None:
        pass


class _FakeEmbedding(EmbeddingProviderPort):
    async def embed(self, request: EmbeddingRequest) -> EmbeddingBatch:
        return EmbeddingBatch(
            model_id=request.model_id,
            vectors=[
                EmbeddingVector(values=(0.0, 0.0), model_id=request.model_id)
            ]
            * len(request.texts),
        )

    def model_dim(self, model_id: str) -> int:
        return 2


class _FakeNeighborhood(NeighborhoodQueryPort):
    async def mention_sources(self, entity_id: str) -> set[str]:
        return {"src:1"}

    async def related_neighbors(self, entity_id: str) -> set[str]:
        return {"book:ch1:neighbor"}


class _FakeQuarantine(QuarantineWriterPort):
    def __init__(self) -> None:
        self.records: list[QuarantineRecord] = []

    def append(self, record: QuarantineRecord) -> None:
        self.records.append(record)

    def read_all(self) -> list[QuarantineRecord]:
        return list(self.records)

    def read_pending(self) -> list[QuarantineRecord]:
        return [r for r in self.records if r.decision == QuarantineDecision.PENDING]

    def update_decision(
        self,
        seq: int,
        decision: QuarantineDecision,
        reviewed_by: str,
        reviewed_at: datetime,
    ) -> None:
        pass


def _namespace_from_id(entity_id: str) -> str:
    parts = entity_id.split(":")
    if len(parts) >= 2:
        return f"{parts[0]}:{parts[1]}"
    return entity_id


@st.composite
def _entity_set(draw: st.DrawFn) -> list[_EntityWithMerged]:
    """Generate a heterogeneous set of active and merged entities."""
    count = draw(st.integers(min_value=1, max_value=12))
    entities: list[_EntityWithMerged] = []
    for i in range(count):
        entity_type = draw(st.sampled_from(_ENTITY_TYPES))
        namespace = draw(
            st.text(
                alphabet=st.characters(whitelist_categories=("L", "N")),
                min_size=1,
                max_size=10,
            )
        )
        merged = draw(st.booleans())
        entities.append(
            _EntityWithMerged(
                id=f"{namespace}:{entity_type}:ent{i}",
                name=f"Entity {i} {namespace}",
                type=entity_type,
                aliases=[],
                merged_into="canonical:entity" if merged else None,
            )
        )
    return entities


async def _run_analysis(entities: list[_EntityWithMerged]) -> ResolveEntitiesResult:
    loader = _FakeLoader(entities)
    retrieval = _FakeRetrieval(entities)
    use_case = ResolveEntitiesUseCase(
        retrieval=retrieval,
        embedding=_FakeEmbedding(),
        entity_loader=loader,
        neighborhood=_FakeNeighborhood(),
        quarantine_writer=_FakeQuarantine(),
        thresholds=BandThresholds(),
        input_variant="A",
        model_id="fake",
        top_k=50,
        min_similarity=0.0,
    )
    return await use_case.analyze(dry_run=True)


@given(entities=_entity_set())
@settings(max_examples=150, deadline=None)
@pytest.mark.asyncio
async def test_load_active_entities_never_returns_merged_entities(
    entities: list[_EntityWithMerged],
) -> None:
    """The fake loader itself must exclude every entity with ``merged_into`` set."""
    loader = _FakeLoader(entities)
    active = await loader.load_active_entities()
    active_ids = {e.id for e in active}
    merged_ids = {e.id for e in entities if e.merged_into}

    assert active_ids.isdisjoint(merged_ids)
    assert all(isinstance(e, Entity) for e in active)


@given(entities=_entity_set())
@settings(max_examples=150, deadline=None)
@pytest.mark.asyncio
async def test_analysis_result_never_references_merged_entities(
    entities: list[_EntityWithMerged],
) -> None:
    """No analysis artifact may contain a merged entity id."""
    result = await _run_analysis(entities)
    merged_ids = {e.id for e in entities if e.merged_into}
    active_ids = {e.id for e in entities if not e.merged_into}

    for group in result.auto_merge_groups:
        assert group.canonical_id in active_ids
        assert group.canonical_id not in merged_ids
        for dup_id in group.duplicate_ids:
            assert dup_id in active_ids
            assert dup_id not in merged_ids

    for record in result.quarantine_records:
        assert record.anchor_id in active_ids
        assert record.anchor_id not in merged_ids
        assert record.candidate_id in active_ids
        assert record.candidate_id not in merged_ids

    for pair in result.no_merge_candidates:
        assert pair.anchor_id in active_ids
        assert pair.anchor_id not in merged_ids
        assert pair.candidate_id in active_ids
        assert pair.candidate_id not in merged_ids
