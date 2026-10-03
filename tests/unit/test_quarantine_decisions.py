"""Cross-namespace quarantine decisions (T6b): enqueue, approve, reject.

No database: graph reads come from a stub ``QuarantineReviewPort`` while
persistence uses the real ``JSONLQuarantineWriter`` over ``tmp_path`` files, so
idempotence, ordering, the §7.2 gate wording and every "refusal leaves the file
untouched" claim are asserted against real bytes on disk.

Covered contract:

* ``enqueue`` detects the audit's cross-namespace groups, orders candidates by
  ``description_overlap`` desc, skips every pair that already has a record
  (pending *or* rejected) unless ``force``, writes ``band=medium`` records with
  real S0/S2/S3 evidence (``s1=None``: cosine never computed) and writes
  nothing under ``dry_run``;
* ``approve`` validates the AGENTS.md §7.2 gate first (approval file with
  ``approve``, backup ≤ 24 h), refuses unknown/already-decided/canonical-less
  or non-reviewable records *before* touching the file, accepts a
  cross-namespace ``exact`` band (R6.2: quarantine regardless of band) while
  still refusing an intra-namespace ``low`` band;
* ``reject`` demands a non-empty ``--reason`` recorded as ``review_note`` and
  refuses to re-decide a record.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from click.testing import CliRunner

from book_graph_rag.application.apply_merge_use_case import ApplyMergeUseCase
from book_graph_rag.application.approve_quarantine_use_case import (
    ApproveQuarantineUseCase,
    QuarantineDecisionOutcome,
)
from book_graph_rag.application.enqueue_cross_namespace_quarantine_use_case import (
    EnqueueCrossNamespaceQuarantineUseCase,
)
from book_graph_rag.domain.duplicate_grouping import choose_canonical_id
from book_graph_rag.domain.merge_ledger_models import FoldedAlias, MergeLedgerEntry, chained_hash
from book_graph_rag.domain.models import Entity
from book_graph_rag.domain.quarantine_models import QuarantineDecision, QuarantineRecord
from book_graph_rag.domain.quarantine_review_models import (
    CrossNamespaceCandidateGroup,
    EntityReviewFacts,
    PairReviewFacts,
)
from book_graph_rag.domain.resolution_errors import ResolutionError
from book_graph_rag.domain.resolution_models import ConfidenceBand
from book_graph_rag.domain.s0_normalization import namespace_from_id, s0_match
from book_graph_rag.infrastructure.jsonl_quarantine_writer import JSONLQuarantineWriter
from book_graph_rag.main import cli
from book_graph_rag.ports.graph_db_port import GraphDatabasePort
from book_graph_rag.ports.graph_merge_port import GraphMergePort, InverseMappingSnapshot
from book_graph_rag.ports.merge_ledger_port import MergeLedgerPort
from book_graph_rag.ports.quarantine_review_port import QuarantineReviewPort

# ── Seed entities (two namespaces per pair) ─────────────────────────────────

_R1 = "knowledge:alpha:retrieval-pipeline"
_R2 = "knowledge:beta:retrieval-pipeline"
_G1 = "knowledge:alpha:agent"
_G2 = "knowledge:essential:agent"
_E1 = "knowledge:alpha:evaluation-pipeline"
_E2 = "knowledge:work:evaluation-pipeline"

_R_DESCRIPTION_A = "Pipeline de recuperación que fusiona grafo y vector para responder preguntas."
_R_DESCRIPTION_B = "Pipeline de recuperación que fusiona grafo y vector para responder consultas."
_G_DESCRIPTION_A = "Entidad que utiliza herramientas y planificación para un objetivo."
_G_DESCRIPTION_B = "An AI agent measured by benchmark evaluation scores."


def _entity(
    entity_id: str,
    name: str,
    *,
    description: str = "",
    entity_type: str = "concept",
    aliases: tuple[str, ...] = (),
) -> Entity:
    return Entity(
        id=entity_id,
        name=name,
        type=entity_type,  # type: ignore[arg-type]
        description=description,
        aliases=list(aliases),
    )


def _retrieval_pair() -> tuple[Entity, Entity]:
    return (
        _entity(_R1, "Retrieval Pipeline", description=_R_DESCRIPTION_A),
        _entity(_R2, "Retrieval Pipeline", description=_R_DESCRIPTION_B),
    )


def _generic_pair() -> tuple[Entity, Entity]:
    return (
        _entity(_G1, "Agent", description=_G_DESCRIPTION_A, entity_type="component"),
        _entity(_G2, "Agent", description=_G_DESCRIPTION_B, entity_type="component"),
    )


def _evaluation_pair() -> tuple[Entity, Entity]:
    return (
        _entity(_E1, "Evaluation Pipeline", description="Sistema de evaluación de calidad."),
        _entity(_E2, "Evaluation Pipeline", description="Evaluation quality harness."),
    )


def _group(key: str, entity_type: str, *member_ids: str) -> CrossNamespaceCandidateGroup:
    return CrossNamespaceCandidateGroup(
        group_key=key,
        entity_type=entity_type,
        member_ids=tuple(member_ids),
        namespaces=tuple(sorted({namespace_from_id(member_id) for member_id in member_ids})),
    )


def _record(
    anchor: Entity,
    candidate: Entity,
    *,
    seq: int,
    band: ConfidenceBand,
    canonical_id: str | None = None,
) -> QuarantineRecord:
    """Orient the record so the anchor is the canonical id (enqueue rule).

    ``canonical_id`` follows ``choose_canonical_id`` and the anchor is the
    detected pair member chosen by the same rule, so ``candidate_id`` can
    never equal ``canonical_id`` — approving would otherwise stage a
    self-merge.
    """
    canonical = (
        choose_canonical_id([anchor.id, candidate.id]) if canonical_id is None else canonical_id
    )
    if anchor.id == canonical:
        anchor_out, candidate_out = anchor, candidate
    elif candidate.id == canonical:
        anchor_out, candidate_out = candidate, anchor
    else:
        anchor_out, candidate_out = anchor, candidate
    return QuarantineRecord(
        seq=seq,
        anchor_id=anchor_out.id,
        candidate_id=candidate_out.id,
        canonical_id=canonical,
        band=band,
        evidence=s0_match(anchor_out, candidate_out),
        created_at=datetime(2026, 10, 3, 18, 22, tzinfo=UTC),
    )


# ── Stub review port (no database) ──────────────────────────────────────────


def _facts(entity: Entity) -> EntityReviewFacts:
    return EntityReviewFacts(
        entity=entity,
        mention_source_ids=(namespace_from_id(entity.id),),
        mention_chunk_count=1,
        neighbor_ids=(),
        mention_context="",
    )


class _StubReviewPort(QuarantineReviewPort):
    """Canned detection groups and pair facts; never touches a graph."""

    def __init__(
        self,
        groups: list[CrossNamespaceCandidateGroup],
        entities: list[Entity],
    ) -> None:
        self.groups = groups
        self.entities = {entity.id: entity for entity in entities}
        self.pair_reads: list[tuple[str, str]] = []

    async def find_cross_namespace_candidate_groups(self) -> list[CrossNamespaceCandidateGroup]:
        return list(self.groups)

    async def read_descriptions(self, entity_ids: Sequence[str]) -> dict[str, str]:
        return {
            entity_id: self.entities[entity_id].description
            for entity_id in entity_ids
            if entity_id in self.entities
        }

    async def read_pair_facts(self, anchor_id: str, candidate_id: str) -> PairReviewFacts:
        self.pair_reads.append((anchor_id, candidate_id))
        if anchor_id not in self.entities or candidate_id not in self.entities:
            raise LookupError(f"Entity id(s) not found in the graph: {anchor_id}, {candidate_id}")
        return PairReviewFacts(
            anchor=_facts(self.entities[anchor_id]),
            candidate=_facts(self.entities[candidate_id]),
        )

    async def close(self) -> None:
        return None


def _enqueue_use_case(
    groups: list[CrossNamespaceCandidateGroup],
    entities: list[Entity],
    quarantine_path: Path,
) -> EnqueueCrossNamespaceQuarantineUseCase:
    return EnqueueCrossNamespaceQuarantineUseCase(
        quarantine=JSONLQuarantineWriter(quarantine_path),
        review=_StubReviewPort(groups, entities),
    )


def _all_entities() -> list[Entity]:
    return [*_retrieval_pair(), *_generic_pair(), *_evaluation_pair()]


# ── Fake apply ports (approve uses the real ApplyMergeUseCase) ──────────────


class _RecordingGraphMerge(GraphMergePort):
    def __init__(self) -> None:
        self.applied: list[tuple[str, list[str]]] = []

    async def capture_inverse_mapping(self, candidate_ids: list[str]) -> InverseMappingSnapshot:
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
        self.applied.append((canonical_id, list(candidate_ids)))

    async def rollback_merge(self, entry: MergeLedgerEntry) -> None:
        raise NotImplementedError("rollback is not exercised by these tests")


class _InMemoryLedger(MergeLedgerPort):
    def __init__(self) -> None:
        self.entries: list[MergeLedgerEntry] = []

    def append(self, entry: MergeLedgerEntry) -> None:
        previous = self.entries[-1].entry_sha256 if self.entries else "0" * 64
        self.entries.append(chained_hash(entry, previous))

    def read_all(self) -> list[MergeLedgerEntry]:
        return list(self.entries)

    def read_by_seq(self, seq: int) -> MergeLedgerEntry | None:
        return next((entry for entry in self.entries if entry.seq == seq), None)

    def verify_chain(self) -> None:
        return None


class _StubEntityLoader:
    def __init__(self, entities: list[Entity]) -> None:
        self._entities = entities

    async def load_active_entities(self, *, batch_size: int = 500) -> list[Entity]:
        return list(self._entities)


def _approve_use_case(
    quarantine_path: Path,
    entities: list[Entity],
) -> tuple[ApproveQuarantineUseCase, _RecordingGraphMerge, _InMemoryLedger]:
    graph_merge = _RecordingGraphMerge()
    ledger = _InMemoryLedger()
    use_case = ApproveQuarantineUseCase(
        quarantine=JSONLQuarantineWriter(quarantine_path),
        apply=ApplyMergeUseCase(
            graph_merge=graph_merge,
            ledger=ledger,
            entity_loader=cast(GraphDatabasePort, _StubEntityLoader(entities)),
        ),
    )
    return use_case, graph_merge, ledger


def _read_bytes(path: Path) -> bytes:
    return path.read_bytes() if path.exists() else b""


# ── enqueue: detection → ordering → idempotence ─────────────────────────────


async def test_enqueue_writes_medium_records_with_real_scoring_and_no_cosine(
    tmp_path: Path,
) -> None:
    """A fresh pair becomes one pending ``band=medium`` record with S0/S2/S3."""
    quarantine_path = tmp_path / "quarantine.jsonl"
    anchor, candidate = _retrieval_pair()
    use_case = _enqueue_use_case(
        [_group("retrieval pipeline", "concept", _R1, _R2)],
        [anchor, candidate],
        quarantine_path,
    )

    result = await use_case.enqueue()

    assert result.groups_found == 1
    assert result.pairs_found == 1
    assert result.enqueued == 1
    assert result.skipped_existing == 0
    assert (result.seq_first, result.seq_last) == (1, 1)
    assert result.cosine_computed is False

    records = JSONLQuarantineWriter(quarantine_path).read_all()
    assert len(records) == 1
    record = records[0]
    assert record.decision is QuarantineDecision.PENDING
    assert record.band is ConfidenceBand.MEDIUM
    # canonical follows the project rule; anchor/candidate are the detected pair.
    expected_canonical = choose_canonical_id([anchor.id, candidate.id])
    assert record.canonical_id == expected_canonical
    assert {record.anchor_id, record.candidate_id} == {anchor.id, candidate.id}
    # Orientation invariant: approving can never stage a self-merge.
    assert record.candidate_id != record.canonical_id
    # Real domain scoring: S0 builder + S2 gate + three overlaps, no S1/cosine.
    assert record.evidence.s1 is None
    assert record.evidence.s2 is not None
    assert record.evidence.s3 is not None
    assert record.evidence.composite_score is not None
    assert record.evidence.band is ConfidenceBand.MEDIUM
    assert record.evidence.cross_namespace is True


async def test_enqueue_orders_by_description_overlap_descending_and_honours_limit(
    tmp_path: Path,
) -> None:
    """``--limit N`` keeps the most promising pairs first (design §4.1)."""
    quarantine_path = tmp_path / "quarantine.jsonl"
    use_case = _enqueue_use_case(
        [
            _group("agent", "component", _G1, _G2),
            _group("retrieval pipeline", "concept", _R1, _R2),
        ],
        _all_entities(),
        quarantine_path,
    )

    result = await use_case.enqueue(limit=1)

    assert result.enqueued == 1
    assert result.pairs_found == 2
    records = JSONLQuarantineWriter(quarantine_path).read_all()
    assert len(records) == 1
    # The high-overlap retrieval pair outranks the disjoint generic pair.
    assert {records[0].anchor_id, records[0].candidate_id} == {_R1, _R2}


async def test_enqueue_generic_only_keeps_single_word_labels(tmp_path: Path) -> None:
    """``--generic-only`` restricts the batch to single-word labels."""
    quarantine_path = tmp_path / "quarantine.jsonl"
    use_case = _enqueue_use_case(
        [
            _group("retrieval pipeline", "concept", _R1, _R2),
            _group("agent", "component", _G1, _G2),
            _group("evaluation pipeline", "concept", _E1, _E2),
        ],
        _all_entities(),
        quarantine_path,
    )

    result = await use_case.enqueue(generic_only=True)

    assert result.pairs_found == 1
    assert result.enqueued == 1
    records = JSONLQuarantineWriter(quarantine_path).read_all()
    assert {records[0].anchor_id, records[0].candidate_id} == {_G1, _G2}


async def test_enqueue_namespace_filter_keeps_pairs_touching_the_namespace(
    tmp_path: Path,
) -> None:
    """``--namespace X`` keeps only pairs involving that namespace."""
    quarantine_path = tmp_path / "quarantine.jsonl"
    use_case = _enqueue_use_case(
        [
            _group("retrieval pipeline", "concept", _R1, _R2),
            _group("evaluation pipeline", "concept", _E1, _E2),
        ],
        _all_entities(),
        quarantine_path,
    )

    result = await use_case.enqueue(namespace="knowledge:work")

    assert result.pairs_found == 1
    records = JSONLQuarantineWriter(quarantine_path).read_all()
    assert {records[0].anchor_id, records[0].candidate_id} == {_E1, _E2}


async def test_enqueue_skips_pairs_that_already_have_a_record(
    tmp_path: Path,
) -> None:
    """Idempotence: pending and rejected records both block a re-enqueue."""
    quarantine_path = tmp_path / "quarantine.jsonl"
    writer = JSONLQuarantineWriter(quarantine_path)
    anchor, candidate = _retrieval_pair()
    generic_anchor, generic_candidate = _generic_pair()
    writer.append(_record(anchor, candidate, seq=1, band=ConfidenceBand.MEDIUM))
    rejected = _record(generic_anchor, generic_candidate, seq=2, band=ConfidenceBand.MEDIUM)
    writer.append(rejected)
    writer.update_decision_with_note(
        seq=2,
        decision=QuarantineDecision.REJECTED,
        reviewed_by="human:tester",
        reviewed_at=datetime.now(UTC),
        review_note="label collision: author-specific framing",
    )

    use_case = _enqueue_use_case(
        [
            _group("retrieval pipeline", "concept", _R1, _R2),
            _group("agent", "component", _G1, _G2),
        ],
        _all_entities(),
        quarantine_path,
    )

    result = await use_case.enqueue()

    assert result.pairs_found == 2
    assert result.skipped_existing == 2
    assert result.enqueued == 0
    assert len(writer.read_all()) == 2  # no new lines

    # ``--force`` re-enqueues even a rejected pair (documented escape hatch).
    forced = await use_case.enqueue(force=True)
    assert forced.enqueued == 2
    assert forced.skipped_existing == 0
    assert len(writer.read_all()) == 4


async def test_enqueue_dry_run_reports_the_outcome_and_writes_nothing(
    tmp_path: Path,
) -> None:
    """``--dry-run`` prints what would be written without touching the file."""
    quarantine_path = tmp_path / "quarantine.jsonl"
    use_case = _enqueue_use_case(
        [_group("retrieval pipeline", "concept", _R1, _R2)],
        _all_entities(),
        quarantine_path,
    )

    result = await use_case.enqueue(dry_run=True)

    assert result.dry_run is True
    assert result.enqueued == 1
    assert (result.seq_first, result.seq_last) == (1, 1)
    assert not quarantine_path.exists()
    assert JSONLQuarantineWriter(quarantine_path).read_all() == []


# ── approve: §7.2 gate + band rule, refusals leave the file untouched ───────


async def test_approve_accepts_cross_namespace_exact_band_record(tmp_path: Path) -> None:
    """R6.2: a cross-namespace record is approvable regardless of its band."""
    quarantine_path = tmp_path / "quarantine.jsonl"
    writer = JSONLQuarantineWriter(quarantine_path)
    anchor, candidate = _retrieval_pair()
    record = _record(anchor, candidate, seq=1, band=ConfidenceBand.EXACT)
    assert record.evidence.cross_namespace is True  # S0 short-circuit class
    writer.append(record)
    use_case, graph_merge, ledger = _approve_use_case(quarantine_path, [anchor, candidate])

    entry = await use_case.approve(
        seq=1, reviewer="human:tester", approver_for_apply="human:tester"
    )

    stored = writer.read_all()[0]
    assert stored.decision is QuarantineDecision.APPROVED
    assert stored.reviewed_by == "human:tester"
    assert stored.reviewed_at is not None
    canonical = choose_canonical_id([anchor.id, candidate.id])
    assert graph_merge.applied == [(canonical, [record.candidate_id])]
    assert ledger.entries[-1].seq == entry.seq


async def test_approve_refuses_intra_namespace_low_band(tmp_path: Path) -> None:
    """A same-namespace ``low`` band still cannot be approved (band rule)."""
    quarantine_path = tmp_path / "quarantine.jsonl"
    writer = JSONLQuarantineWriter(quarantine_path)
    anchor = _entity("book:ch1:alpha-thing", "Alpha Thing")
    candidate = _entity("book:ch1:beta-thing", "Beta Thing")
    writer.append(_record(anchor, candidate, seq=1, band=ConfidenceBand.LOW))
    use_case, graph_merge, _ledger = _approve_use_case(quarantine_path, [anchor, candidate])
    before = _read_bytes(quarantine_path)

    with pytest.raises(ResolutionError, match="non-reviewable band"):
        await use_case.approve(seq=1, reviewer="human:tester", approver_for_apply="human:tester")

    assert _read_bytes(quarantine_path) == before
    assert writer.read_all()[0].decision is QuarantineDecision.PENDING
    assert graph_merge.applied == []


async def test_approve_refuses_unknown_seq_and_leaves_file_untouched(
    tmp_path: Path,
) -> None:
    quarantine_path = tmp_path / "quarantine.jsonl"
    writer = JSONLQuarantineWriter(quarantine_path)
    anchor, candidate = _retrieval_pair()
    writer.append(_record(anchor, candidate, seq=1, band=ConfidenceBand.MEDIUM))
    use_case, graph_merge, _ledger = _approve_use_case(quarantine_path, [anchor, candidate])
    before = _read_bytes(quarantine_path)

    with pytest.raises(ResolutionError, match="999"):
        await use_case.approve(seq=999, reviewer="human:tester", approver_for_apply="human:tester")

    assert _read_bytes(quarantine_path) == before
    assert graph_merge.applied == []


async def test_approve_refuses_already_decided_seq_and_leaves_file_untouched(
    tmp_path: Path,
) -> None:
    quarantine_path = tmp_path / "quarantine.jsonl"
    writer = JSONLQuarantineWriter(quarantine_path)
    anchor, candidate = _retrieval_pair()
    writer.append(_record(anchor, candidate, seq=1, band=ConfidenceBand.MEDIUM))
    writer.update_decision(
        seq=1,
        decision=QuarantineDecision.REJECTED,
        reviewed_by="human:tester",
        reviewed_at=datetime.now(UTC),
    )
    use_case, graph_merge, _ledger = _approve_use_case(quarantine_path, [anchor, candidate])
    before = _read_bytes(quarantine_path)

    with pytest.raises(ResolutionError, match="already decided"):
        await use_case.approve(seq=1, reviewer="human:tester", approver_for_apply="human:tester")

    assert _read_bytes(quarantine_path) == before
    assert graph_merge.applied == []


async def test_approve_refuses_missing_canonical_and_leaves_file_untouched(
    tmp_path: Path,
) -> None:
    quarantine_path = tmp_path / "quarantine.jsonl"
    writer = JSONLQuarantineWriter(quarantine_path)
    anchor, candidate = _retrieval_pair()
    writer.append(
        _record(anchor, candidate, seq=1, band=ConfidenceBand.MEDIUM).model_copy(
            update={"canonical_id": None}
        )
    )
    use_case, graph_merge, _ledger = _approve_use_case(quarantine_path, [anchor, candidate])
    before = _read_bytes(quarantine_path)

    with pytest.raises(ResolutionError, match="canonical"):
        await use_case.approve(seq=1, reviewer="human:tester", approver_for_apply="human:tester")

    assert _read_bytes(quarantine_path) == before
    assert graph_merge.applied == []


async def test_approve_many_validates_every_seq_before_writing(tmp_path: Path) -> None:
    """One bad seq in the batch refuses the whole batch with no partial write."""
    quarantine_path = tmp_path / "quarantine.jsonl"
    writer = JSONLQuarantineWriter(quarantine_path)
    anchor, candidate = _retrieval_pair()
    writer.append(_record(anchor, candidate, seq=1, band=ConfidenceBand.MEDIUM))
    use_case, graph_merge, _ledger = _approve_use_case(quarantine_path, [anchor, candidate])
    before = _read_bytes(quarantine_path)

    with pytest.raises(ResolutionError, match="999"):
        await use_case.approve_many(
            seqs=[1, 999],
            reviewer="human:tester",
            approver_for_apply="human:tester",
        )

    assert _read_bytes(quarantine_path) == before
    assert writer.read_all()[0].decision is QuarantineDecision.PENDING
    assert graph_merge.applied == []


async def test_approve_many_returns_one_outcome_per_seq(tmp_path: Path) -> None:
    quarantine_path = tmp_path / "quarantine.jsonl"
    writer = JSONLQuarantineWriter(quarantine_path)
    first_anchor, first_candidate = _retrieval_pair()
    second_anchor, second_candidate = _generic_pair()
    writer.append(_record(first_anchor, first_candidate, seq=1, band=ConfidenceBand.MEDIUM))
    writer.append(_record(second_anchor, second_candidate, seq=2, band=ConfidenceBand.HIGH))
    use_case, _graph_merge, _ledger = _approve_use_case(
        quarantine_path, [first_anchor, first_candidate, second_anchor, second_candidate]
    )

    outcomes = await use_case.approve_many(
        seqs=[1, 2],
        reviewer="human:tester",
        approver_for_apply="human:tester",
    )

    assert [outcome.seq for outcome in outcomes] == [1, 2]
    assert all(outcome.decision is QuarantineDecision.APPROVED for outcome in outcomes)
    assert [outcome.ledger_seq for outcome in outcomes] == [1, 2]


# ── reject: mandatory reason recorded as review_note ────────────────────────


def test_reject_requires_a_non_empty_reason(tmp_path: Path) -> None:
    quarantine_path = tmp_path / "quarantine.jsonl"
    writer = JSONLQuarantineWriter(quarantine_path)
    anchor, candidate = _retrieval_pair()
    writer.append(_record(anchor, candidate, seq=1, band=ConfidenceBand.MEDIUM))
    use_case = ApproveQuarantineUseCase(quarantine=writer, apply=None)
    before = _read_bytes(quarantine_path)

    for reason in ("", "   "):
        with pytest.raises(ResolutionError, match="reason"):
            use_case.reject(seqs=[1], reviewer="human:tester", reason=reason)

    assert _read_bytes(quarantine_path) == before


def test_reject_records_decision_reviewer_and_note(tmp_path: Path) -> None:
    quarantine_path = tmp_path / "quarantine.jsonl"
    writer = JSONLQuarantineWriter(quarantine_path)
    anchor, candidate = _retrieval_pair()
    writer.append(_record(anchor, candidate, seq=1, band=ConfidenceBand.MEDIUM))
    use_case = ApproveQuarantineUseCase(quarantine=writer, apply=None)

    outcomes = use_case.reject(
        seqs=[1],
        reviewer="human:tester",
        reason="label collision: different framings",
    )

    assert [outcome.decision for outcome in outcomes] == [QuarantineDecision.REJECTED]
    stored = writer.read_all()[0]
    assert stored.decision is QuarantineDecision.REJECTED
    assert stored.reviewed_by == "human:tester"
    assert stored.reviewed_at is not None
    assert stored.review_note == "label collision: different framings"
    # The note survives the JSONL round trip and is part of the record line.
    line = quarantine_path.read_text(encoding="utf-8").strip()
    assert "label collision: different framings" in line


def test_reject_refuses_already_decided_record_and_leaves_file_untouched(
    tmp_path: Path,
) -> None:
    quarantine_path = tmp_path / "quarantine.jsonl"
    writer = JSONLQuarantineWriter(quarantine_path)
    anchor, candidate = _retrieval_pair()
    writer.append(_record(anchor, candidate, seq=1, band=ConfidenceBand.MEDIUM))
    writer.update_decision(
        seq=1,
        decision=QuarantineDecision.APPROVED,
        reviewed_by="human:tester",
        reviewed_at=datetime.now(UTC),
    )
    use_case = ApproveQuarantineUseCase(quarantine=writer, apply=None)
    before = _read_bytes(quarantine_path)

    with pytest.raises(ResolutionError, match="already decided"):
        use_case.reject(seqs=[1], reviewer="human:tester", reason="because")

    assert _read_bytes(quarantine_path) == before


def test_review_note_is_optional_and_omitted_from_old_format_lines(
    tmp_path: Path,
) -> None:
    """Backwards compatibility: no note → byte-identical legacy line; old
    lines without the key keep parsing."""
    quarantine_path = tmp_path / "quarantine.jsonl"
    writer = JSONLQuarantineWriter(quarantine_path)
    anchor, candidate = _retrieval_pair()
    writer.append(_record(anchor, candidate, seq=1, band=ConfidenceBand.MEDIUM))

    fresh_line = quarantine_path.read_text(encoding="utf-8").splitlines()[0]
    assert "review_note" not in fresh_line

    legacy_line = (
        '{"schema_version":"1.0.0","seq":7,"anchor_id":"a:one:x","candidate_id":"a:two:y",'
        '"canonical_id":"a:one:x","band":"medium","evidence":'
        + s0_match(anchor, candidate).model_dump_json()
        + ',"created_at":"2026-10-03T18:22:00Z","decision":"pending"}'
    )
    with quarantine_path.open("a", encoding="utf-8") as handle:
        handle.write(legacy_line + "\n")

    records = writer.read_all()
    assert [record.seq for record in records] == [1, 7]
    assert records[1].review_note is None


# ── CLI layer: §7.2 gate wording, argument validation, output ───────────────


def test_cli_enqueue_requires_the_cross_namespace_mode() -> None:
    result = CliRunner().invoke(cli, ["quarantine", "enqueue"])
    assert result.exit_code != 0
    assert "--cross-namespace" in result.output


def test_cli_reject_requires_a_non_empty_reason() -> None:
    result = CliRunner().invoke(cli, ["quarantine", "reject", "--seq", "1", "--reason", "   "])
    assert result.exit_code != 0
    assert "--reason" in result.output

    missing = CliRunner().invoke(cli, ["quarantine", "reject", "--seq", "1"])
    assert missing.exit_code != 0
    assert "--reason" in missing.output


def test_cli_approve_requires_at_least_one_seq() -> None:
    result = CliRunner().invoke(cli, ["quarantine", "approve"])
    assert result.exit_code != 0
    assert "--seq" in result.output


def test_cli_approve_gate_missing_approval_file(tmp_path: Path) -> None:
    backup = tmp_path / "backup.json"
    backup.write_text("{}", encoding="utf-8")

    result = CliRunner().invoke(
        cli,
        [
            "quarantine",
            "approve",
            "--seq",
            "1",
            "--backup",
            str(backup),
            "--approval",
            str(tmp_path / "missing.txt"),
        ],
    )

    assert result.exit_code == 1
    assert "APPROVE abortado: falta --approval" in result.output


def test_cli_approve_gate_approval_file_without_the_word(tmp_path: Path) -> None:
    backup = tmp_path / "backup.json"
    backup.write_text("{}", encoding="utf-8")
    approval = tmp_path / "approval.txt"
    approval.write_text("looks good to me", encoding="utf-8")

    result = CliRunner().invoke(
        cli,
        [
            "quarantine",
            "approve",
            "--seq",
            "1",
            "--backup",
            str(backup),
            "--approval",
            str(approval),
        ],
    )

    assert result.exit_code == 1
    assert "debe contener la palabra 'approve'" in result.output


def test_cli_approve_gate_missing_backup(tmp_path: Path) -> None:
    approval = tmp_path / "approval.txt"
    approval.write_text("approve", encoding="utf-8")

    result = CliRunner().invoke(
        cli,
        [
            "quarantine",
            "approve",
            "--seq",
            "1",
            "--approval",
            str(approval),
        ],
    )

    assert result.exit_code == 1
    assert "APPROVE abortado: falta --backup" in result.output


def test_cli_approve_gate_stale_backup_needs_allow_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup = tmp_path / "backup.json"
    backup.write_text("{}", encoding="utf-8")
    approval = tmp_path / "approval.txt"
    approval.write_text("approve", encoding="utf-8")
    stale = datetime.now(UTC).timestamp() - 25 * 3600
    os.utime(backup, (stale, stale))

    stale_result = CliRunner().invoke(
        cli,
        [
            "quarantine",
            "approve",
            "--seq",
            "1",
            "--backup",
            str(backup),
            "--approval",
            str(approval),
        ],
    )
    assert stale_result.exit_code == 1
    assert "APPROVE abortado: el backup tiene" in stale_result.output
    assert "--allow-stale-backup" in stale_result.output

    # With the override the gate passes: the command proceeds to Settings.
    class _ExplodingSettings:
        @classmethod
        def model_validate(cls, data: object) -> _ExplodingSettings:
            raise RuntimeError("gate passed: reached settings")

    monkeypatch.setattr("book_graph_rag.main.Settings", _ExplodingSettings)
    allowed_result = CliRunner().invoke(
        cli,
        [
            "quarantine",
            "approve",
            "--seq",
            "1",
            "--backup",
            str(backup),
            "--approval",
            str(approval),
            "--allow-stale-backup",
        ],
    )
    assert allowed_result.exit_code == 1
    assert "APPROVE abortado" not in allowed_result.output
    assert "Configuration error" in allowed_result.output


def test_cli_approve_prints_ledger_seq_and_audit_reminder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: outcomes per seq, reviewer default, audit reminder."""
    backup = tmp_path / "backup.json"
    backup.write_text("{}", encoding="utf-8")
    approval = tmp_path / "approval.txt"
    approval.write_text("approve", encoding="utf-8")
    captured: dict[str, object] = {}

    class _StubSettings:
        @classmethod
        def model_validate(cls, data: object) -> _StubSettings:
            return cls()

    class _FakeApproveUseCase:
        async def approve_many(
            self,
            *,
            seqs: tuple[int, ...],
            reviewer: str,
            approver_for_apply: str,
        ) -> list[QuarantineDecisionOutcome]:
            captured["seqs"] = seqs
            captured["reviewer"] = reviewer
            return [
                QuarantineDecisionOutcome(
                    seq=seq,
                    decision=QuarantineDecision.APPROVED,
                    ledger_seq=index,
                )
                for index, seq in enumerate(seqs, start=41)
            ]

    async def _fake_build(
        settings: object,
    ) -> tuple[_FakeApproveUseCase, list[object]]:
        return _FakeApproveUseCase(), []

    monkeypatch.setattr("book_graph_rag.main.Settings", _StubSettings)
    monkeypatch.setattr("book_graph_rag.main.build_approve_quarantine_use_case", _fake_build)

    result = CliRunner().invoke(
        cli,
        [
            "quarantine",
            "approve",
            "--seq",
            "3",
            "--seq",
            "5",
            "--backup",
            str(backup),
            "--approval",
            str(approval),
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["seqs"] == (3, 5)
    assert str(captured["reviewer"]).startswith("human:")
    assert "seq 3: approved" in result.output
    assert "ledger seq 41" in result.output
    assert "seq 5: approved" in result.output
    assert "ledger seq 42" in result.output
    assert "scoped" in result.output
    assert "global" in result.output
