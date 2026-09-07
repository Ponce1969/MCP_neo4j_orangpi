"""ResolveEntitiesUseCase — orchestrates S0→S4 + policy (Slice E4/E5).

The use case depends only on domain logic and ports. It loads active entities,
retrieves candidates, stages evidence, and routes each pair through the binding
band-to-action matrix from the policy clarification (engram 1165).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from book_graph_rag.domain.models import Entity
from book_graph_rag.domain.quarantine_models import QuarantineDecision, QuarantineRecord
from book_graph_rag.domain.resolution_models import (
    ConfidenceBand,
    ResolutionEvidence,
    S1EmbeddingSignal,
)
from book_graph_rag.domain.resolution_policy import decide
from book_graph_rag.domain.s0_normalization import s0_match
from book_graph_rag.domain.s2_type_gate import s2_type_gate
from book_graph_rag.domain.s3_context_scoring import (
    description_overlap,
    mentions_jaccard,
    related_jaccard,
    s3_context_score,
)
from book_graph_rag.domain.s4_band_assignment import BandThresholds, assign_band
from book_graph_rag.ports.candidate_retrieval_port import (
    CandidateHit,
    CandidateRetrievalPort,
    CandidateRetrievalRequest,
)
from book_graph_rag.ports.embedding_provider_port import EmbeddingProviderPort
from book_graph_rag.ports.graph_db_port import GraphDatabasePort
from book_graph_rag.ports.neighborhood_query_port import NeighborhoodQueryPort
from book_graph_rag.ports.quarantine_writer_port import QuarantineWriterPort


class CandidatePair(BaseModel):
    """A pair that the policy routed to NO_MERGE."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    anchor_id: str
    candidate_id: str
    band: ConfidenceBand
    evidence: ResolutionEvidence


class MergeGroup(BaseModel):
    """An auto-merge-ready group produced by the analyzer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    canonical_id: str
    duplicate_ids: list[str]
    band: ConfidenceBand
    evidence: list[ResolutionEvidence]


class ResolveEntitiesResult(BaseModel):
    """Output of one analysis pass."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    auto_merge_groups: list[MergeGroup]
    quarantine_records: list[QuarantineRecord]
    no_merge_candidates: list[CandidatePair]
    total_pairs_evaluated: int


