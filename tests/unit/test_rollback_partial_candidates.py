"""T8f unit contract: partial rollback of one candidate inside one ledger entry.

Pure domain + application tests — fake ports only, no Docker:

* ``build_entry_plan(..., selected_candidates=...)`` validates the selection
  (non-empty subset of ``entry.candidate_ids``, order normalised to the
  entry's own) and, when partial, filters the inverse map, the folded
  aliases, the predicted census and the affected entities down to the
  selection;
* selecting EVERY candidate reproduces the full plan's inferred entry while
  the plan fingerprint still differs (``selected_candidates`` is part of the
  authenticated payload);
* ``PlanRollbackUseCase.plan`` requires exactly one seq for a selection and
  probes only the selected candidates' RELATED inverse entries;
* an OVERLAPPING request (any candidate already covered by a prior
  compensating entry) is refused at PLAN time with ``MergeNotReversible``,
  before any graph probe — the plan must always equal the mutation (defect 1);
* a partial selection whose folded alias VALUE is shared with a non-selected
  candidate is refused with ``ValueError``: the adapter removes aliases by
  value, so a partial restore would delete the other candidate's alias
  (defect 2, data-model limitation);
* ``compare_census`` expects the ``merged_into``/mentions delta of the
  SELECTION, not of the whole entry;
* ``RollbackMergeUseCase`` idempotence is candidate-aware: a compensating
  entry covers only the candidates it recorded, so partial A then partial B
  proceeds while repeating partial A is a no-op that appends nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from book_graph_rag.application.plan_rollback_use_case import (
    PlanRollbackUseCase,
    planned_entry_ledger,
)
from book_graph_rag.application.rollback_merge_use_case import RollbackMergeUseCase
from book_graph_rag.domain.merge_ledger_models import (
    EdgeInverseMap,
    FoldedAlias,
    MergeBand,
    MergeLedgerEntry,
    chained_hash,
)
from book_graph_rag.domain.resolution_errors import (
    MergeNotReversible,
    RollbackTargetInvalid,
)
from book_graph_rag.domain.rollback_plan_models import (
    ActualEntryOutcome,
    EdgeCensus,
    PredictedCensus,
    RelatedEdgeObservation,
    RelatedEdgeProbe,
    RollbackMeasurement,
    RollbackPlan,
    build_entry_plan,
    build_rollback_plan,
    compare_census,
)
from book_graph_rag.ports.graph_merge_port import GraphMergePort, InverseMappingSnapshot
from book_graph_rag.ports.merge_ledger_port import MergeLedgerPort
from book_graph_rag.ports.rollback_plan_port import RollbackPlanPort

_CANON = "graphrag-agentic:tool-hub"
_DUP_A = "agentic-patterns:tool-hub"
_DUP_B = "essential-graphrag:tool-hub"
_OTHER_A = "graphrag-agentic:tool-registry"
_OTHER_B = "graphrag-agentic:tool-router"
_CHUNK_A = "graphrag-agentic:chunk-1"
_CHUNK_B = "graphrag-agentic:chunk-2"
_SEQ = 480


def _entry(*, shared_alias: bool = False) -> MergeLedgerEntry:
    """A 2-candidate entry: one MENTIONS + one RELATED per candidate.

    ``shared_alias=True`` folds the SAME alias value from both candidates —
    the data-model shape that makes a partial alias restore unsafe (the
    adapter removes aliases by value).
    """
    return MergeLedgerEntry(
        seq=_SEQ,
        candidate_ids=[_DUP_A, _DUP_B],
        canonical_id=_CANON,
        band=MergeBand.EXACT,
        evidence=[],
        aliases_folded=[
            FoldedAlias(from_entity_id=_DUP_A, alias_value="Tool Hub"),
            FoldedAlias(
                from_entity_id=_DUP_B,
                alias_value="ToolHub" if not shared_alias else "Tool Hub",
            ),
        ],
        edge_inverse_map=[
            EdgeInverseMap(
                edge_kind="MENTIONS",
                duplicate_entity_id=_DUP_A,
                original_other_endpoint_id=_CHUNK_A,
                edge_properties={"source_page": 3},
            ),
            EdgeInverseMap(
                edge_kind="RELATED",
                duplicate_entity_id=_DUP_A,
                original_other_endpoint_id=_OTHER_A,
                edge_properties={"type": "requires", "source_page": 4},
            ),
            EdgeInverseMap(
                edge_kind="MENTIONS",
                duplicate_entity_id=_DUP_B,
                original_other_endpoint_id=_CHUNK_B,
                edge_properties={"source_page": 5},
            ),
            EdgeInverseMap(
                edge_kind="RELATED",
                duplicate_entity_id=_DUP_B,
                original_other_endpoint_id=_OTHER_B,
                edge_properties={"type": "requires", "source_page": 6},
            ),
        ],
        approver="auto:bypass",
        applied_at=datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC),
    )


def _observations() -> list[RelatedEdgeObservation]:
    """Canonical shows ``canon -> otherA`` (out) and ``otherB -> canon`` (in)."""
    return [
        RelatedEdgeObservation(
            seq=_SEQ,
            map_index=1,
            probe=RelatedEdgeProbe(a_to_b=True, b_to_a=False),
        ),
        RelatedEdgeObservation(
            seq=_SEQ,
            map_index=3,
            probe=RelatedEdgeProbe(a_to_b=False, b_to_a=True),
        ),
    ]


def _plan(
    selected: list[str] | None = None,
    *,
    entry: MergeLedgerEntry | None = None,
) -> RollbackPlan:
    return build_rollback_plan(
        [build_entry_plan(entry or _entry(), _observations(), selected_candidates=selected)]
    )


# ── build_entry_plan: validation + filtering ─────────────────────────────────


def test_partial_selection_filters_map_census_aliases_and_affected_entities() -> None:
    """Selecting A narrows every plan surface to A: map, aliases, census, affected."""
    entry = _entry()

    entry_plan = build_entry_plan(entry, _observations(), selected_candidates=[_DUP_A])

    assert entry_plan.selected_candidates == [_DUP_A]
    # The stored entry is never touched: full candidates, full aliases, full map.
    assert entry_plan.entry.candidate_ids == [_DUP_A, _DUP_B]
    assert len(entry_plan.entry.edge_inverse_map) == 4
    # The inferred copy carries the selection everywhere it matters.
    assert entry_plan.inferred_entry.candidate_ids == [_DUP_A]
    assert [m.duplicate_entity_id for m in entry_plan.inferred_entry.edge_inverse_map] == [
        _DUP_A,
        _DUP_A,
    ]
    assert entry_plan.inferred_entry.aliases_folded == [
        FoldedAlias(from_entity_id=_DUP_A, alias_value="Tool Hub")
    ]
    # Directions are still inferred over the filtered RELATED entries only.
    assert [inf.duplicate_entity_id for inf in entry_plan.inferences] == [_DUP_A]
    assert entry_plan.inferred_entry.edge_inverse_map[1].direction == "out"
    assert entry_plan.predicted == PredictedCensus(
        mentions_restored=1,
        related_restored=1,
        directions_inferred=1,
        fallback_both=0,
        fallback_unknown=0,
        predicted_mirrors=0,
    )
    assert entry_plan.affected_entities == sorted({_CANON, _DUP_A, _OTHER_A})


def test_selection_order_follows_the_entry_order() -> None:
    """A selection given as [B, A] is normalised to the entry's own order."""
    entry_plan = build_entry_plan(_entry(), _observations(), selected_candidates=[_DUP_B, _DUP_A])
    assert entry_plan.selected_candidates == [_DUP_A, _DUP_B]
    assert entry_plan.inferred_entry.candidate_ids == [_DUP_A, _DUP_B]


