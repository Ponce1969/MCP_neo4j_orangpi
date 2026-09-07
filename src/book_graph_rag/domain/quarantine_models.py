"""Quarantine record models for human-review queue (Slice E1).

All models are pure Pydantic + stdlib. A quarantine record is one JSONL line
in the quarantine file; it stays immutable except for the review decision,
which is represented by a new copy via ``model_copy(update=...)``.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

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
