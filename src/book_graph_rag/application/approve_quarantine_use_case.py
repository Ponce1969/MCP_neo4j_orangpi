"""ApproveQuarantineUseCase — review queue → graph merge + ledger (Slice E4).

T6b decision surface:

* ``approve`` / ``approve_many`` validate **before the first write** so any
  refusal (unknown seq, already decided, missing canonical, non-reviewable
  band) leaves the quarantine file untouched. Batches are two-phase: every
  explicit seq is validated first, then decided.
* ``reject`` records the mandatory reviewer ``--reason`` as
  ``QuarantineRecord.review_note`` (same atomic rewrite as the decision) and
  never touches the graph — no backup gate needed for a read-only-to-graph
  decision.

**Band rule (T6a finding, R6.2).** The pipeline short-circuits a same-name
pair to ``exact`` at S0, so a cross-namespace record can carry any band while
the routing rule still sends it to quarantine. The approve path therefore
accepts a record when ``band ∈ {medium, high}`` **or** the record crosses
namespaces (derived with the public ``namespace_from_id`` from the anchor and
candidate ids): the band cannot veto a class that is quarantined regardless of
band. An intra-namespace record outside medium/high stays refused.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict

from book_graph_rag.domain.merge_approval import MergeApproval
from book_graph_rag.domain.merge_ledger_models import MergeLedgerEntry
from book_graph_rag.domain.quarantine_models import QuarantineDecision, QuarantineRecord
from book_graph_rag.domain.resolution_errors import ResolutionError
from book_graph_rag.domain.resolution_models import ConfidenceBand
from book_graph_rag.domain.s0_normalization import namespace_from_id
from book_graph_rag.ports.quarantine_writer_port import QuarantineWriterPort

from .apply_merge_use_case import ApplyMergeUseCase, MergeGroup


class QuarantineDecisionOutcome(BaseModel):
    """One decided seq reported to the CLI (decision + produced ledger seq)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int
    decision: QuarantineDecision
    ledger_seq: int | None = None


class ApproveQuarantineUseCase:
    """Approve or reject pending quarantine records; apply merges on approval."""

    def __init__(
        self,
        quarantine: QuarantineWriterPort,
        apply: ApplyMergeUseCase | None = None,
    ) -> None:
        self._quarantine = quarantine
        self._apply = apply

    async def approve(
        self, *, seq: int, reviewer: str, approver_for_apply: str
    ) -> MergeLedgerEntry:
        """Approve the pending record ``seq`` and apply its merge.

        Every refusal raises ``ResolutionError`` **before** the file is
        touched. On approval, builds the ``MergeApproval`` from the reviewed
        record and passes it to ``ApplyMergeUseCase`` so the legitimate flow
        clears the namespace guard (spec 03 §2.4, design D-A2).
        """
        if self._apply is None:
            raise ResolutionError(
                "ApproveQuarantineUseCase was built without a merge applier; "
                "approve needs ApplyMergeUseCase"
            )
        record = self._require_open_record(seq)
        canonical_id = self._require_canonical(record)
        self._require_reviewable_band(record)

        reviewed_at = datetime.now(UTC)
        self._quarantine.update_decision(
            seq=seq,
            decision=QuarantineDecision.APPROVED,
            reviewed_by=reviewer,
            reviewed_at=reviewed_at,
        )

        apply = self._apply
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
        return await apply.apply(group, approver=approver_for_apply, approval=approval)

    async def approve_many(
        self,
        *,
        seqs: Sequence[int],
        reviewer: str,
        approver_for_apply: str,
    ) -> list[QuarantineDecisionOutcome]:
        """Approve an explicit batch of seqs, all-or-nothing on validation.

        Phase 1 validates every seq (open, canonical, band/crossing rule);
        only then does phase 2 decide and apply one by one, so a refusal never
        leaves a partially decided file. Duplicate seqs are decided once.
        """
        unique = list(dict.fromkeys(seqs))
        if not unique:
            raise ResolutionError("approve requires at least one explicit quarantine seq")
        if self._apply is None:
            raise ResolutionError(
                "ApproveQuarantineUseCase was built without a merge applier; "
                "approve needs ApplyMergeUseCase"
            )
        for seq in unique:
            record = self._require_open_record(seq)
            self._require_canonical(record)
            self._require_reviewable_band(record)

        outcomes: list[QuarantineDecisionOutcome] = []
        for seq in unique:
            entry = await self.approve(
                seq=seq, reviewer=reviewer, approver_for_apply=approver_for_apply
            )
            outcomes.append(
                QuarantineDecisionOutcome(
                    seq=seq,
                    decision=QuarantineDecision.APPROVED,
                    ledger_seq=entry.seq,
                )
            )
        return outcomes

    def reject(
        self,
        *,
        seqs: Sequence[int],
        reviewer: str,
        reason: str,
    ) -> list[QuarantineDecisionOutcome]:
        """Reject explicit seqs, recording ``reason`` as ``review_note``.

        The reason is mandatory and non-empty; it is stripped and persisted
        with the decision in one atomic rewrite. No graph mutation happens,
        so no backup or approval gate applies. Refuses unknown and
        already-decided records before writing anything.
        """
        note = reason.strip()
        if not note:
            raise ResolutionError("reject requires a non-empty reason (--reason)")
        unique = list(dict.fromkeys(seqs))
        if not unique:
            raise ResolutionError("reject requires at least one explicit quarantine seq")
        for seq in unique:
            self._require_open_record(seq)

        reviewed_at = datetime.now(UTC)
        for seq in unique:
            self._quarantine.update_decision_with_note(
                seq=seq,
                decision=QuarantineDecision.REJECTED,
                reviewed_by=reviewer,
                reviewed_at=reviewed_at,
                review_note=note,
            )
        return [
            QuarantineDecisionOutcome(seq=seq, decision=QuarantineDecision.REJECTED)
            for seq in unique
        ]

    def _require_open_record(self, seq: int) -> QuarantineRecord:
        """Return the pending record or refuse (missing / already decided)."""
        record = next((r for r in self._quarantine.read_all() if r.seq == seq), None)
        if record is None:
            raise ResolutionError(f"Quarantine record with seq={seq} not found")
        if record.decision is not QuarantineDecision.PENDING:
            raise ResolutionError(
                f"Quarantine record seq={seq} is already decided "
                f"({record.decision}); refusing to re-decide it"
            )
        return record

    @staticmethod
    def _require_canonical(record: QuarantineRecord) -> str:
        """Return the record's canonical id or refuse before any write."""
        if record.canonical_id is None:
            raise ResolutionError(
                f"Quarantine record seq={record.seq} has no canonical_id; choose the "
                "canonical first: a cross-namespace merge must be approved "
                "against an explicit canonical"
            )
        return record.canonical_id

    @staticmethod
    def _require_reviewable_band(record: QuarantineRecord) -> None:
        """Accept medium/high bands **or** any cross-namespace record.

        R6.2 routes cross-namespace pairs to quarantine regardless of band
        (the S0 short-circuit can label a same-name crossing pair ``exact``),
        so there the band must not veto approval; the namespace guard inside
        ``ApplyMergeUseCase`` still demands the ``MergeApproval``. An
        intra-namespace record outside medium/high stays refused.
        """
        if record.band in {ConfidenceBand.MEDIUM, ConfidenceBand.HIGH}:
            return
        if namespace_from_id(record.anchor_id) != namespace_from_id(record.candidate_id):
            return
        raise ResolutionError(
            f"Quarantine record seq={record.seq} has non-reviewable band "
            f"{record.band} (only medium/high bands or cross-namespace records "
            "may be approved)"
        )
