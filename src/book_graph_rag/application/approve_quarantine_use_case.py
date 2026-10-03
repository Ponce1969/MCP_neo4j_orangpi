"""ApproveQuarantineUseCase — review queue → graph merge + ledger (Slice E4)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from book_graph_rag.domain.merge_approval import MergeApproval
from book_graph_rag.domain.quarantine_models import QuarantineDecision
from book_graph_rag.domain.resolution_errors import ResolutionError
from book_graph_rag.domain.resolution_models import ConfidenceBand
from book_graph_rag.ports.quarantine_writer_port import QuarantineWriterPort

from .apply_merge_use_case import ApplyMergeUseCase, MergeGroup


class ApproveQuarantineUseCase:
    """Approve or reject a pending quarantine record and apply on approval."""

    def __init__(
        self,
        quarantine: QuarantineWriterPort,
        apply: ApplyMergeUseCase,
    ) -> None:
        self._quarantine = quarantine
        self._apply = apply

    async def approve(self, *, seq: int, reviewer: str, approver_for_apply: str) -> Any:
        """Approve the pending quarantine record with ``seq`` and apply the merge.

        Raises ``ResolutionError`` if the record is missing, already decided, or
        never chose a ``canonical_id`` (a cross-namespace merge must be approved
        against an explicit canonical). On approval, builds the
        ``MergeApproval`` from the reviewed record and passes it to
        ``ApplyMergeUseCase`` so the legitimate flow clears the namespace
        guard (spec 03 §2.4, design D-A2).
        """
        pending = self._quarantine.read_pending()
        record = next((r for r in pending if r.seq == seq), None)
        if record is None:
            raise ResolutionError(f"Pending quarantine record with seq={seq} not found")
        if record.canonical_id is None:
            raise ResolutionError(
                f"Quarantine record seq={seq} has no canonical_id; choose the "
                "canonical first: a cross-namespace merge must be approved "
                "against an explicit canonical"
            )

        reviewed_at = datetime.now(UTC)
        self._quarantine.update_decision(
            seq=seq,
            decision=QuarantineDecision.APPROVED,
            reviewed_by=reviewer,
            reviewed_at=reviewed_at,
        )

        if record.band not in {ConfidenceBand.MEDIUM, ConfidenceBand.HIGH}:
            raise ResolutionError(
                f"Quarantine record seq={seq} has non-reviewable band {record.band}"
            )

        canonical_id = record.canonical_id
        stored = next(r for r in self._quarantine.read_all() if r.seq == seq)
        approval = MergeApproval(
            quarantine_seq=stored.seq,
            approved_by=stored.reviewed_by or reviewer,
            approved_at=stored.reviewed_at or reviewed_at,
            canonical_id=canonical_id,
            candidate_ids=(record.candidate_id,),
        )
        group = MergeGroup(
            canonical_id=canonical_id,
            duplicate_ids=[record.candidate_id],
            band=record.band,
            evidence=[record.evidence],
        )
        return await self._apply.apply(group, approver=approver_for_apply, approval=approval)
