"""RollbackMergeUseCase — fail-closed reversal with compensating ledger entry (Slice E4)."""

from __future__ import annotations

from datetime import UTC, datetime

from book_graph_rag.domain.merge_ledger_models import MergeLedgerEntry
from book_graph_rag.domain.resolution_errors import (
    LedgerChainBroken,
    MergeNotReversible,
    RollbackTargetInvalid,
)
from book_graph_rag.ports.graph_merge_port import GraphMergePort
from book_graph_rag.ports.merge_ledger_port import MergeLedgerPort


class RollbackMergeUseCase:
    """Reverse a previously applied merge and append a compensating ledger entry."""

    def __init__(
        self,
        ledger: MergeLedgerPort,
        graph_merge: GraphMergePort,
    ) -> None:
        self._ledger = ledger
        self._graph_merge = graph_merge

    async def rollback(self, *, seq: int) -> None:
        """Reverse the merge recorded at ``seq``.

        1. Verify the ledger chain (fail-closed on tampering).
        2. Read the original entry by seq.
        3. Reject compensating entries and missing entries.
        4. Reverse the graph mutation.
        5. Append a compensating ledger entry pointing back to ``seq``.

        Idempotency (spec R6.5, candidate-aware since T8f): compensating
        entries for ``seq`` record the candidates they reversed, so the no-op
        only fires when the union of their ``candidate_ids`` COVERS the
        requested set (``entry.candidate_ids`` — the entry this use case read,
        i.e. the selection the injected ``PlannedEntryLedger`` serves for a
        partial rollback). A complementary partial rollback of the same seq
        is not covered yet and proceeds normally.

        Overlap rule (same as the plan-time guard): a request that PARTIALLY
        overlaps the already-compensated candidates is refused, never
        re-reversed. The CLI preempts this case in ``PlanRollbackUseCase``, so
        this is the backstop for a direct drive or a ledger that changed
        between plan and apply; keeping both layers on the same rule means a
        second caller cannot reintroduce the double reversal.
        """
        try:
            self._ledger.verify_chain()
        except LedgerChainBroken:
            # Re-raise so callers get the precise tamper evidence.
            raise

        entry = self._ledger.read_by_seq(seq)
        if entry is None:
            raise RollbackTargetInvalid(f"Ledger entry with seq={seq} not found")

        if entry.rollback_of is not None:
            raise MergeNotReversible(
                f"Entry seq={seq} is already a compensating rollback "
                f"(rollback_of={entry.rollback_of})"
            )

        # Idempotency (spec R6.5): compensating entries for this seq cover
        # only the candidates they recorded. No-op only when that union covers
        # everything still requested; a complementary partial rollback must
        # proceed, and a partial overlap must never re-reverse.
        compensated: set[str] = set()
        for prior in self._ledger.read_all():
            if prior.rollback_of == seq:
                compensated.update(prior.candidate_ids)
        requested = set(entry.candidate_ids)
        if compensated.issuperset(requested):
            return
        overlap = requested & compensated
        if overlap:
            raise MergeNotReversible(
                f"Entry seq={seq} overlaps prior compensations for "
                f"{sorted(overlap)}; request only the uncompensated candidates"
            )

        await self._graph_merge.rollback_merge(entry)

        compensating = self._compensating_entry(entry)
        self._ledger.append(compensating)

    def _compensating_entry(self, original: MergeLedgerEntry) -> MergeLedgerEntry:
        """Build the forward-only compensating entry for ``original``."""
        existing = self._ledger.read_all()
        next_seq = 1 if not existing else max(e.seq for e in existing) + 1
        return MergeLedgerEntry(
            schema_version=original.schema_version,
            seq=next_seq,
            candidate_ids=list(original.candidate_ids),
            canonical_id=original.canonical_id,
            band=original.band,
            evidence=list(original.evidence),
            aliases_folded=list(original.aliases_folded),
            edge_inverse_map=list(original.edge_inverse_map),
            approver="auto:rollback",
            applied_at=datetime.now(UTC),
            rollback_of=original.seq,
        )
