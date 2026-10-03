"""Chain-hash compatibility of ``EdgeInverseMap.direction`` with old ledger lines (R1 / T5).

The merge ledger is tamper-evident: ``verify_chain`` recomputes ``entry_sha256``
from the *parsed model dump* (``compute_entry_sha256``), not from the raw stored
line. Adding the optional ``direction`` field must therefore keep every
pre-existing (pre-R1) line hashing byte-for-byte identically, or the 958
production entries would fail verification.

These tests pin that contract with an independent legacy payload computation
(the pre-R1 model shape, reproduced locally) and the real ``JSONLMergeLedger``
reader over a temporary file.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import BaseModel, ConfigDict

from book_graph_rag.domain.merge_ledger_models import (
    EdgeInverseMap,
    FoldedAlias,
    MergeBand,
    MergeLedgerEntry,
    compute_entry_sha256,
)
from book_graph_rag.domain.resolution_errors import LedgerChainBroken
from book_graph_rag.domain.resolution_models import (
    ConfidenceBand,
    ResolutionEvidence,
    S0NormalizedForm,
)
from book_graph_rag.infrastructure.jsonl_merge_ledger import JSONLMergeLedger

GENESIS = "0" * 64


class _LegacyEdgeInverseMap(BaseModel):
    """Byte-for-byte reproduction of the pre-R1 model (no ``direction`` field)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    edge_kind: Literal["MENTIONS", "RELATED"]
    duplicate_entity_id: str
    original_other_endpoint_id: str
    edge_properties: dict[str, Any]


def _dummy_evidence(anchor_id: str = "a", candidate_id: str = "b") -> ResolutionEvidence:
    norm = S0NormalizedForm(
        original="X",
        nfkc="X",
        casefold="x",
        compact="x",
        tokens=("x",),
    )
    return ResolutionEvidence(
        anchor_id=anchor_id,
        candidate_id=candidate_id,
        anchor_type="agent",
        candidate_type="agent",
        anchor_namespace="ns",
        candidate_namespace="ns",
        anchor_normalized=norm,
        candidate_normalized=norm,
        s0_matched_field="none",
        band=ConfidenceBand.HIGH,
        cross_namespace=False,
        cross_type=False,
        decided_at=datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC),
    )


def _entry(seq: int, *, direction: Literal["out", "in"] | None = None) -> MergeLedgerEntry:
    return MergeLedgerEntry(
        seq=seq,
        candidate_ids=["dup1"],
        canonical_id="canon",
        band=MergeBand.HIGH,
        evidence=[_dummy_evidence(f"a{seq}", f"b{seq}")],
        aliases_folded=[FoldedAlias(from_entity_id="dup1", alias_value="alias")],
        edge_inverse_map=[
            EdgeInverseMap(
                edge_kind="MENTIONS",
                duplicate_entity_id="dup1",
                original_other_endpoint_id="chunk-1",
                edge_properties={"source_page": 1},
            ),
            EdgeInverseMap(
                edge_kind="RELATED",
                duplicate_entity_id="dup1",
                original_other_endpoint_id="concept-x",
                edge_properties={"type": "requires"},
                direction=direction,
            ),
        ],
        approver="alice",
        applied_at=datetime(2026, 9, 7, 12, seq, 0, tzinfo=UTC),
    )


def _legacy_payload(entry: MergeLedgerEntry) -> str:
    """The exact bytes the pre-R1 model hashed: direction structurally absent.

    Independent of the new serializer: the pre-R1 model had no ``direction``
    key at all, so the legacy digest is the current dump minus every
    ``,"direction":null`` occurrence (legacy entries always carry ``None``).
    """
    payload = entry.model_dump_json(exclude={"entry_sha256", "prev_seq_sha256"})
    return payload.replace(',"direction":null', "")


def _legacy_line(entry: MergeLedgerEntry, prev: str) -> str:
    """Reproduce one pre-R1 ledger line (content + chained hashes) byte-for-byte."""
    with_prev = entry.model_copy(update={"prev_seq_sha256": prev})
    sha = hashlib.sha256(_legacy_payload(with_prev).encode("utf-8")).hexdigest()
    chained = with_prev.model_copy(update={"entry_sha256": sha})
    line = chained.model_dump_json()
    assert '"direction"' not in line, "a pre-R1 line must not contain the direction key"
    return line


def test_legacy_entries_hash_byte_identically_after_direction_field() -> None:
    """An entry without direction hashes exactly as the pre-R1 model did."""
    entry = _entry(1)  # direction defaults to None on both RELATED and MENTIONS

    legacy_sha = hashlib.sha256(_legacy_payload(entry).encode("utf-8")).hexdigest()
    assert compute_entry_sha256(entry) == legacy_sha, (
        "compute_entry_sha256 must ignore direction=None so stored hashes of "
        "the existing ledger entries keep verifying"
    )


def test_legacy_item_dump_matches_pre_direction_model() -> None:
    """Each inverse-map item dumps byte-identically to the pre-R1 model shape."""
    entry = _entry(1)
    related = next(e for e in entry.edge_inverse_map if e.edge_kind == "RELATED")
    legacy_item = _LegacyEdgeInverseMap(
        edge_kind=related.edge_kind,
        duplicate_entity_id=related.duplicate_entity_id,
        original_other_endpoint_id=related.original_other_endpoint_id,
        edge_properties=related.edge_properties,
    )
    assert related.model_dump_json(exclude={"direction"}) == legacy_item.model_dump_json()


def test_direction_is_covered_by_the_entry_hash() -> None:
    """When present, ``direction`` is inside the hashed payload (tamper-evident)."""
    out_entry = _entry(1, direction="out")
    in_entry = _entry(1, direction="in")
    assert out_entry.model_dump_json(exclude={"entry_sha256", "prev_seq_sha256"}) != (
        in_entry.model_dump_json(exclude={"entry_sha256", "prev_seq_sha256"})
    )
    assert '"direction":"out"' in out_entry.model_dump_json(
        exclude={"entry_sha256", "prev_seq_sha256"}
    )


def test_old_format_ledger_lines_still_verify_and_chain_forward(tmp_path: Path) -> None:
    """Two pre-R1 lines verify with the real reader, and a new entry chains onto them."""
    path = tmp_path / "merge_ledger.jsonl"
    lines = [
        _legacy_line(_entry(1), GENESIS),
        _legacy_line(_entry(2), hashlib.sha256(_legacy_payload(_entry(1)).encode()).hexdigest()),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    ledger = JSONLMergeLedger(path)
    ledger.verify_chain()  # must not raise for old-format entries
    assert [e.seq for e in ledger.read_all()] == [1, 2]

    # A new-format entry (direction known) appends and keeps the chain intact.
    ledger.append(_entry(3, direction="out"))
    ledger.verify_chain()
    raw = path.read_text(encoding="utf-8")
    assert '"direction":"out"' in raw.splitlines()[2]


def test_tampering_with_direction_breaks_the_chain(tmp_path: Path) -> None:
    """Flipping a stored direction makes verify_chain raise LedgerChainBroken."""
    path = tmp_path / "merge_ledger.jsonl"
    ledger = JSONLMergeLedger(path)
    ledger.append(_entry(1, direction="out"))

    raw = path.read_text(encoding="utf-8")
    tampered = raw.replace('"direction":"out"', '"direction":"in"')
    assert tampered != raw
    path.write_text(tampered, encoding="utf-8")

    with pytest.raises(LedgerChainBroken) as exc:
        ledger.verify_chain()
    assert exc.value.seq == 1
    assert exc.value.expected != exc.value.actual
