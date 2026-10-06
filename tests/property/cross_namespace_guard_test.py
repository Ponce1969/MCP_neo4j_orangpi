"""Property: no band and no forged evidence can cross the namespace boundary
without a matching ``MergeApproval`` (task T7, design §5, decision D-A2).

The guard lives inside ``ApplyMergeUseCase.apply`` (spec 03 §2.4, policy R6.2:
cross-namespace merges are always quarantine/review), so for arbitrary ids,
namespaces and bands the only way a crossing group reaches ``GraphMergePort``
is with a ``MergeApproval`` naming exactly that canonical and the crossing
candidates. This includes the historical bypass: a caller forging
``band=EXACT`` with ``cross_namespace=True`` evidence.

These tests are pure: in-memory stub ports, no Neo4j and no testcontainers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from book_graph_rag.application.apply_merge_use_case import (
    ApplyMergeUseCase,
    MergeGroup,
)
from book_graph_rag.domain.merge_approval import MergeApproval
from book_graph_rag.domain.merge_ledger_models import (
    FoldedAlias,
    MergeLedgerEntry,
    chained_hash,
)
from book_graph_rag.domain.models import Entity
from book_graph_rag.domain.resolution_errors import (
    CrossNamespaceApprovalRequired,
    ResolutionError,
)
from book_graph_rag.domain.resolution_models import (
    ConfidenceBand,
    ResolutionEvidence,
    S0NormalizedForm,
)
from book_graph_rag.domain.s0_normalization import namespace_from_id
from book_graph_rag.ports.graph_db_port import GraphDatabasePort
from book_graph_rag.ports.graph_merge_port import GraphMergePort, InverseMappingSnapshot
from book_graph_rag.ports.merge_ledger_port import MergeLedgerPort

_APPROVAL_KINDS = [
    "none",
    "matching",
    "wrong_canonical",
    "missing_candidate",
    "extra_candidate",
]

_BANDS = [
    ConfidenceBand.EXACT,
    ConfidenceBand.HIGH,
    ConfidenceBand.MEDIUM,
    ConfidenceBand.LOW,
]

_NAMESPACE_PART = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N")),
    min_size=1,
    max_size=6,
)
_NAMESPACE = st.builds(lambda a, b: f"{a}:{b}", _NAMESPACE_PART, _NAMESPACE_PART)
_SLUG = st.text(alphabet="abcdef0123456789-", min_size=1, max_size=8)


# ── Pure stubs ──────────────────────────────────────────────────────────────


class _RecordingGraphMerge(GraphMergePort):
    """Records only what actually reached the graph port."""

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
        raise NotImplementedError("not used by the namespace-guard property tests")


class _InMemoryLedger(MergeLedgerPort):
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
    def __init__(self, entities: list[Entity]) -> None:
        self._entities = entities

    async def load_active_entities(self, *, batch_size: int = 500) -> list[Entity]:
        return list(self._entities)


def _entity(entity_id: str) -> Entity:
    return Entity(id=entity_id, name=entity_id, type="framework")


def _loader(ids: list[str]) -> GraphDatabasePort:
    return cast(GraphDatabasePort, _StubEntityLoader([_entity(i) for i in ids]))


def _forges_evidence(
    canonical_id: str, duplicate_ids: list[str], band: ConfidenceBand
) -> list[ResolutionEvidence]:
    """Evidence that claims certainty regardless of the truth (the bypass)."""
    return [
        ResolutionEvidence(
            anchor_id=canonical_id,
            candidate_id=dup,
            anchor_type="framework",
            candidate_type="framework",
            anchor_namespace=namespace_from_id(canonical_id),
            candidate_namespace=namespace_from_id(dup),
            anchor_normalized=_s0(canonical_id),
            candidate_normalized=_s0(dup),
            s0_matched_field="id",
            composite_score=1.0,
            band=band,
            cross_namespace=True,
            cross_type=False,
        )
        for dup in duplicate_ids
    ]


def _s0(entity_id: str) -> S0NormalizedForm:
    """Minimal normalized form for forged evidence (pure, deterministic)."""
    raw = entity_id.rsplit(":", 1)[-1]
    folded = raw.casefold()
    return S0NormalizedForm(
        original=raw,
        nfkc=raw,
        casefold=folded,
        compact=folded,
        tokens=(folded,),
    )


def _approval(*, canonical_id: str, candidate_ids: tuple[str, ...]) -> MergeApproval:
    return MergeApproval(
        quarantine_seq=1,
        approved_by="prop-test",
        approved_at=datetime(2026, 10, 3, tzinfo=UTC),
        canonical_id=canonical_id,
        candidate_ids=candidate_ids,
    )


def _build_approval(
    kind: str,
    canonical_id: str,
    duplicate_ids: list[str],
    crossing: list[str],
) -> MergeApproval | None:
    """Build a valid ``MergeApproval`` of the requested (possibly wrong) shape."""
    if kind == "none":
        return None
    phantom = "zz:zz:phantom-not-in-group"
    if kind == "matching":
        candidates = tuple(crossing) or tuple(duplicate_ids)
        return _approval(canonical_id=canonical_id, candidate_ids=candidates)
    if kind == "wrong_canonical":
        candidates = tuple(crossing) or tuple(duplicate_ids)
        return _approval(canonical_id="wrong:ns:some-group", candidate_ids=candidates)
    if kind == "missing_candidate":
        # Always leave out at least one crossing candidate (or the first dup).
        if crossing:
            candidates = tuple(d for d in duplicate_ids if d != crossing[0])
        else:
            candidates = tuple(duplicate_ids[1:])
        if not candidates:
            candidates = (phantom,)
        return _approval(canonical_id=canonical_id, candidate_ids=candidates)
    # extra_candidate: carry an id the guard never asked for.
    candidates = tuple(crossing) + (phantom,)
    return _approval(canonical_id=canonical_id, candidate_ids=candidates)


@st.composite
def _group_scenario(
    draw: st.DrawFn,
) -> tuple[str, list[str], ConfidenceBand, str]:
    """Canonical id, duplicate ids, band and approval kind for one apply call."""
    canonical_ns = draw(_NAMESPACE)
    other_ns = draw(_NAMESPACE)
    slug = draw(_SLUG)
    canonical_id = f"{canonical_ns}:{slug}"

    duplicate_ids: list[str] = []
    for i in range(draw(st.integers(min_value=1, max_value=4))):
        ns = canonical_ns if draw(st.booleans()) else other_ns
        duplicate_ids.append(f"{ns}:{draw(_SLUG)}-{i}")

    band = draw(st.sampled_from(_BANDS))
    kind = draw(st.sampled_from(_APPROVAL_KINDS))
    return canonical_id, duplicate_ids, band, kind


@given(scenario=_group_scenario())
@settings(max_examples=200, deadline=None)
@pytest.mark.asyncio
async def test_no_band_and_no_forged_evidence_can_cross_without_matching_approval(
    scenario: tuple[str, list[str], ConfidenceBand, str],
) -> None:
    """The guard fires for every band unless the approval matches exactly.

    A crossing group with any other approval shape (none, wrong canonical,
    missing or extra candidate) must raise ``CrossNamespaceApprovalRequired``
    regardless of band and forged evidence, before any port call. Otherwise
    the band rule decides: non-auto bands (LOW) are refused as non-auto.
    """
    canonical_id, duplicate_ids, band, kind = scenario
    crossing = [d for d in duplicate_ids if namespace_from_id(d) != namespace_from_id(canonical_id)]
    approval = _build_approval(kind, canonical_id, duplicate_ids, crossing)
    auto_band = band in {
        ConfidenceBand.EXACT,
        ConfidenceBand.HIGH,
        ConfidenceBand.MEDIUM,
    }
    guard_must_fire = bool(crossing) and kind != "matching"

    group = MergeGroup(
        canonical_id=canonical_id,
        duplicate_ids=duplicate_ids,
        band=band,
        evidence=_forges_evidence(canonical_id, duplicate_ids, band),
    )
    graph_merge = _RecordingGraphMerge()
    ledger = _InMemoryLedger()
    use_case = ApplyMergeUseCase(
        graph_merge=graph_merge,
        ledger=ledger,
        entity_loader=_loader([canonical_id, *duplicate_ids]),
    )

    raised_guard = False
    raised_other = False
    try:
        await use_case.apply(group, approver="prop", approval=approval)
    except CrossNamespaceApprovalRequired:
        raised_guard = True
    except ResolutionError:
        raised_other = True

    reached = len(graph_merge.applied) == 1
    context = f"band={band} kind={kind} crossing={crossing} approval={approval}"
    if guard_must_fire:
        assert raised_guard, f"guard must fire: {context}"
        assert not reached, f"nothing may reach the port: {context}"
        assert ledger.entries == []
    elif auto_band:
        # Non-crossing, or crossing with the matching approval: reaches the port.
        assert not raised_guard, f"guard must not fire: {context}"
        assert not raised_other, f"apply must succeed: {context}"
        assert reached, f"merge must reach the port: {context}"
        assert len(ledger.entries) == 1
    else:
        # LOW band: refused by the band rule (the guard already passed).
        assert raised_other, f"band rule must refuse: {context}"
        assert not raised_guard
        assert not reached
        assert ledger.entries == []


@given(
    ns_part=_NAMESPACE_PART,
    slug=_SLUG,
    dup_slug=_SLUG,
)
@settings(max_examples=100, deadline=None)
@pytest.mark.asyncio
async def test_historical_exact_band_bypass_still_fires(
    ns_part: str, slug: str, dup_slug: str
) -> None:
    """band=EXACT + forged cross_namespace evidence (the 302-merge bypass)."""
    canonical_ns = f"{ns_part}:{ns_part}"
    cross_ns = f"{ns_part}:{ns_part}x"  # always a different namespace
    canonical_id = f"{canonical_ns}:{slug}"
    dup_id = f"{cross_ns}:{dup_slug}"

    group = MergeGroup(
        canonical_id=canonical_id,
        duplicate_ids=[dup_id],
        band=ConfidenceBand.EXACT,
        evidence=_forges_evidence(canonical_id, [dup_id], ConfidenceBand.EXACT),
    )

    graph_merge = _RecordingGraphMerge()
    ledger = _InMemoryLedger()
    use_case = ApplyMergeUseCase(
        graph_merge=graph_merge,
        ledger=ledger,
        entity_loader=_loader([canonical_id, dup_id]),
    )

    # Without an approval the guard fires even for EXACT.
    with pytest.raises(CrossNamespaceApprovalRequired):
        await use_case.apply(group, approver="human:gonzalo")
    assert graph_merge.applied == []
    assert ledger.entries == []

    # The matching approval is the only credential that opens the boundary.
    await use_case.apply(
        group,
        approver="human:gonzalo",
        approval=_approval(canonical_id=canonical_id, candidate_ids=(dup_id,)),
    )
    assert graph_merge.applied == [(canonical_id, [dup_id])]
    assert len(ledger.entries) == 1
