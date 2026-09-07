"""Resolution policy: pure band-to-action mapping (Slice D).

This module encodes the binding matrix from the policy clarification artifact
(engram 1165). It is the final gate before any merge is staged.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from book_graph_rag.domain.resolution_models import ConfidenceBand, ResolutionEvidence
from book_graph_rag.domain.s4_band_assignment import BandThresholds


class MergeAction(StrEnum):
    """Action chosen by the resolution policy for a single candidate pair."""

    AUTO_MERGE = "auto_merge"
    QUARANTINE = "quarantine"
    NO_MERGE = "no_merge"


class ResolutionDecision(BaseModel):
    """The immutable policy decision for one anchor→candidate pair."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence: ResolutionEvidence
    action: MergeAction
    rationale: str


def decide(evidence: ResolutionEvidence, thresholds: BandThresholds) -> ResolutionDecision:
    """Return the merge action and rationale for a pair.

    Routing order (binding matrix):

    1. ``cross_type`` → ``NO_MERGE`` always (R6.1).
    2. ``cross_namespace`` → ``QUARANTINE`` always (R6.2), regardless of band.
    3. Band routing:
       * ``exact`` → ``AUTO_MERGE``
       * ``high`` → ``QUARANTINE`` (human-confirm queue; NOT auto-applied)
       * ``medium`` → ``QUARANTINE``
       * ``low`` → ``NO_MERGE``

    The ``thresholds`` argument is part of the public contract so that future
    band-dependent policy tweaks remain explicit; today only ``band`` is used.
    """
    if evidence.cross_type:
        return ResolutionDecision(
            evidence=evidence,
            action=MergeAction.NO_MERGE,
            rationale=(
                f"NO_MERGE per R6.1: type boundary violation "
                f"({evidence.anchor_type} vs {evidence.candidate_type})"
            ),
        )

    if evidence.cross_namespace:
        return ResolutionDecision(
            evidence=evidence,
            action=MergeAction.QUARANTINE,
            rationale=(
                f"QUARANTINE per R6.2: cross-namespace candidate "
                f"({evidence.anchor_namespace} vs {evidence.candidate_namespace})"
            ),
        )

    if evidence.band == ConfidenceBand.EXACT:
        return ResolutionDecision(
            evidence=evidence,
            action=MergeAction.AUTO_MERGE,
            rationale="AUTO_MERGE: exact S0 id/canonical/alias match",
        )

    if evidence.band == ConfidenceBand.HIGH:
        return ResolutionDecision(
            evidence=evidence,
            action=MergeAction.QUARANTINE,
            rationale=(
                "QUARANTINE: high-band pair requires human confirmation "
                "before merge (human-confirm queue)"
            ),
        )

    if evidence.band == ConfidenceBand.MEDIUM:
        return ResolutionDecision(
            evidence=evidence,
            action=MergeAction.QUARANTINE,
            rationale="QUARANTINE: medium-band pair awaits human review",
        )

    return ResolutionDecision(
        evidence=evidence,
        action=MergeAction.NO_MERGE,
        rationale="NO_MERGE: low-band pair below merge thresholds",
    )