def test_selection_must_be_a_non_empty_subset_of_the_entry_candidates() -> None:
    """Unknown ids and an empty selection are refused with ValueError."""
    entry = _entry()

    with pytest.raises(ValueError, match="tool-oracle"):
        build_entry_plan(
            entry,
            _observations(),
            selected_candidates=["graphrag-agentic:tool-oracle"],
        )

    with pytest.raises(ValueError, match="non-empty"):
        build_entry_plan(entry, _observations(), selected_candidates=[])

    with pytest.raises(ValueError, match="tool-oracle"):
        build_entry_plan(
            entry,
            _observations(),
            selected_candidates=[_DUP_A, "graphrag-agentic:tool-oracle"],
        )


def test_selecting_every_candidate_yields_the_same_inferred_entry_as_the_full_plan() -> None:
    """The full plan and the all-selected plan infer the exact same entry."""
    full = build_entry_plan(_entry(), _observations())
    all_selected = build_entry_plan(_entry(), _observations(), selected_candidates=[_DUP_A, _DUP_B])

    assert all_selected.inferred_entry == full.inferred_entry
    assert all_selected.predicted == full.predicted
    assert all_selected.affected_entities == full.affected_entities


def test_partial_plan_fingerprint_differs_from_the_full_plan() -> None:
    """The selection is part of the authenticated payload: hashes must differ."""
    full = _plan()
    partial = _plan([_DUP_A])

    assert partial.fingerprint != full.fingerprint
    # And a partial plan is still stable across rebuilds (what the guard binds).
    assert _plan([_DUP_A]).fingerprint == partial.fingerprint


