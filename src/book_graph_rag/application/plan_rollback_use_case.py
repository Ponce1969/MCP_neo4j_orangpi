"""PlanRollbackUseCase — read-only direction-aware rollback planning (T8c).

Turns a reviewed ``book-graph-rag ledger rollback`` into three steps:

1. ``plan`` — verify the ledger chain, read each requested entry, probe the
   canonical's current RELATED edges (with their properties, so the provenance
   rule can match the loser's captured ``chunk_index`` when the geometry is
   ``both``/``unknown``) and build the pure domain plan: the entry copy with
   inferred directions, the rule (``geometric``/``provenance``) and reason per
   inference, the predicted census (restored edges, inference successes,
   ``both``/``unknown`` fallbacks and therefore predicted mirrors), the
   affected entities and the fingerprint. Read-only.
2. ``read_census`` — the pre-apply edge census for the affected entities.
3. ``measure`` — after the (CLI-orchestrated) apply, re-probe the duplicates
   and re-read the census so :func:`compare_census` can verify the mutation
   against the prediction.

Applying is *not* this use case's job: the CLI passes the plan's inferred
entry copy to the existing ``RollbackMergeUseCase`` through
:func:`planned_entry_ledger`, whose signature stays untouched.
"""

from __future__ import annotations

from collections.abc import Sequence

from book_graph_rag.domain.merge_ledger_models import (
    EdgeInverseMap,
    MergeLedgerEntry,
)
from book_graph_rag.domain.resolution_errors import (
    MergeNotReversible,
    RollbackTargetInvalid,
)
from book_graph_rag.domain.rollback_plan_models import (
    EdgeCensus,
    RelatedEdgeObservation,
    RollbackMeasurement,
    RollbackPlan,
    build_actual_outcomes,
    build_entry_plan,
    build_rollback_plan,
)
from book_graph_rag.ports.merge_ledger_port import MergeLedgerPort
from book_graph_rag.ports.rollback_plan_port import RollbackPlanPort


def _edge_type(inverse: EdgeInverseMap) -> str:
    return str(inverse.edge_properties.get("type", ""))


def _affected_entities(plan: RollbackPlan) -> list[str]:
    affected: set[str] = set()
    for entry_plan in plan.plans:
        affected.update(entry_plan.affected_entities)
    return sorted(affected)


class PlannedEntryLedger(MergeLedgerPort):
    """Read-through ledger serving the planned (direction-filled) entry copies.

    ``RollbackMergeUseCase`` reads by seq and reverses exactly what it reads;
    wrapping the ledger is how the reviewed plan reaches the unchanged use
    case. Everything else (chain verification, idempotence reads, appending
    the compensating entry) delegates to the real ledger, so the compensating
    entry is still chained to the real previous hash and records the inferred
    directions of the entry it reverses.
    """

    def __init__(self, inner: MergeLedgerPort, planned: dict[int, MergeLedgerEntry]) -> None:
        self._inner = inner
        self._planned = planned

    def append(self, entry: MergeLedgerEntry) -> None:
        self._inner.append(entry)

    def read_all(self) -> list[MergeLedgerEntry]:
        return self._inner.read_all()

    def read_by_seq(self, seq: int) -> MergeLedgerEntry | None:
        planned = self._planned.get(seq)
        if planned is not None:
            return planned
        return self._inner.read_by_seq(seq)

    def verify_chain(self) -> None:
        self._inner.verify_chain()


def planned_entry_ledger(ledger: MergeLedgerPort, plan: RollbackPlan) -> MergeLedgerPort:
    """Ledger view whose ``read_by_seq`` returns each plan's inferred entry."""
    return PlannedEntryLedger(ledger, {p.seq: p.inferred_entry for p in plan.plans})


