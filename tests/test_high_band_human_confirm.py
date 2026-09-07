"""Load-bearing guard for the high-band human-confirm queue (Slice E5).

The binding matrix (engram 1165) requires ``band == high`` to be routed to the
quarantine JSONL as a human-confirm record, NOT to ``auto_merge_groups``.
"""

from __future__ import annotations

from datetime import datetime

from book_graph_rag.application.resolve_entities_use_case import (
    ResolveEntitiesUseCase,
)
from book_graph_rag.domain.checkpoint_models import Checkpoint
from book_graph_rag.domain.models import (
    Chapter,
    Entity,
    KnowledgeGraphChunk,
    Relationship,
    Section,
)
from book_graph_rag.domain.quarantine_models import QuarantineDecision, QuarantineRecord
from book_graph_rag.domain.resolution_models import ConfidenceBand
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


class _FakeLoader(GraphDatabasePort):
    def __init__(self, entities: list[Entity]) -> None:
        self._entities = entities

    async def load_active_entities(self, *, batch_size: int = 500) -> list[Entity]:
        return list(self._entities)

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

    async def clear_index(self) -> None:
        raise NotImplementedError

    async def count_chunks(self) -> int:
        return 0

    async def count_entities(self) -> int:
        return 0

    async def count_mentions(self) -> int:
        return 0

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


class _FakeRetrieval(CandidateRetrievalPort):
    def __init__(self, hits: list[CandidateHit]) -> None:
        self._hits = hits

    async def retrieve(self, request: CandidateRetrievalRequest) -> list[CandidateHit]:
        return list(self._hits)

    async def upsert_entity_embedding(self, entity_id: str, vector: EmbeddingVector) -> None:
        pass

    async def ensure_index(self) -> None:
        pass


class _FakeEmbedding(EmbeddingProviderPort):
    async def embed(self, request: EmbeddingRequest) -> EmbeddingBatch:
        return EmbeddingBatch(
            model_id=request.model_id,
            vectors=[EmbeddingVector(values=(0.0,), model_id=request.model_id)]
            * len(request.texts),
        )

    def model_dim(self, model_id: str) -> int:
        return 1


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
        return []

    def read_pending(self) -> list[QuarantineRecord]:
        return []

    def update_decision(
        self,
        seq: int,
        decision: QuarantineDecision,
        reviewed_by: str,
        reviewed_at: datetime,
    ) -> None:
        pass


def _entity(entity_id: str, name: str, entity_type: str) -> Entity:
    return Entity(id=entity_id, name=name, type=entity_type)  # type: ignore[arg-type]


async def test_high_band_is_quarantined_not_auto_merged() -> None:
    """A same-type, same-namespace high-band pair lands in the human-confirm queue."""
    anchor = _entity("book:ch1:anchor", "Anchor", "framework")
    high_dup = _entity("book:ch1:high", "High", "framework")

    retrieval = _FakeRetrieval(
        [
            CandidateHit(
                candidate_id=high_dup.id,
                cosine_similarity=0.95,
                candidate_type="framework",
                candidate_namespace="book:ch1",
            )
        ]
    )
    quarantine = _FakeQuarantine()

    use_case = ResolveEntitiesUseCase(
        retrieval=retrieval,
        embedding=_FakeEmbedding(),
        entity_loader=_FakeLoader([anchor, high_dup]),
        neighborhood=_FakeNeighborhood(),
        quarantine_writer=quarantine,
        thresholds=BandThresholds(),
        input_variant="A",
        model_id="fake",
        top_k=10,
        min_similarity=0.0,
    )

    result = await use_case.analyze(dry_run=True)

    assert result.auto_merge_groups == []
    assert len(result.quarantine_records) == 1
    record = result.quarantine_records[0]
    assert record.band == ConfidenceBand.HIGH
    assert record.anchor_id == anchor.id
    assert record.candidate_id == high_dup.id