class ResolveEntitiesUseCase:
    """Run the staged S0–S4 resolution pipeline over the active entity set."""

    def __init__(
        self,
        retrieval: CandidateRetrievalPort,
        embedding: EmbeddingProviderPort,
        entity_loader: GraphDatabasePort,
        neighborhood: NeighborhoodQueryPort,
        quarantine_writer: QuarantineWriterPort,
        thresholds: BandThresholds,
        input_variant: Literal["A", "B"],
        model_id: str,
        top_k: int,
        min_similarity: float,
    ) -> None:
        self._retrieval = retrieval
        self._embedding = embedding
        self._entity_loader = entity_loader
        self._neighborhood = neighborhood
        self._quarantine_writer = quarantine_writer
        self._thresholds = thresholds
        self._input_variant = input_variant
        self._model_id = model_id
        self._top_k = top_k
        self._min_similarity = min_similarity

    async def analyze(self, *, dry_run: bool) -> ResolveEntitiesResult:
        """Evaluate every active anchor against its retrieved candidates.

        When ``dry_run`` is True, quarantine records are returned but not
        persisted, and auto-merge groups are staged but not applied. Re-running
        after merges is idempotent because ``load_active_entities`` filters out
        soft-deleted duplicates.
        """
        entities = await self._entity_loader.load_active_entities(batch_size=500)
        entity_by_id = {e.id: e for e in entities}

        auto_merge_groups: dict[str, MergeGroup] = {}
        quarantine_records: list[QuarantineRecord] = []
        no_merge_candidates: list[CandidatePair] = []
        total_pairs = 0

        next_quarantine_seq = self._next_quarantine_seq()

        for anchor in entities:
            request = CandidateRetrievalRequest(
                anchor_id=anchor.id,
                anchor_text=self._anchor_text(anchor),
                anchor_type=anchor.type,
                anchor_namespace=self._namespace_from_id(anchor.id),
                top_k=self._top_k,
                min_similarity=self._min_similarity,
            )
            hits = await self._retrieval.retrieve(request)

            for rank, hit in enumerate(hits, start=1):
                if hit.candidate_id == anchor.id:
                    continue
                candidate = entity_by_id.get(hit.candidate_id)
                if candidate is None:
                    # Stale retrieval result for an entity that is no longer active.
                    continue

                total_pairs += 1
                evidence = await self._evaluate_pair(anchor, candidate, hit, rank)
                decision = decide(evidence, self._thresholds)

                if decision.action.value == "auto_merge":
                    self._add_to_auto_merge_group(
                        auto_merge_groups, anchor.id, candidate.id, evidence
                    )
                elif decision.action.value == "quarantine":
                    record = QuarantineRecord(
                        schema_version="1.0.0",
                        seq=next_quarantine_seq,
                        anchor_id=anchor.id,
                        candidate_id=candidate.id,
                        canonical_id=anchor.id,
                        band=evidence.band,
                        evidence=evidence,
                        created_at=datetime.now(UTC),
                        decision=QuarantineDecision.PENDING,
                    )
                    quarantine_records.append(record)
                    if not dry_run:
                        self._quarantine_writer.append(record)
                    next_quarantine_seq += 1
                else:
                    no_merge_candidates.append(
                        CandidatePair(
                            anchor_id=anchor.id,
                            candidate_id=candidate.id,
                            band=evidence.band,
                            evidence=evidence,
                        )
                    )

        return ResolveEntitiesResult(
            auto_merge_groups=list(auto_merge_groups.values()),
            quarantine_records=quarantine_records,
            no_merge_candidates=no_merge_candidates,
            total_pairs_evaluated=total_pairs,
        )

    def _anchor_text(self, anchor: Entity) -> str:
        """Build the embedding input text for the anchor (variant A or B)."""
        parts = [anchor.name] + list(anchor.aliases)
        if self._input_variant == "B":
            parts.append(anchor.description or "")
        return " ".join(p for p in parts if p)

    def _namespace_from_id(self, entity_id: str) -> str:
        """Extract ``corpus:source`` from ``corpus:source:slug-type``."""
        parts = entity_id.split(":")
        if len(parts) >= 2:
            return f"{parts[0]}:{parts[1]}"
        return entity_id

    def _next_quarantine_seq(self) -> int:
        """Return the next unused quarantine sequence number."""
        existing = self._quarantine_writer.read_all()
        if not existing:
            return 1
        return max(r.seq for r in existing) + 1

    async def _evaluate_pair(
        self,
        anchor: Entity,
        candidate: Entity,
        hit: CandidateHit,
        rank: int,
    ) -> ResolutionEvidence:
        """Run S0, S2, S3, S4 for a single anchor→candidate hit."""
        evidence = s0_match(anchor, candidate)

        if evidence.s0_matched_field != "none":
            # S0 exact match short-circuits later stages.
            return evidence

        s2 = s2_type_gate(anchor.type, candidate.type)
        evidence = evidence.model_copy(
            update={
                "s1": S1EmbeddingSignal(
                    cosine_similarity=hit.cosine_similarity,
                    candidate_rank=rank,
                    input_variant=self._input_variant,
                    model_id=self._model_id,
                ),
                "s2": s2,
            }
        )

        # S3 is only meaningful when the type gate passed.
        if s2.passed:
            anchor_mentions = await self._neighborhood.mention_sources(anchor.id)
            cand_mentions = await self._neighborhood.mention_sources(candidate.id)
            anchor_related = await self._neighborhood.related_neighbors(anchor.id)
            cand_related = await self._neighborhood.related_neighbors(candidate.id)

            mentions_j, mentions_shared, mentions_union = mentions_jaccard(
                anchor_mentions, cand_mentions
            )
            related_j, related_shared, related_union = related_jaccard(
                anchor_related, cand_related
            )
            desc_o = description_overlap(anchor.description or "", candidate.description or "")
            s3 = s3_context_score(
                mentions_j=mentions_j,
                related_j=related_j,
                desc_o=desc_o,
                cosine=hit.cosine_similarity,
                mentions_source_count=mentions_union,
                mentions_shared_count=mentions_shared,
                related_neighbor_count=related_union,
                related_shared_count=related_shared,
            )
            composite = (mentions_j + related_j + desc_o) / 3.0
            band = assign_band(
                s1_cosine=hit.cosine_similarity,
                s3=s3,
                s0_match_field=evidence.s0_matched_field,
                thresholds=self._thresholds,
            )
            evidence = evidence.model_copy(
                update={
                    "s3": s3,
                    "composite_score": composite,
                    "band": band,
                }
            )
        else:
            evidence = evidence.model_copy(
                update={
                    "composite_score": 0.0,
                    "band": ConfidenceBand.LOW,
                }
            )

        return evidence

    def _add_to_auto_merge_group(
        self,
        groups: dict[str, MergeGroup],
        canonical_id: str,
        duplicate_id: str,
        evidence: ResolutionEvidence,
    ) -> None:
        """Accumulate a duplicate into the auto-merge group for ``canonical_id``."""
        if canonical_id not in groups:
            groups[canonical_id] = MergeGroup(
                canonical_id=canonical_id,
                duplicate_ids=[],
                band=evidence.band,
                evidence=[],
            )
        group = groups[canonical_id]
        if duplicate_id not in group.duplicate_ids:
            group.duplicate_ids.append(duplicate_id)
        group.evidence.append(evidence)
