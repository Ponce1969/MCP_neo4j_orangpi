"""Namespace guard on ``ApplyMergeUseCase`` (task T7, design §5, decision D-A2).

A group whose canonical and duplicates span namespaces must never reach the
graph port without a ``MergeApproval`` naming exactly that canonical and the
crossing candidates (spec 03 §2.4, policy R6.2: cross-namespace is always
quarantine/review). No database: every port is an in-memory stub in the style
of ``tests/test_use_cases_unit.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest
from pydantic import ValidationError

from book_graph_rag.application.apply_merge_use_case import (
    ApplyMergeUseCase,
    MergeGroup,
)
from book_graph_rag.application.approve_quarantine_use_case import (
    ApproveQuarantineUseCase,
)
from book_graph_rag.domain.merge_approval import MergeApproval
from book_graph_rag.domain.merge_ledger_models import (
    FoldedAlias,
    MergeLedgerEntry,
    chained_hash,
)
from book_graph_rag.domain.models import Entity
from book_graph_rag.domain.quarantine_models import QuarantineDecision, QuarantineRecord
from book_graph_rag.domain.resolution_errors import (
    CrossNamespaceApprovalRequired,
    ResolutionError,
)
from book_graph_rag.domain.resolution_models import (
    ConfidenceBand,
    ResolutionEvidence,
    S0NormalizedForm,
)
from book_graph_rag.ports.graph_db_port import GraphDatabasePort
from book_graph_rag.ports.graph_merge_port import GraphMergePort, InverseMappingSnapshot
from book_graph_rag.ports.merge_ledger_port import MergeLedgerPort
from book_graph_rag.ports.quarantine_writer_port import QuarantineWriterPort

_CANON = "book:ch1:canon"
_SAME = "book:ch1:same-dup"
_CROSS_A = "corp:ch2:cross-a"
_CROSS_B = "other:ch3:cross-b"
_CROSSING = [_CROSS_A, _CROSS_B]


# ── Stubs (in-memory ports, no database) ────────────────────────────────────


class _RecordingGraphMerge(GraphMergePort):
    """Records ``apply_merge`` calls so tests can prove what reached the port."""

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
        raise NotImplementedError("not used by the namespace-guard tests")


class _InMemoryLedger(MergeLedgerPort):
    """In-memory ledger that chains hashes like the JSONL adapter."""

    def __init__(self) -> None:
        self.entries: list[MergeLedgerEntry] = []

    def append(self, entry: MergeLedgerEntry) -> None:
        prev = self.entries[-1].entry_sha256 if self.entries else "0" * 64
        self.entries.append(chained_hash(entry, prev))

    def read_all(self) -> list[MergeLedgerEntry]:
        return list(self.entries)

    def read_by_seq(self, seq: int) -> MergeLedgerEntry | None:
        for entry in self.entries:
            if entry.seq == seq:
                return entry
        return None

    def verify_chain(self) -> None:
        pass


class _StubEntityLoader:
    """Returns a fixed entity set; cast to ``GraphDatabasePort`` below."""

    def __init__(self, entities: list[Entity]) -> None:
        self._entities = entities

    async def load_active_entities(self, *, batch_size: int = 500) -> list[Entity]:
        return list(self._entities)


class _InMemoryQuarantine(QuarantineWriterPort):
    """In-memory quarantine store, mirroring ``FakeQuarantineWriter``."""

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


# ── Helpers ─────────────────────────────────────────────────────────────────


def _entity(entity_id: str, name: str, entity_type: str = "framework") -> Entity:
    return Entity(
        id=entity_id,
        name=name,
        type=entity_type,  # type: ignore[arg-type]
    )


def _loader(entities: list[Entity]) -> GraphDatabasePort:
    return cast(GraphDatabasePort, _StubEntityLoader(entities))


def _default_entities() -> list[Entity]:
    return [
        _entity(_CANON, "Canon"),
        _entity(_SAME, "Same"),
        _entity(_CROSS_A, "CrossA"),
        _entity(_CROSS_B, "CrossB"),
    ]


def _use_case() -> tuple[ApplyMergeUseCase, _RecordingGraphMerge, _InMemoryLedger]:
    graph_merge = _RecordingGraphMerge()
    ledger = _InMemoryLedger()
    use_case = ApplyMergeUseCase(
        graph_merge=graph_merge,
        ledger=ledger,
        entity_loader=_loader(_default_entities()),
    )
    return use_case, graph_merge, ledger


def _crossing_group(band: ConfidenceBand = ConfidenceBand.EXACT) -> MergeGroup:
    """Canonical in ``book:ch1``; one duplicate inside, two outside."""
    return MergeGroup(
        canonical_id=_CANON,
        duplicate_ids=[_SAME, _CROSS_A, _CROSS_B],
        band=band,
        evidence=[],
    )


def _same_ns_group() -> MergeGroup:
    return MergeGroup(
        canonical_id=_CANON,
        duplicate_ids=[_SAME],
        band=ConfidenceBand.EXACT,
        evidence=[],
    )


def _approval(
    *,
    canonical_id: str = _CANON,
    candidate_ids: tuple[str, ...] = tuple(_CROSSING),
) -> MergeApproval:
    return MergeApproval(
        quarantine_seq=7,
        approved_by="bob",
        approved_at=datetime(2026, 10, 3, tzinfo=UTC),
        canonical_id=canonical_id,
        candidate_ids=candidate_ids,
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


def _record(
    *,
    canonical_id: str | None,
    candidate_id: str = _CROSS_A,
    seq: int = 1,
) -> QuarantineRecord:
    evidence = ResolutionEvidence(
        anchor_id=_CANON,
        candidate_id=candidate_id,
        anchor_type="framework",
        candidate_type="framework",
        anchor_namespace="book:ch1",
        candidate_namespace=":".join(candidate_id.split(":")[:2]),
        anchor_normalized=_norm("Canon"),
        candidate_normalized=_norm("Cross"),
        s0_matched_field="none",
        band=ConfidenceBand.MEDIUM,
        cross_namespace=True,
        cross_type=False,
    )
    return QuarantineRecord(
        seq=seq,
        anchor_id=_CANON,
        candidate_id=candidate_id,
        canonical_id=canonical_id,
        band=ConfidenceBand.MEDIUM,
        evidence=evidence,
        created_at=datetime(2026, 10, 3, tzinfo=UTC),
    )


# ── MergeApproval model ─────────────────────────────────────────────────────


def test_merge_approval_rejects_empty_candidate_ids() -> None:
    """The proof must cover at least one candidate."""
    with pytest.raises(ValidationError, match="candidate_ids"):
        _approval(candidate_ids=())


def test_merge_approval_rejects_blank_approved_by() -> None:
    """The proof must name the human who approved it."""
    with pytest.raises(ValidationError, match="approved_by"):
        MergeApproval(
            quarantine_seq=7,
            approved_by="   ",
            approved_at=datetime(2026, 10, 3, tzinfo=UTC),
            canonical_id=_CANON,
            candidate_ids=tuple(_CROSSING),
        )


def test_merge_approval_is_frozen_and_strict() -> None:
    """Immutable record; loose types (list) are not coerced."""
    approval = _approval()
    with pytest.raises(ValidationError):  # frozen
        approval.canonical_id = "other"
    with pytest.raises(ValidationError, match="candidate_ids"):  # strict tuple
        _approval(candidate_ids=list(_CROSSING))  # type: ignore[arg-type]


def test_cross_namespace_error_is_a_resolution_error() -> None:
    assert issubclass(CrossNamespaceApprovalRequired, ResolutionError)


# ── The guard: ApplyMergeUseCase.apply ──────────────────────────────────────


async def test_cross_namespace_without_approval_is_refused_and_lists_candidates() -> None:
    """No approval → refuse before touching any port, naming canon + crossers."""
    use_case, graph_merge, ledger = _use_case()

    with pytest.raises(CrossNamespaceApprovalRequired) as excinfo:
        await use_case.apply(_crossing_group(), approver="alice")

    message = str(excinfo.value)
    assert _CANON in message
    assert _CROSS_A in message
    assert _CROSS_B in message
    assert _SAME not in message  # the in-namespace duplicate is not offending
    assert "approve the quarantine record first" in message

    # Guard fires before any port is touched.
    assert graph_merge.applied == []
    assert ledger.entries == []


async def test_cross_namespace_with_valid_approval_reaches_the_port() -> None:
    """An approval naming exactly this canonical + crossing candidates merges."""
    use_case, graph_merge, ledger = _use_case()

    entry = await use_case.apply(_crossing_group(), approver="alice", approval=_approval())

    assert graph_merge.applied == [(_CANON, [_SAME, _CROSS_A, _CROSS_B])]
    assert len(ledger.entries) == 1
    assert entry.canonical_id == _CANON
    assert entry.candidate_ids == [_SAME, _CROSS_A, _CROSS_B]


async def test_same_namespace_without_approval_proceeds() -> None:
    """Same-namespace groups keep working with no approval at all."""
    use_case, graph_merge, ledger = _use_case()

    await use_case.apply(_same_ns_group(), approver="alice")

    assert graph_merge.applied == [(_CANON, [_SAME])]
    assert len(ledger.entries) == 1


async def test_approval_with_wrong_canonical_is_refused() -> None:
    """A proof for a different canonical does not authorize this group."""
    use_case, graph_merge, _ = _use_case()

    with pytest.raises(CrossNamespaceApprovalRequired) as excinfo:
        await use_case.apply(
            _crossing_group(),
            approver="alice",
            approval=_approval(canonical_id="book:ch9:someone-else"),
        )

    message = str(excinfo.value)
    assert _CANON in message  # the group's canonical is still named
    assert "book:ch9:someone-else" in message  # the mismatch is explained
    assert graph_merge.applied == []


async def test_approval_missing_a_crossing_candidate_is_refused() -> None:
    """Partial coverage of the crossing candidates fails closed."""
    use_case, graph_merge, _ = _use_case()

    with pytest.raises(CrossNamespaceApprovalRequired) as excinfo:
        await use_case.apply(
            _crossing_group(),
            approver="alice",
            approval=_approval(candidate_ids=(_CROSS_A,)),
        )

    message = str(excinfo.value)
    assert _CROSS_B in message  # the missing candidate is named
    assert graph_merge.applied == []


async def test_approval_with_extra_candidate_is_refused() -> None:
    """A proof carrying extras (here: the in-namespace duplicate) fails closed."""
    use_case, graph_merge, _ = _use_case()

    with pytest.raises(CrossNamespaceApprovalRequired) as excinfo:
        await use_case.apply(
            _crossing_group(),
            approver="alice",
            approval=_approval(candidate_ids=tuple(_CROSSING + [_SAME])),
        )

    message = str(excinfo.value)
    assert _SAME in message  # the extra candidate is named
    assert graph_merge.applied == []


async def test_same_namespace_ignores_unnecessary_approval() -> None:
    """Documented choice: an approval is consulted ONLY for crossing groups.

    A same-namespace merge never needs one, so a supplied (even mismatched)
    approval is ignored rather than allowed to block a legitimate merge.
    """
    use_case, graph_merge, ledger = _use_case()

    await use_case.apply(
        _same_ns_group(),
        approver="alice",
        approval=_approval(canonical_id="totally:unrelated:group"),
    )

    assert graph_merge.applied == [(_CANON, [_SAME])]
    assert len(ledger.entries) == 1


async def test_historical_bypass_with_exact_band_still_fires() -> None:
    """band=EXACT + cross_namespace evidence (the 302-merge bypass) is refused."""
    use_case, graph_merge, _ = _use_case()
    group = _crossing_group(band=ConfidenceBand.EXACT)

    with pytest.raises(CrossNamespaceApprovalRequired):
        await use_case.apply(group, approver="human:gonzalo")

    assert graph_merge.applied == []


# ── ApproveQuarantineUseCase: the legitimate flow keeps working ─────────────


async def test_approve_quarantine_cross_namespace_passes_its_approval() -> None:
    """Approving a cross-namespace record builds and passes its MergeApproval."""
    quarantine = _InMemoryQuarantine()
    quarantine.append(_record(canonical_id=_CANON))

    graph_merge = _RecordingGraphMerge()
    ledger = _InMemoryLedger()
    apply = ApplyMergeUseCase(
        graph_merge=graph_merge,
        ledger=ledger,
        entity_loader=_loader(_default_entities()),
    )
    use_case = ApproveQuarantineUseCase(quarantine=quarantine, apply=apply)

    entry = await use_case.approve(seq=1, reviewer="bob", approver_for_apply="alice")

    assert quarantine.records[0].decision == QuarantineDecision.APPROVED
    assert quarantine.records[0].reviewed_by == "bob"
    assert graph_merge.applied == [(_CANON, [_CROSS_A])]
    assert entry.canonical_id == _CANON


async def test_approve_quarantine_without_canonical_refuses_instead_of_applying() -> None:
    """A record that never chose a canonical cannot authorize a cross-ns merge."""
    quarantine = _InMemoryQuarantine()
    quarantine.append(_record(canonical_id=None))

    graph_merge = _RecordingGraphMerge()
    apply = ApplyMergeUseCase(
        graph_merge=graph_merge,
        ledger=_InMemoryLedger(),
        entity_loader=_loader(_default_entities()),
    )
    use_case = ApproveQuarantineUseCase(quarantine=quarantine, apply=apply)

    with pytest.raises(ResolutionError, match="canonical"):
        await use_case.approve(seq=1, reviewer="bob", approver_for_apply="alice")

    # Nothing was mutated: decision untouched, no merge applied.
    assert quarantine.records[0].decision == QuarantineDecision.PENDING
    assert graph_merge.applied == []