def test_partial_selection_refuses_an_alias_value_shared_with_a_non_selected_candidate() -> None:
    """Fail closed on the by-value alias removal (data-model limitation).

    ``_ROLLBACK_REMOVE_ALIASES`` deletes folded aliases from the canonical by
    VALUE, so reviving a selected candidate that folded the same value as a
    NON-selected (still merged) candidate would silently delete the other
    candidate's alias — a partial plan must refuse instead of losing data.
    """
    entry = _entry(shared_alias=True)

    with pytest.raises(ValueError, match="shared alias") as excinfo:
        build_entry_plan(entry, _observations(), selected_candidates=[_DUP_A])
    message = str(excinfo.value)
    assert "Tool Hub" in message, "the shared value must be named"
    assert _DUP_A in message, "the selected side must be named"
    assert _DUP_B in message, "the non-selected side must be named"
    assert "by value" in message, "the reason (adapter removes by value) must be stated"

    # The refusal is symmetric: selecting B alone loses A's alias just the same.
    with pytest.raises(ValueError, match="Tool Hub"):
        build_entry_plan(entry, _observations(), selected_candidates=[_DUP_B])

    # The whole entry over the same data still plans: nothing can be lost.
    full = build_entry_plan(entry, _observations())
    assert full.inferred_entry.aliases_folded == entry.aliases_folded
    # Selecting EVERY candidate is equally lossless (no non-selected side).
    all_selected = build_entry_plan(entry, _observations(), selected_candidates=[_DUP_A, _DUP_B])
    assert all_selected.inferred_entry.aliases_folded == entry.aliases_folded


# ── PlanRollbackUseCase.plan: single-seq rule + scoped probes ────────────────


class _FakeLedger(MergeLedgerPort):
    """In-memory ledger with chained hashes (same contract as the JSONL adapter)."""

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
        return None


class _FakePlanPort(RollbackPlanPort):
    """Records every RELATED probe so scoping assertions can read them."""

    def __init__(self) -> None:
        self.probes: list[tuple[str, str, str]] = []

    async def probe_related_edges(
        self,
        *,
        a_id: str,
        b_id: str,
        edge_type: str,
    ) -> RelatedEdgeProbe:
        self.probes.append((a_id, b_id, edge_type))
        return RelatedEdgeProbe(a_to_b=True, b_to_a=False)

    async def read_edge_census(self, entity_ids: list[str]) -> EdgeCensus:
        return EdgeCensus(
            mentions_by_entity=dict.fromkeys(entity_ids, 0),
            related_by_entity=dict.fromkeys(entity_ids, 0),
            total_mentions=0,
            total_related=0,
            merged_into_count=0,
        )


