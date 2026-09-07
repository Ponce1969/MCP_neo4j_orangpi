"""Merge ledger models for tamper-evident applied merges (Slice E2).

All models are pure Pydantic + stdlib. Each applied merge is one JSONL line
with a SHA-256 of its content and a chained hash to the previous entry.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from book_graph_rag.domain.resolution_models import ResolutionEvidence


class MergeBand(StrEnum):
    """Band of an applied merge.

    EXACT and HIGH may be auto-merged; MEDIUM appears only after a human
    approves a quarantine record.
    """

    EXACT = "exact"
    HIGH = "high"
    MEDIUM = "medium"


class FoldedAlias(BaseModel):
    """An alias folded onto the canonical entity by this merge."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    from_entity_id: str
    alias_value: str


class EdgeInverseMap(BaseModel):
    """Records the original endpoint of a re-pointed edge for rollback."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    edge_kind: Literal["MENTIONS", "RELATED"]
    duplicate_entity_id: str
    original_other_endpoint_id: str
    edge_properties: dict[str, Any]


class MergeLedgerEntry(BaseModel):
    """One append-only JSONL line in the merge ledger.

    Per spec §7.7 / design §1.8. The ``entry_sha256`` covers the content
    *excluding* both hash fields so the digest is stable after chaining.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = "1.0.0"
    seq: int
    candidate_ids: list[str]
    canonical_id: str
    band: MergeBand
    evidence: list[ResolutionEvidence]
    aliases_folded: list[FoldedAlias]
    edge_inverse_map: list[EdgeInverseMap]
    approver: str
    applied_at: datetime
    rollback_of: int | None = None

    # Tamper-evident hashes; populated by ``chained_hash`` before persistence.
    entry_sha256: str = ""
    prev_seq_sha256: str = ""


def compute_entry_sha256(entry: MergeLedgerEntry) -> str:
    """SHA-256 of the entry content excluding the hash fields themselves."""
    payload = entry.model_dump_json(exclude={"entry_sha256", "prev_seq_sha256"})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def chained_hash(
    entry: MergeLedgerEntry, prev_entry_sha256: str
) -> MergeLedgerEntry:
    """Return a new entry with ``prev_seq_sha256`` and ``entry_sha256`` set.

    The genesis previous hash is ``"0" * 64``.
    """
    with_prev = entry.model_copy(update={"prev_seq_sha256": prev_entry_sha256})
    sha = compute_entry_sha256(with_prev)
    return with_prev.model_copy(update={"entry_sha256": sha})
