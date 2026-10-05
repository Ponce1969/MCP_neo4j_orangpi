"""Cross-namespace human decisions (T9a): record + pure read model.

The registry answers "did a human already judge this pair?" without touching
the graph: every record is one append-only JSONL line keyed by
``(seq, candidate_id)`` where ``seq`` is the merge-ledger entry the decision
reviews. ``keep`` records a pair the human kept merged (identity); a
``separate`` decision records that the pair was judged as different concepts
so ``DUPLICATE_ENTITY_CROSS_NAMESPACE`` can stop warning about it (R5a).

The read model is pure and order-dependent: within the record sequence the
LATEST record per key wins, and a later record with a different decision
supersedes the older one — so a decision can be revised by appending, never
by editing.

All models are pure Pydantic + stdlib (domain rules: no infrastructure).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator

from book_graph_rag.domain.resolution_errors import ResolutionError


class DecisionKind(StrEnum):
    """How a human resolved one cross-namespace candidate pair."""

    KEEP = "keep"  # kept merged: one concept, the merge stands
    SEPARATE = "separate"  # judged distinct: keep the entities separate


#: Identity of a decision: the reviewed ledger ``seq`` + the candidate id.
DecisionKey = tuple[int, str]


class CrossNamespaceDecision(BaseModel):
    """One immutable human decision over a merge-ledger candidate.

    ``reason`` is mandatory and non-empty for BOTH kinds (an undocumented
    decision is not a decision); ``batch`` is optional review-batch metadata
    (e.g. ``2B``). ``canonical_id`` is taken from the ledger entry at record
    time — the CLI never accepts it as an argument.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = "1.0.0"
    seq: int
    candidate_id: str
    canonical_id: str
    decision: DecisionKind
    decided_by: str
    decided_at: datetime
    reason: str
    batch: str | None = None

    @field_validator("reason")
    @classmethod
    def _reason_must_be_non_empty(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("reason must be non-empty for both keep and separate")
        return stripped


class InvalidDecisionRecord(ResolutionError):  # noqa: N818
    """Raised when a persisted decision line cannot be parsed (fail closed)."""


def latest_by_key(
    records: Iterable[CrossNamespaceDecision],
) -> dict[DecisionKey, CrossNamespaceDecision]:
    """Return the winning record per ``(seq, candidate_id)`` in input order.

    The LATEST record wins: a later record for the same key — including one
    with a different decision — supersedes the older one.
    """
    latest: dict[DecisionKey, CrossNamespaceDecision] = {}
    for record in records:
        latest[(record.seq, record.candidate_id)] = record
    return latest


def separate_entity_ids(records: Iterable[CrossNamespaceDecision]) -> list[str]:
    """Candidate ids whose LATEST decision per ``candidate_id`` is ``separate``.

    Contract: records are resolved in file order (chronological, last wins —
    the same ordering :func:`latest_by_key` uses, minus the ``seq``
    component) by ``candidate_id`` alone, and only the ids whose winning
    record is ``separate`` are returned, **sorted and unique**. An entity
    whose latest decision is ``keep`` is NEVER excluded — even when an older
    record decided ``separate`` for the same candidate id under a different
    ledger entry — and an entity decided ``separate`` under several ledger
    entries appears exactly once.

    This is the exclusion list R5a and the enqueue detection pass as
    ``$decided_separate_ids``.
    """
    latest_per_candidate: dict[str, CrossNamespaceDecision] = {}
    for record in records:
        latest_per_candidate[record.candidate_id] = record
    return sorted(
        candidate_id
        for candidate_id, record in latest_per_candidate.items()
        if record.decision is DecisionKind.SEPARATE
    )


def keep_pair_keys(records: Iterable[CrossNamespaceDecision]) -> set[DecisionKey]:
    """Keys whose LATEST decision is ``keep`` (the still-merged identity pairs)."""
    return {
        key
        for key, record in latest_by_key(records).items()
        if record.decision is DecisionKind.KEEP
    }


def decided_pair_keys(records: Iterable[CrossNamespaceDecision]) -> set[DecisionKey]:
    """Keys with any decision on record — superseded records excluded."""
    return set(latest_by_key(records))
