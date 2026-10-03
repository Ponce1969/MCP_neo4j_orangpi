"""MergeApproval — proof that a human approved one exact cross-namespace merge."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator


class MergeApproval(BaseModel):
    """Frozen, strict record authorizing one exact merge group to cross namespaces.

    Built by the quarantine approval flow (``ApproveQuarantineUseCase``) from the
    reviewed record and consumed by ``ApplyMergeUseCase`` as the only credential
    that lets a group cross the namespace boundary (spec 03 §2.4, policy R6.2,
    design decision D-A2). ``candidate_ids`` must cover exactly the crossing
    candidates of the group; the guard enforces that at apply time.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    quarantine_seq: int
    approved_by: str
    approved_at: datetime
    canonical_id: str
    candidate_ids: tuple[str, ...]

    @field_validator("candidate_ids")
    @classmethod
    def _candidate_ids_not_empty(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """The proof must cover at least one candidate."""
        if not value:
            raise ValueError("candidate_ids must not be empty")
        return value

    @field_validator("approved_by")
    @classmethod
    def _approved_by_not_blank(cls, value: str) -> str:
        """Every approval names the human who made it."""
        if not value.strip():
            raise ValueError("approved_by must not be blank")
        return value