def _plan_use_case(
    ledger: MergeLedgerPort | None = None,
    plan_port: _FakePlanPort | None = None,
) -> tuple[PlanRollbackUseCase, _FakePlanPort]:
    fake_ledger = ledger or _FakeLedger()
    if not fake_ledger.read_all():
        fake_ledger.append(_entry())
    port = plan_port or _FakePlanPort()
    return PlanRollbackUseCase(ledger=fake_ledger, plan_port=port), port


async def test_plan_refuses_candidates_with_more_than_one_seq() -> None:
    """A selection addresses ONE entry: two seqs raise RollbackTargetInvalid."""
    use_case, port = _plan_use_case()

    with pytest.raises(RollbackTargetInvalid, match="exactly one seq"):
        await use_case.plan([_SEQ, _SEQ + 1], candidates=[_DUP_A])

    assert port.probes == [], "the refusal must happen before any graph probe"


async def test_plan_probes_only_the_selected_candidates_related_entries() -> None:
    """The dry-run must not probe the RELATED entries of non-selected candidates."""
    use_case, port = _plan_use_case()

    plan = await use_case.plan([_SEQ], candidates=[_DUP_A])

    assert port.probes == [(_CANON, _OTHER_A, "requires")]
    entry_plan = plan.plans[0]
    assert entry_plan.selected_candidates == [_DUP_A]
    assert entry_plan.inferred_entry.candidate_ids == [_DUP_A]
    assert (
        plan.fingerprint
        != build_rollback_plan([build_entry_plan(_entry(), _observations())]).fingerprint
    )


async def test_plan_without_candidates_keeps_the_whole_entry_behaviour() -> None:
    """``candidates=None`` is the current behaviour: every entry is probed."""
    use_case, port = _plan_use_case()

    plan = await use_case.plan([_SEQ])

    assert port.probes == [
        (_CANON, _OTHER_A, "requires"),
        (_CANON, _OTHER_B, "requires"),
    ]
    assert plan.plans[0].selected_candidates is None
    assert plan.plans[0].inferred_entry.candidate_ids == [_DUP_A, _DUP_B]


# ── T8f defect 1: overlapping requests must fail closed at PLAN time ─────────


def _compensating_entry(candidate_ids: list[str]) -> MergeLedgerEntry:
    """A prior compensating entry for ``_SEQ`` claiming only ``candidate_ids``."""
    return MergeLedgerEntry(
        seq=_SEQ + 1,
        candidate_ids=list(candidate_ids),
        canonical_id=_CANON,
        band=MergeBand.EXACT,
        evidence=[],
        aliases_folded=[],
        edge_inverse_map=[],
        approver="auto:rollback",
        applied_at=datetime(2026, 10, 4, 12, 0, 0, tzinfo=UTC),
        rollback_of=_SEQ,
    )


def _ledger_with_compensation(compensated: list[str]) -> _FakeLedger:
    ledger = _FakeLedger()
    ledger.append(_entry())
    ledger.append(_compensating_entry(compensated))
    return ledger


async def test_plan_refuses_an_overlapping_request_before_probing() -> None:
    """Defect 1: any overlap with prior compensations fails closed pre-probe.

    After a partial rollback of A, BOTH the full request ``[A, B]`` and a
    repeat of ``[A]`` would re-reverse A's inverse entries and append a second
    compensating entry — the plan must refuse before any graph probe, naming
    the overlap, the compensated set and the explicit ``--candidate`` path for
    the remaining candidates.
    """
    use_case, port = _plan_use_case(_ledger_with_compensation([_DUP_A]))

    with pytest.raises(MergeNotReversible) as full_exc:
        await use_case.plan([_SEQ])
    message = str(full_exc.value)
    assert _DUP_A in message, "the overlapping candidate must be named"
    assert _DUP_B in message, "the remaining uncompensated candidate must be named"
    assert "--candidate" in message, "the operator must be told how to proceed"
    assert port.probes == [], "the refusal must happen before any graph probe"

    with pytest.raises(MergeNotReversible):
        await use_case.plan([_SEQ], candidates=[_DUP_A])
    assert port.probes == [], "a repeat of the compensated candidate never probes"


