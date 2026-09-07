"""ApproveQuarantineUseCase — review queue → graph merge + ledger (Slice E4)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

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

    async def approve(
        self, *, seq: int, reviewer: str, approver_for_apply: str
    ) -> Any:
        """Approve the pending quarantine record with ``seq`` and apply the merge.

        Raises ``ResolutionError`` if the record is missing or already decided.
        """
        pending = self._quarantine.read_pending()
        record = next((r for r in pending if r.seq == seq), None)
        if record is None:
            raise ResolutionError(f"Pending quarantine record with seq={seq} not found")

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

        group = MergeGroup(
            canonical_id=record.canonical_id or record.anchor_id,
            duplicate_ids=[record.candidate_id],
            band=record.band,
            evidence=[record.evidence],
        )
        return await self._apply.apply(group, approver=approver_for_apply)
