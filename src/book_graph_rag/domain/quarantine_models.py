"""Quarantine record models for human-review queue (Slice E1).

All models are pure Pydantic + stdlib. A quarantine record is one JSONL line
in the quarantine file; it stays immutable except for the review decision,
which is represented by a new copy via ``model_copy(update=...)``.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    SerializerFunctionWrapHandler,
    model_serializer,
)

from book_graph_rag.domain.resolution_models import ConfidenceBand, ResolutionEvidence


class QuarantineDecision(StrEnum):
    """Human-review decision for a quarantine record."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class QuarantineRecord(BaseModel):
    """One JSONL line in the quarantine file.

    Per spec §6 / design §1.7.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = "1.0.0"
    seq: int
    anchor_id: str
    candidate_id: str
    canonical_id: str | None = None
    band: ConfidenceBand
    evidence: ResolutionEvidence
    created_at: datetime
    decision: QuarantineDecision = QuarantineDecision.PENDING
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    #: Mandatory ``quarantine reject --reason`` payload (T6b). ``None`` on
    #: every record written before this field existed and on approvals.
    review_note: str | None = None

    @model_serializer(mode="wrap")
    def _serialize_review_note(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """Omit ``review_note`` when unset so legacy lines stay byte-stable.

        Mirrors the ``merge_ledger_models`` omit-when-absent pattern: records
        without a note dump exactly like the pre-T6b model (the key never
        appears), and files written before the field landed keep parsing (it
        defaults to ``None``).
        """
        data: dict[str, Any] = handler(self)
        if data.get("review_note") is None:
            data.pop("review_note", None)
        return data
