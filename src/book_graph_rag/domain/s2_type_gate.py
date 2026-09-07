"""S2 hard type gate (Slice B).

Type mismatch is a hard boundary: candidates of a different type are never
merged. The gate result is still recorded in evidence for auditability.
"""

from __future__ import annotations

from book_graph_rag.domain.models import EntityType
from book_graph_rag.domain.resolution_models import S2TypeGateResult


def s2_type_gate(anchor_type: EntityType, candidate_type: EntityType) -> S2TypeGateResult:
    """Return the S2 type-gate result for an anchor/candidate pair."""
    passed = anchor_type == candidate_type
    if passed:
        reason = f"anchor type {anchor_type} matches candidate type {candidate_type}"
    else:
        reason = f"type mismatch: {anchor_type} vs {candidate_type}"

    return S2TypeGateResult(
        anchor_type=anchor_type,
        candidate_type=candidate_type,
        passed=passed,
        reason=reason,
    )