async def test_plan_proceeds_for_the_complementary_candidate_after_a_compensation() -> None:
    """The remaining candidate is still plannable: B alone probes only B."""
    use_case, port = _plan_use_case(_ledger_with_compensation([_DUP_A]))

    plan = await use_case.plan([_SEQ], candidates=[_DUP_B])

    assert port.probes == [(_CANON, _OTHER_B, "requires")]
    assert plan.plans[0].selected_candidates == [_DUP_B]
    assert plan.plans[0].inferred_entry.candidate_ids == [_DUP_B]


# ── compare_census: the selection drives the delta ───────────────────────────


def test_compare_census_expects_the_selections_delta_not_the_whole_entry() -> None:
    """A partial rollback clears ONE merged_into marker and restores one mention."""
    plan = _plan([_DUP_A])
    assert plan.predicted.mentions_restored == 1

    before = EdgeCensus(
        mentions_by_entity={_DUP_A: 0, _CANON: 2},
        related_by_entity={_DUP_A: 0, _CANON: 2},
        total_mentions=2,
        total_related=2,
        merged_into_count=2,  # A and B still merged (db-wide count)
    )
    after = EdgeCensus(
        mentions_by_entity={_DUP_A: 1, _CANON: 1},
        related_by_entity={_DUP_A: 1, _CANON: 1},
        total_mentions=2,
        total_related=2,
        merged_into_count=1,  # only A was cleared: B stays merged
    )
    measurement = RollbackMeasurement(
        outcomes=[ActualEntryOutcome(seq=_SEQ, related_restored=1, mirrors_created=0)],
        census=after,
    )

    comparison = compare_census(plan, before, measurement)

    assert comparison.drift == []
    assert comparison.passed is True


# ── RollbackMergeUseCase idempotence: candidate-aware ────────────────────────


class _FakeGraphMerge(GraphMergePort):
    """Records reversals and tracks which candidates are still merged."""

    def __init__(self) -> None:
        self.merged: set[str] = set()
        self.rollbacks: list[MergeLedgerEntry] = []

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
        raise NotImplementedError("not used by the partial-rollback tests")

    async def rollback_merge(self, entry: MergeLedgerEntry) -> None:
        self.rollbacks.append(entry)
        self.merged.difference_update(entry.candidate_ids)


def _partial_view(
    ledger: MergeLedgerPort, selected: list[str]
) -> tuple[MergeLedgerPort, RollbackPlan]:
    """The injected ledger view: ``read_by_seq`` returns the filtered entry."""
    plan = _plan(selected)
    return planned_entry_ledger(ledger, plan), plan


async def test_partial_a_then_complementary_partial_b_proceeds() -> None:
    """A compensating entry for A does NOT cover B: the second rollback runs."""
    ledger = _FakeLedger()
    ledger.append(_entry())
    graph = _FakeGraphMerge()
    graph.merged = {_DUP_A, _DUP_B}

    view_a, _ = _partial_view(ledger, [_DUP_A])
    await RollbackMergeUseCase(ledger=view_a, graph_merge=graph).rollback(seq=_SEQ)

    assert len(graph.rollbacks) == 1
    entries = ledger.read_all()
    assert len(entries) == 2
    assert entries[1].rollback_of == _SEQ
    assert entries[1].candidate_ids == [_DUP_A]
    assert [m.duplicate_entity_id for m in entries[1].edge_inverse_map] == [
        _DUP_A,
        _DUP_A,
    ]
    assert entries[1].aliases_folded == [FoldedAlias(from_entity_id=_DUP_A, alias_value="Tool Hub")]

    view_b, _ = _partial_view(ledger, [_DUP_B])
    await RollbackMergeUseCase(ledger=view_b, graph_merge=graph).rollback(seq=_SEQ)

    assert len(graph.rollbacks) == 2
    assert graph.rollbacks[1].candidate_ids == [_DUP_B]
    entries = ledger.read_all()
    assert len(entries) == 3
    assert entries[2].rollback_of == _SEQ
    assert entries[2].candidate_ids == [_DUP_B]
    ledger.verify_chain()


