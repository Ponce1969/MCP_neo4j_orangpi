"""Unit tests for the semantic entity resolution application use cases (Slice E4).

All ports are faked in memory so these tests exercise pure application/domain
logic without Neo4j, sentence-transformers, or the real filesystem.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from typing import cast as typing_cast

import pytest

from book_graph_rag.domain.merge_ledger_models import (
    EdgeInverseMap,
    FoldedAlias,
    MergeBand,
    MergeLedgerEntry,
)
from book_graph_rag.domain.models import Entity
from book_graph_rag.domain.quarantine_models import QuarantineDecision, QuarantineRecord
from book_graph_rag.domain.resolution_errors import MergeNotReversible, ResolutionError
from book_graph_rag.domain.resolution_models import (
    ConfidenceBand,
    ResolutionEvidence,
    S0NormalizedForm,
)
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
from book_graph_rag.ports.graph_merge_port import GraphMergePort, InverseMappingSnapshot
from book_graph_rag.ports.merge_ledger_port import MergeLedgerPort
from book_graph_rag.ports.neighborhood_query_port import NeighborhoodQueryPort
from book_graph_rag.ports.quarantine_writer_port import QuarantineWriterPort

# ── Fakes ────────────────────────────────────────────────────────────────────


def _fake_loader(entities: list[Entity]) -> GraphDatabasePort:
    """Cast the fake loader to the port type for mypy."""
    return typing_cast(GraphDatabasePort, FakeEntityLoader(entities))


class FakeEntityLoader:
    """In-memory stand-in for ``GraphDatabasePort.load_active_entities``."""

    def __init__(self, entities: list[Entity]) -> None:
        self._entities = entities
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def load_active_entities(self, *, batch_size: int = 500) -> list[Entity]:
        self.calls.append(("load_active_entities", {"batch_size": batch_size}))
        return list(self._entities)


class FakeRetrieval(CandidateRetrievalPort):
    """Returns a pre-programmed candidate list per anchor."""

    def __init__(self, hits_by_anchor: dict[str, list[CandidateHit]]) -> None:
        self.hits_by_anchor = hits_by_anchor
        self.requests: list[CandidateRetrievalRequest] = []

    async def retrieve(self, request: CandidateRetrievalRequest) -> list[CandidateHit]:
        self.requests.append(request)
        return list(self.hits_by_anchor.get(request.anchor_id, []))

    async def upsert_entity_embedding(self, entity_id: str, vector: EmbeddingVector) -> None:
        pass

    async def ensure_index(self) -> None:
        pass


class FakeEmbedding(EmbeddingProviderPort):
    """No-op embedding provider; analyze() currently drives retrieval directly."""

    async def embed(self, request: EmbeddingRequest) -> EmbeddingBatch:
        return EmbeddingBatch(
            model_id=request.model_id,
            vectors=[EmbeddingVector(values=(0.0, 0.0), model_id=request.model_id)]
            * len(request.texts),
        )

    def model_dim(self, model_id: str) -> int:
        return 2


class FakeNeighborhood(NeighborhoodQueryPort):
    """Returns canned mention/related sets for any entity id."""

    def __init__(
        self,
        mentions: dict[str, set[str]] | None = None,
        related: dict[str, set[str]] | None = None,
    ) -> None:
        self.mentions = mentions or {}
        self.related = related or {}

    async def mention_sources(self, entity_id: str) -> set[str]:
        return set(self.mentions.get(entity_id, set()))

    async def related_neighbors(self, entity_id: str) -> set[str]:
        return set(self.related.get(entity_id, set()))


class FakeQuarantineWriter(QuarantineWriterPort):
    """In-memory quarantine store with atomic-ish updates."""

    def __init__(self) -> None:
        self.records: list[QuarantineRecord] = []
        self.updates: list[tuple[int, QuarantineDecision, str, datetime]] = []

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
        self.updates.append((seq, decision, reviewed_by, reviewed_at))
        for i, record in enumerate(self.records):
            if record.seq == seq:
                self.records[i] = record.model_copy(
                    update={
                        "decision": decision,
                        "reviewed_by": reviewed_by,
                        "reviewed_at": reviewed_at,
                    }
                )
                return
        raise ValueError(f"seq {seq} not found")


class FakeGraphMerge(GraphMergePort):
    """Records merge/rollback calls and simulates reversible state."""

    def __init__(self) -> None:
        self.applied: list[tuple[str, list[str], list[FoldedAlias], InverseMappingSnapshot]] = []
        self.rollbacks: list[MergeLedgerEntry] = []
        self._merged: set[str] = set()
        self._should_raise_on_rollback: set[int] = set()

    async def capture_inverse_mapping(
        self, candidate_ids: list[str]
    ) -> InverseMappingSnapshot:
        return InverseMappingSnapshot(
            aliases_before=dict.fromkeys(candidate_ids, ()),
            edge_inverse_map=[],
        )

    async def apply_merge(
        self,
        canonical_id: str,
        candidate_ids: list[str],
        aliases_folded: list[FoldedAlias],
        inverse_mapping: InverseMappingSnapshot,
    ) -> None:
        self.applied.append((canonical_id, candidate_ids, aliases_folded, inverse_mapping))
        self._merged.update(candidate_ids)

    async def rollback_merge(self, entry: MergeLedgerEntry) -> None:
        if entry.seq in self._should_raise_on_rollback:
            raise MergeNotReversible(f"seq {entry.seq} is no longer reversible")
        still_merged = self._merged & set(entry.candidate_ids)
        if not still_merged:
            raise MergeNotReversible(
                f"candidates {entry.candidate_ids} are not currently merged"
            )
        self.rollbacks.append(entry)
        self._merged.difference_update(entry.candidate_ids)

    def mark_irreversible(self, seq: int) -> None:
        self._should_raise_on_rollback.add(seq)


class FakeMergeLedger(MergeLedgerPort):
    """In-memory ledger that chains hashes like the real JSONL adapter."""

    def __init__(self) -> None:
        self.entries: list[MergeLedgerEntry] = []
        self.verified: int = 0
        self._broken: bool = False

    def append(self, entry: MergeLedgerEntry) -> None:
        from book_graph_rag.domain.merge_ledger_models import chained_hash

        prev = self.entries[-1].entry_sha256 if self.entries else "0" * 64
        chained = chained_hash(entry, prev)
        self.entries.append(chained)

    def read_all(self) -> list[MergeLedgerEntry]:
        return list(self.entries)

    def read_by_seq(self, seq: int) -> MergeLedgerEntry | None:
        for entry in self.entries:
            if entry.seq == seq:
                return entry
        return None

    def verify_chain(self) -> None:
        self.verified += 1
        if self._broken:
            from book_graph_rag.domain.resolution_errors import LedgerChainBroken

            raise LedgerChainBroken("fake chain broken")

    def break_chain(self) -> None:
        self._broken = True


# ── Entity fixtures ──────────────────────────────────────────────────────────


def _entity(
    entity_id: str,
    name: str,
    entity_type: str,
    aliases: tuple[str, ...] = (),
    canonical_name: str | None = None,
) -> Entity:
    return Entity(
        id=entity_id,
        name=name,
        type=entity_type,  # type: ignore[arg-type]
        aliases=list(aliases),
        canonical_name=canonical_name,
    )


def _norm(text: str) -> S0NormalizedForm:
    folded = text.casefold()
    return S0NormalizedForm(
        original=text,
        nfkc=text,
        casefold=folded,
        compact=folded,
        tokens=(folded,),
    )


# ── ResolveEntitiesUseCase tests ─────────────────────────────────────────────


async def test_resolve_routes_exact_to_auto_merge_and_medium_to_quarantine() -> None:
    """Analyzer returns one auto-merge group (exact) and one quarantine record (medium)."""
    from book_graph_rag.application.resolve_entities_use_case import ResolveEntitiesUseCase

    anchor = _entity("book:ch1:lang-graph", "Lang Graph", "framework")
    exact_dup = _entity(
        "book:ch1:langgraph", "LangGraph", "framework", aliases=("Lang Graph",)
    )
    medium_dup = _entity("book:ch1:lang-graph-tool", "Lang Graph Tool", "framework")

    loader = _fake_loader([anchor, exact_dup, medium_dup])
    retrieval = FakeRetrieval(
        {
            anchor.id: [
                CandidateHit(
                    candidate_id=exact_dup.id,
                    cosine_similarity=0.99,
                    candidate_type=exact_dup.type,
                    candidate_namespace="book:ch1",
                ),
                CandidateHit(
                    candidate_id=medium_dup.id,
                    cosine_similarity=0.85,
                    candidate_type=medium_dup.type,
                    candidate_namespace="book:ch1",
                ),
            ]
        }
    )
    neighborhood = FakeNeighborhood(
        mentions={
            anchor.id: {"book:ch1"},
            medium_dup.id: {"book:ch2"},
        },
        related={
            anchor.id: {"book:ch1:other"},
            medium_dup.id: {"book:ch1:other2"},
        },
    )
    quarantine = FakeQuarantineWriter()

    use_case = ResolveEntitiesUseCase(
        retrieval=retrieval,
        embedding=FakeEmbedding(),
        entity_loader=loader,
        neighborhood=neighborhood,
        quarantine_writer=quarantine,
        thresholds=BandThresholds(),
        input_variant="A",
        model_id="fake",
        top_k=10,
        min_similarity=0.5,
    )

    result = await use_case.analyze(dry_run=True)

    assert len(result.auto_merge_groups) == 1
    group = result.auto_merge_groups[0]
    assert group.canonical_id == anchor.id
    assert group.duplicate_ids == [exact_dup.id]
    assert group.band == ConfidenceBand.EXACT

    assert len(result.quarantine_records) == 1
    record = result.quarantine_records[0]
    assert record.anchor_id == anchor.id
    assert record.candidate_id == medium_dup.id
    assert record.band == ConfidenceBand.MEDIUM

    assert len(result.no_merge_candidates) == 0
    assert result.total_pairs_evaluated == 2


async def test_resolve_routes_all_six_matrix_rows() -> None:
    """Full pipeline proves the binding policy matrix for every row."""
    from book_graph_rag.application.resolve_entities_use_case import ResolveEntitiesUseCase

    anchor = _entity("book:ch1:anchor", "Anchor", "framework")
    exact_dup = _entity("book:ch1:exact", "Exact", "framework", aliases=("Anchor",))
    high_dup = _entity("book:ch1:high", "High", "framework")
    medium_dup = _entity("book:ch1:medium", "Medium", "framework")
    low_dup = _entity("book:ch1:low", "Low", "framework")
    cross_type = _entity("book:ch1:cross-type", "Cross Type", "concept")
    cross_ns = _entity("other:ch1:cross-ns", "Cross Namespace", "framework")

    loader = _fake_loader(
        [anchor, exact_dup, high_dup, medium_dup, low_dup, cross_type, cross_ns]
    )

    retrieval = FakeRetrieval(
        {
            anchor.id: [
                CandidateHit(
                    candidate_id=exact_dup.id,
                    cosine_similarity=0.99,
                    candidate_type="framework",
                    candidate_namespace="book:ch1",
                ),
                CandidateHit(
                    candidate_id=high_dup.id,
                    cosine_similarity=0.95,
                    candidate_type="framework",
                    candidate_namespace="book:ch1",
                ),
                CandidateHit(
                    candidate_id=medium_dup.id,
                    cosine_similarity=0.85,
                    candidate_type="framework",
                    candidate_namespace="book:ch1",
                ),
                CandidateHit(
                    candidate_id=low_dup.id,
                    cosine_similarity=0.50,
                    candidate_type="framework",
                    candidate_namespace="book:ch1",
                ),
                CandidateHit(
                    candidate_id=cross_type.id,
                    cosine_similarity=0.92,
                    candidate_type="concept",
                    candidate_namespace="book:ch1",
                ),
                CandidateHit(
                    candidate_id=cross_ns.id,
                    cosine_similarity=0.95,
                    candidate_type="framework",
                    candidate_namespace="other:ch1",
                ),
            ]
        }
    )
    neighborhood = FakeNeighborhood(
        mentions={
            anchor.id: {"src:1"},
            high_dup.id: {"src:1", "src:2"},
            medium_dup.id: {"src:3"},
            low_dup.id: {"src:1"},
            cross_type.id: {"src:1"},
            cross_ns.id: {"src:1"},
        },
        related={
            anchor.id: {"book:ch1:neighbor"},
            high_dup.id: {"book:ch1:neighbor"},
            medium_dup.id: {"book:ch1:other"},
            low_dup.id: {"book:ch1:neighbor"},
            cross_type.id: {"book:ch1:neighbor"},
            cross_ns.id: {"book:ch1:neighbor"},
        },
    )
    quarantine = FakeQuarantineWriter()

    use_case = ResolveEntitiesUseCase(
        retrieval=retrieval,
        embedding=FakeEmbedding(),
        entity_loader=loader,
        neighborhood=neighborhood,
        quarantine_writer=quarantine,
        thresholds=BandThresholds(),
        input_variant="A",
        model_id="fake",
        top_k=10,
        min_similarity=0.0,
    )

    result = await use_case.analyze(dry_run=True)

    assert {g.canonical_id for g in result.auto_merge_groups} == {anchor.id}
    assert all(g.band == ConfidenceBand.EXACT for g in result.auto_merge_groups)
    assert {g.duplicate_ids[0] for g in result.auto_merge_groups} == {exact_dup.id}

    quarantine_ids = {(r.anchor_id, r.candidate_id, r.band) for r in result.quarantine_records}
    assert (anchor.id, high_dup.id, ConfidenceBand.HIGH) in quarantine_ids
    assert (anchor.id, cross_ns.id, ConfidenceBand.HIGH) in quarantine_ids
    assert (anchor.id, medium_dup.id, ConfidenceBand.MEDIUM) in quarantine_ids

    no_merge_ids = {(p.anchor_id, p.candidate_id) for p in result.no_merge_candidates}
    assert (anchor.id, low_dup.id) in no_merge_ids
    assert (anchor.id, cross_type.id) in no_merge_ids

    assert result.total_pairs_evaluated == 6


async def test_resolve_is_idempotent_after_merge() -> None:
    """Once duplicates are soft-deleted, they disappear from analysis."""
    from book_graph_rag.application.resolve_entities_use_case import ResolveEntitiesUseCase

    anchor = _entity("book:ch1:anchor", "Anchor", "framework")
    _ = _entity("book:ch1:dup", "Dup", "framework", aliases=("Anchor",))

    loader = _fake_loader([anchor])  # dup already merged
    retrieval = FakeRetrieval({anchor.id: []})
    quarantine = FakeQuarantineWriter()

    use_case = ResolveEntitiesUseCase(
        retrieval=retrieval,
        embedding=FakeEmbedding(),
        entity_loader=loader,
        neighborhood=FakeNeighborhood(),
        quarantine_writer=quarantine,
        thresholds=BandThresholds(),
        input_variant="A",
        model_id="fake",
        top_k=10,
        min_similarity=0.5,
    )

    result = await use_case.analyze(dry_run=True)

    assert result.auto_merge_groups == []
    assert result.quarantine_records == []
    assert result.no_merge_candidates == []
    assert result.total_pairs_evaluated == 0


async def test_resolve_appends_quarantine_when_not_dry_run() -> None:
    """A non-dry-run analysis persists medium/high/cross-namespace records."""
    from book_graph_rag.application.resolve_entities_use_case import ResolveEntitiesUseCase

    anchor = _entity("book:ch1:anchor", "Anchor", "framework")
    medium_dup = _entity("book:ch1:medium", "Medium", "framework")

    loader = _fake_loader([anchor, medium_dup])
    retrieval = FakeRetrieval(
        {
            anchor.id: [
                CandidateHit(
                    candidate_id=medium_dup.id,
                    cosine_similarity=0.85,
                    candidate_type="framework",
                    candidate_namespace="book:ch1",
                )
            ]
        }
    )
    quarantine = FakeQuarantineWriter()

    use_case = ResolveEntitiesUseCase(
        retrieval=retrieval,
        embedding=FakeEmbedding(),
        entity_loader=loader,
        neighborhood=FakeNeighborhood(
            mentions={anchor.id: {"src:1"}, medium_dup.id: {"src:2"}},
            related={anchor.id: {"n1"}, medium_dup.id: {"n2"}},
        ),
        quarantine_writer=quarantine,
        thresholds=BandThresholds(),
        input_variant="A",
        model_id="fake",
        top_k=10,
        min_similarity=0.5,
    )

    result = await use_case.analyze(dry_run=False)

    assert len(result.quarantine_records) == 1
    assert len(quarantine.records) == 1
    assert quarantine.records[0].seq == 1
    assert quarantine.records[0].candidate_id == medium_dup.id


# ── ApplyMergeUseCase tests ──────────────────────────────────────────────────


async def test_apply_merge_captures_inverse_applies_and_appends_ledger() -> None:
    """ApplyMergeUseCase wires capture → graph merge → ledger append."""
    from book_graph_rag.application.apply_merge_use_case import (
        ApplyMergeUseCase,
        MergeGroup,
    )

    group = MergeGroup(
        canonical_id="book:ch1:canon",
        duplicate_ids=["book:ch1:dup1", "book:ch1:dup2"],
        band=ConfidenceBand.EXACT,
        evidence=[],
    )

    graph_merge = FakeGraphMerge()
    ledger = FakeMergeLedger()
    loader = _fake_loader(
        [
            _entity("book:ch1:canon", "Canon", "framework"),
            _entity("book:ch1:dup1", "Dup1", "framework", aliases=("Alias1",)),
            _entity("book:ch1:dup2", "Dup2", "framework", aliases=("Alias2",)),
        ]
    )

    use_case = ApplyMergeUseCase(
        graph_merge=graph_merge,
        ledger=ledger,
        entity_loader=loader,
    )
    entry = await use_case.apply(group, approver="alice")

    assert len(graph_merge.applied) == 1
    canonical, dups, aliases, inverse = graph_merge.applied[0]
    assert canonical == "book:ch1:canon"
    assert dups == ["book:ch1:dup1", "book:ch1:dup2"]
    assert {a.alias_value for a in aliases} == {"Alias1", "Alias2"}

    assert len(ledger.entries) == 1
    assert ledger.entries[0] is entry
    assert entry.canonical_id == group.canonical_id
    assert entry.candidate_ids == group.duplicate_ids
    assert entry.approver == "alice"
    assert entry.entry_sha256 != ""
    assert entry.prev_seq_sha256 == "0" * 64


async def test_apply_merge_assigns_increasing_seq() -> None:
    """Each new ledger entry receives the next monotonic sequence number."""
    from book_graph_rag.application.apply_merge_use_case import (
        ApplyMergeUseCase,
        MergeGroup,
    )

    use_case = ApplyMergeUseCase(
        graph_merge=FakeGraphMerge(),
        ledger=FakeMergeLedger(),
        entity_loader=_fake_loader(
            [
                _entity("c1", "C1", "framework"),
                _entity("c2", "C2", "framework"),
                _entity("d1", "D1", "framework"),
                _entity("d2", "D2", "framework"),
            ]
        ),
    )

    entry1 = await use_case.apply(
        MergeGroup(canonical_id="c1", duplicate_ids=["d1"], band=ConfidenceBand.EXACT, evidence=[]),
        approver="alice",
    )
    entry2 = await use_case.apply(
        MergeGroup(canonical_id="c2", duplicate_ids=["d2"], band=ConfidenceBand.EXACT, evidence=[]),
        approver="alice",
    )

    assert entry1.seq == 1
    assert entry2.seq == 2
    assert entry2.prev_seq_sha256 == entry1.entry_sha256


# ── ApproveQuarantineUseCase tests ───────────────────────────────────────────


async def test_approve_quarantine_updates_record_and_applies_merge() -> None:
    """Approving a pending quarantine record writes the merge to the ledger."""
    from book_graph_rag.application.apply_merge_use_case import ApplyMergeUseCase
    from book_graph_rag.application.approve_quarantine_use_case import (
        ApproveQuarantineUseCase,
    )

    evidence = ResolutionEvidence(
        anchor_id="book:ch1:anchor",
        candidate_id="book:ch1:dup",
        anchor_type="framework",
        candidate_type="framework",
        anchor_namespace="book:ch1",
        candidate_namespace="book:ch1",
        anchor_normalized=_norm("Anchor"),
        candidate_normalized=_norm("Dup"),
        s0_matched_field="none",
        band=ConfidenceBand.MEDIUM,
        cross_namespace=False,
        cross_type=False,
        decided_at=datetime.now(UTC),
    )

    record = QuarantineRecord(
        seq=1,
        anchor_id="book:ch1:anchor",
        candidate_id="book:ch1:dup",
        canonical_id="book:ch1:anchor",
        band=ConfidenceBand.MEDIUM,
        evidence=evidence,
        created_at=datetime.now(UTC),
    )
    quarantine = FakeQuarantineWriter()
    quarantine.append(record)

    graph_merge = FakeGraphMerge()
    ledger = FakeMergeLedger()
    apply = ApplyMergeUseCase(
        graph_merge=graph_merge,
        ledger=ledger,
        entity_loader=_fake_loader(
            [
                _entity("book:ch1:anchor", "Anchor", "framework"),
                _entity("book:ch1:dup", "Dup", "framework", aliases=("Alias",)),
            ]
        ),
    )
    use_case = ApproveQuarantineUseCase(quarantine=quarantine, apply=apply)

    entry = await use_case.approve(seq=1, reviewer="bob", approver_for_apply="alice")

    assert quarantine.records[0].decision == QuarantineDecision.APPROVED
    assert quarantine.records[0].reviewed_by == "bob"
    assert len(graph_merge.applied) == 1
    assert len(ledger.entries) == 1
    assert ledger.entries[0] is entry


async def test_approve_missing_quarantine_raises_resolution_error() -> None:
    """Approving a non-existent seq fails closed."""
    from book_graph_rag.application.apply_merge_use_case import ApplyMergeUseCase
    from book_graph_rag.application.approve_quarantine_use_case import (
        ApproveQuarantineUseCase,
    )

    quarantine = FakeQuarantineWriter()
    apply = ApplyMergeUseCase(
        graph_merge=FakeGraphMerge(),
        ledger=FakeMergeLedger(),
        entity_loader=_fake_loader([]),
    )
    use_case = ApproveQuarantineUseCase(quarantine=quarantine, apply=apply)

    with pytest.raises(ResolutionError):
        await use_case.approve(seq=999, reviewer="bob", approver_for_apply="alice")


# ── RollbackMergeUseCase tests ───────────────────────────────────────────────


async def test_rollback_verifies_chain_reverses_and_appends_compensating_entry() -> None:
    """Rollback appends a compensating ledger entry with rollback_of set."""
    from book_graph_rag.application.rollback_merge_use_case import (
        RollbackMergeUseCase,
    )

    original = MergeLedgerEntry(
        seq=1,
        candidate_ids=["book:ch1:dup"],
        canonical_id="book:ch1:canon",
        band=MergeBand.HIGH,
        evidence=[],
        aliases_folded=[FoldedAlias(from_entity_id="book:ch1:dup", alias_value="Alias")],
        edge_inverse_map=[
            EdgeInverseMap(
                edge_kind="MENTIONS",
                duplicate_entity_id="book:ch1:dup",
                original_other_endpoint_id="chunk-1",
                edge_properties={},
            )
        ],
        approver="alice",
        applied_at=datetime.now(UTC),
    )
    ledger = FakeMergeLedger()
    ledger.append(original)
    original = ledger.entries[0]

    graph_merge = FakeGraphMerge()
    graph_merge._merged.add("book:ch1:dup")

    use_case = RollbackMergeUseCase(ledger=ledger, graph_merge=graph_merge)
    await use_case.rollback(seq=1)

    assert ledger.verified == 1
    assert len(graph_merge.rollbacks) == 1
    assert len(ledger.entries) == 2
    compensating = ledger.entries[1]
    assert compensating.rollback_of == 1
    assert compensating.candidate_ids == original.candidate_ids
    assert compensating.canonical_id == original.canonical_id
    assert compensating.entry_sha256 != ""
    assert compensating.prev_seq_sha256 == original.entry_sha256


async def test_rollback_fails_closed_on_broken_chain() -> None:
    """A tampered ledger blocks rollback before any graph mutation."""
    from book_graph_rag.application.rollback_merge_use_case import (
        RollbackMergeUseCase,
    )
    from book_graph_rag.domain.resolution_errors import LedgerChainBroken

    ledger = FakeMergeLedger()
    ledger.append(
        MergeLedgerEntry(
            seq=1,
            candidate_ids=["d1"],
            canonical_id="c1",
            band=MergeBand.HIGH,
            evidence=[],
            aliases_folded=[],
            edge_inverse_map=[],
            approver="alice",
            applied_at=datetime.now(UTC),
        )
    )
    ledger.break_chain()

    use_case = RollbackMergeUseCase(ledger=ledger, graph_merge=FakeGraphMerge())
    with pytest.raises(LedgerChainBroken):
        await use_case.rollback(seq=1)


async def test_rollback_is_idempotent_second_call_is_noop() -> None:
    """Rolling back twice is a safe no-op (spec R6.5 idempotence)."""
    from book_graph_rag.application.rollback_merge_use_case import (
        RollbackMergeUseCase,
    )

    ledger = FakeMergeLedger()
    ledger.append(
        MergeLedgerEntry(
            seq=1,
            candidate_ids=["d1"],
            canonical_id="c1",
            band=MergeBand.HIGH,
            evidence=[],
            aliases_folded=[],
            edge_inverse_map=[],
            approver="alice",
            applied_at=datetime.now(UTC),
        )
    )

    graph_merge = FakeGraphMerge()
    graph_merge._merged.add("d1")
    use_case = RollbackMergeUseCase(ledger=ledger, graph_merge=graph_merge)

    await use_case.rollback(seq=1)
    assert len(ledger.entries) == 2

    # Second call: compensating entry already exists -> safe no-op.
    await use_case.rollback(seq=1)
    assert len(ledger.entries) == 2  # no extra compensating entry


async def test_rollback_missing_seq_raises_rollback_target_invalid() -> None:
    """Rolling back an unknown seq is rejected before the graph is touched."""
    from book_graph_rag.application.rollback_merge_use_case import (
        RollbackMergeUseCase,
    )
    from book_graph_rag.domain.resolution_errors import RollbackTargetInvalid

    ledger = FakeMergeLedger()
    use_case = RollbackMergeUseCase(ledger=ledger, graph_merge=FakeGraphMerge())

    with pytest.raises(RollbackTargetInvalid):
        await use_case.rollback(seq=42)
