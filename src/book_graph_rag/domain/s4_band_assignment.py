"""S4 confidence-band assignment (Slice D).

Band assignment is a pure, threshold-driven routing function over the evidence
produced by S0–S3.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from book_graph_rag.domain.resolution_models import ConfidenceBand, S3ContextSignals


class BandThresholds(BaseModel):
    """Configurable thresholds for confidence-band assignment.

    Defaults are deliberately conservative so ambiguity lands in ``medium``
    (human review) rather than auto-merge.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    high_cosine: float = 0.90
    high_context: float = 0.50
    medium_cosine: float = 0.80
    conflict_floor: float = 0.10

    @model_validator(mode="after")
    def _validate_ordering(self) -> "BandThresholds":
        if self.high_cosine <= self.medium_cosine:
            raise ValueError(
                f"high_cosine ({self.high_cosine}) must be strictly greater than "
                f"medium_cosine ({self.medium_cosine})"
            )
        return self


def _composite_score(s3: S3ContextSignals | None) -> float:
    """Arithmetic mean of the three S3 overlap signals."""
    if s3 is None:
        return 0.0
    return (s3.mentions_jaccard + s3.related_jaccard + s3.description_overlap) / 3.0


def assign_band(
    s1_cosine: float,
    s3: S3ContextSignals | None,
    s0_match_field: Literal["id", "canonical", "alias", "none"],
    thresholds: BandThresholds,
) -> ConfidenceBand:
    """Assign a confidence band from recorded evidence.

    Order (per design §1.5 / spec R4.1):

    1. **exact**: S0 found a matching id, canonical form, or alias.
    2. **high**: cosine >= ``high_cosine`` AND composite >= ``high_context``
       AND no S3 conflict signal.
    3. **medium**: cosine >= ``medium_cosine`` (but not high criteria).
    4. **low**: everything else.
    """
    if s0_match_field != "none":
        return ConfidenceBand.EXACT

    composite = _composite_score(s3)
    if (
        s1_cosine >= thresholds.high_cosine
        and composite >= thresholds.high_context
        and (s3 is None or not s3.conflict_flag)
    ):
        return ConfidenceBand.HIGH

    if s1_cosine >= thresholds.medium_cosine:
        return ConfidenceBand.MEDIUM

    return ConfidenceBand.LOW