class PlanRollbackUseCase:
    """Plan (and later verify) explicit ``ledger rollback`` seqs, read-only."""

    def __init__(self, *, ledger: MergeLedgerPort, plan_port: RollbackPlanPort) -> None:
        self._ledger = ledger
        self._plan_port = plan_port

    async def plan(
        self,
        seqs: Sequence[int],
        candidates: Sequence[str] | None = None,
    ) -> RollbackPlan:
        """Build the read-only plan for every requested seq, in order.

        Fails closed: a broken chain raises ``LedgerChainBroken``, a missing
        seq ``RollbackTargetInvalid`` and a compensating entry
        ``MergeNotReversible`` — all before any graph probe can be mistaken
        for an executable plan. The probe's edge properties travel with each
        observation so ``build_entry_plan`` can run the provenance rule after
        the geometric one.

        ``candidates`` selects a subset of ONE entry's candidates (partial
        rollback, T8f): with more than one seq there is no single entry to
        select from, so ``RollbackTargetInvalid`` is raised before any read.
        When given, only the selected candidates' RELATED inverse entries are
        probed and the resulting plan carries the selection (the census, the
        affected entities and the fingerprint all cover only it).

        Overlap rule (fail closed at plan time): compensating entries for the
        seq record the candidates they already reversed, so any requested
        candidate that overlaps that compensated set would be re-reversed by
        the apply (a second compensating entry claiming it again) and only
        fail later in ``compare_census`` — after the graph was mutated. The
        plan therefore raises ``MergeNotReversible`` BEFORE any probe, naming
        the overlap and pointing at the explicit ``--candidate`` path for the
        remaining candidates, so the plan always equals the mutation. The
        candidate-aware union check inside ``RollbackMergeUseCase`` stays as
        the defensive backstop (direct drives, or a ledger that changes
        between plan and apply).
        """
        if candidates is not None and len(seqs) != 1:
            raise RollbackTargetInvalid(
                f"A candidate selection applies to exactly one seq (got {len(seqs)} seqs)"
            )
        self._ledger.verify_chain()
        selected = list(candidates) if candidates is not None else None
        selected_set = set(selected) if selected is not None else None
        entry_plans = []
        for seq in seqs:
            entry = self._ledger.read_by_seq(seq)
            if entry is None:
                raise RollbackTargetInvalid(f"Ledger entry with seq={seq} not found")
            if entry.rollback_of is not None:
                raise MergeNotReversible(
                    f"Entry seq={seq} is already a compensating rollback "
                    f"(rollback_of={entry.rollback_of})"
                )
            # Fail closed before any probe: a candidate already compensated by
            # a prior rollback must never reach the graph again.
            compensated: set[str] = set()
            for prior in self._ledger.read_all():
                if prior.rollback_of == seq:
                    compensated.update(prior.candidate_ids)
            requested = set(candidates) if candidates is not None else set(entry.candidate_ids)
            overlap = sorted(requested & compensated)
            if overlap:
                remaining = sorted(requested - compensated)
                if remaining:
                    hint = (
                        "request only the remaining candidates explicitly with "
                        "--candidate: "
                        + " ".join(f"--candidate {candidate}" for candidate in remaining)
                    )
                else:
                    hint = (
                        "no uncompensated candidates remain for this entry; "
                        "there is nothing left to roll back"
                    )
                raise MergeNotReversible(
                    f"Entry seq={seq} overlaps prior compensations: already-"
                    f"compensated candidates {sorted(compensated)}, requested "
                    f"{sorted(requested)}, overlap {overlap}. {hint}"
                )
            observations: list[RelatedEdgeObservation] = []
            for index, inverse in enumerate(entry.edge_inverse_map):
                if inverse.edge_kind != "RELATED":
                    continue
                if selected_set is not None and inverse.duplicate_entity_id not in selected_set:
                    continue
                probe = await self._plan_port.probe_related_edges(
                    a_id=entry.canonical_id,
                    b_id=inverse.original_other_endpoint_id,
                    edge_type=_edge_type(inverse),
                )
                observations.append(
                    RelatedEdgeObservation(seq=entry.seq, map_index=index, probe=probe)
                )
            entry_plans.append(build_entry_plan(entry, observations, selected_candidates=selected))
        return build_rollback_plan(entry_plans)

    async def read_census(self, plan: RollbackPlan) -> EdgeCensus:
        """Pre-apply edge census for the plan's affected entities."""
        return await self._plan_port.read_edge_census(_affected_entities(plan))

    async def measure(self, plan: RollbackPlan) -> RollbackMeasurement:
        """Post-apply measurement: probe each duplicate and re-read the census."""
        observations: list[RelatedEdgeObservation] = []
        for entry_plan in plan.plans:
            for index, inverse in enumerate(entry_plan.inferred_entry.edge_inverse_map):
                if inverse.edge_kind != "RELATED":
                    continue
                probe = await self._plan_port.probe_related_edges(
                    a_id=inverse.duplicate_entity_id,
                    b_id=inverse.original_other_endpoint_id,
                    edge_type=_edge_type(inverse),
                )
                observations.append(
                    RelatedEdgeObservation(
                        seq=entry_plan.seq,
                        map_index=index,
                        probe=probe,
                    )
                )
        return RollbackMeasurement(
            outcomes=build_actual_outcomes(plan, observations),
            census=await self._plan_port.read_edge_census(_affected_entities(plan)),
        )