async def test_repeating_the_same_partial_rollback_is_a_noop() -> None:
    """Partial A after partial A: covered by the union -> no graph call, no append."""
    ledger = _FakeLedger()
    ledger.append(_entry())
    graph = _FakeGraphMerge()
    graph.merged = {_DUP_A, _DUP_B}

    view_a, _ = _partial_view(ledger, [_DUP_A])
    await RollbackMergeUseCase(ledger=view_a, graph_merge=graph).rollback(seq=_SEQ)
    assert len(graph.rollbacks) == 1
    assert len(ledger.read_all()) == 2

    view_a_again, _ = _partial_view(ledger, [_DUP_A])
    await RollbackMergeUseCase(ledger=view_a_again, graph_merge=graph).rollback(seq=_SEQ)

    assert len(graph.rollbacks) == 1, "the repeat must not touch the graph again"
    assert len(ledger.read_all()) == 2, "the repeat must not append a compensating entry"
    ledger.verify_chain()


async def test_full_rollback_after_a_partial_one_covers_the_remaining_candidates() -> None:
    """Union semantics: A compensated, then a full request needs only B to proceed.

    The requested set is what the ledger view returns; once the union of the
    compensating entries covers it (here: a full rollback after both partials),
    the rollback no-ops.
    """
    ledger = _FakeLedger()
    ledger.append(_entry())
    graph = _FakeGraphMerge()
    graph.merged = {_DUP_A, _DUP_B}

    view_a, _ = _partial_view(ledger, [_DUP_A])
    await RollbackMergeUseCase(ledger=view_a, graph_merge=graph).rollback(seq=_SEQ)
    view_b, _ = _partial_view(ledger, [_DUP_B])
    await RollbackMergeUseCase(ledger=view_b, graph_merge=graph).rollback(seq=_SEQ)
    assert len(ledger.read_all()) == 3

    # Full view: read_by_seq serves the whole entry; union {A, B} covers it.
    full_view = planned_entry_ledger(ledger, _plan())
    await RollbackMergeUseCase(ledger=full_view, graph_merge=graph).rollback(seq=_SEQ)

    assert len(graph.rollbacks) == 2, "the full request must see full coverage and no-op"
    assert len(ledger.read_all()) == 3
    ledger.verify_chain()


async def test_partially_overlapping_request_is_refused_without_touching_the_graph() -> None:
    """A direct full request after a partial A refuses instead of re-reversing A.

    The plan-time guard preempts this through the CLI; this is the use-case
    backstop for a direct drive or a ledger that changed between plan and
    apply, and it applies the SAME rule: overlap without coverage is refused,
    never double-reversed.
    """
    ledger = _FakeLedger()
    ledger.append(_entry())
    graph = _FakeGraphMerge()
    graph.merged = {_DUP_A, _DUP_B}

    view_a, _ = _partial_view(ledger, [_DUP_A])
    await RollbackMergeUseCase(ledger=view_a, graph_merge=graph).rollback(seq=_SEQ)
    assert len(graph.rollbacks) == 1

    full_view = planned_entry_ledger(ledger, _plan())
    with pytest.raises(MergeNotReversible):
        await RollbackMergeUseCase(ledger=full_view, graph_merge=graph).rollback(seq=_SEQ)

    assert len(graph.rollbacks) == 1, "A must not be reversed twice"
    assert len(ledger.read_all()) == 2, "the refusal must not append an entry"
    ledger.verify_chain()
