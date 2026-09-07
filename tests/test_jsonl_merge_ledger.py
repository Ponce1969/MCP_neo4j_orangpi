"""Tests for the JSONL merge ledger with chained SHA-256 (Slice E2)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from book_graph_rag.domain.merge_ledger_models import (
    EdgeInverseMap,
    FoldedAlias,
    MergeBand,
    MergeLedgerEntry,
)
from book_graph_rag.domain.resolution_errors import LedgerChainBroken
from book_graph_rag.domain.resolution_models import (
    ConfidenceBand,
    ResolutionEvidence,
    S0NormalizedForm,
)
from book_graph_rag.infrastructure.jsonl_merge_ledger import JSONLMergeLedger


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


@pytest.fixture
def ledger(tmp_path: Path) -> JSONLMergeLedger:
    return JSONLMergeLedger(tmp_path / "merge_ledger.jsonl")


def _entry(seq: int, candidate_ids: list[str], rollback_of: int | None = None) -> MergeLedgerEntry:
    return MergeLedgerEntry(
        seq=seq,
        candidate_ids=candidate_ids,
        canonical_id="canon",
        band=MergeBand.HIGH,
        evidence=[_dummy_evidence(f"a{seq}", f"b{seq}")],
        aliases_folded=[
            FoldedAlias(from_entity_id=c, alias_value=f"alias-{c}") for c in candidate_ids
        ],
        edge_inverse_map=[
            EdgeInverseMap(
                edge_kind="MENTIONS",
                duplicate_entity_id=c,
                original_other_endpoint_id="chunk-1",
                edge_properties={"source_page": 1},
            )
            for c in candidate_ids
        ],
        approver="alice",
        applied_at=datetime(2026, 9, 7, 12, seq, 0, tzinfo=UTC),
        rollback_of=rollback_of,
    )


def test_first_entry_uses_genesis_prev_hash(ledger: JSONLMergeLedger) -> None:
    """The chain starts from the 64-zero genesis SHA-256."""
    entry = _entry(1, ["dup1"])
    ledger.append(entry)

    stored = ledger.read_all()
    assert len(stored) == 1
    assert stored[0].prev_seq_sha256 == "0" * 64
    assert len(stored[0].entry_sha256) == 64
    assert int(stored[0].entry_sha256, 16) > 0


def test_second_entry_chains_to_first(ledger: JSONLMergeLedger) -> None:
    """Each entry's prev_seq_sha256 is the previous entry's entry_sha256."""
    first = _entry(1, ["dup1"])
    second = _entry(2, ["dup2"])
    ledger.append(first)
    ledger.append(second)

    stored = ledger.read_all()
    assert len(stored) == 2
    assert stored[1].prev_seq_sha256 == stored[0].entry_sha256


def test_verify_chain_detects_hand_edited_line(ledger: JSONLMergeLedger) -> None:
    """Mutating a single byte in the ledger raises LedgerChainBroken with diagnostics."""
    entry = _entry(1, ["dup1"])
    ledger.append(entry)

    raw = ledger._path.read_text(encoding="utf-8")
    tampered = raw.replace("2026-09-07T12:01:00Z", "2026-09-07T12:01:01Z")
    assert tampered != raw
    ledger._path.write_text(tampered, encoding="utf-8")

    with pytest.raises(LedgerChainBroken) as exc:
        ledger.verify_chain()

    assert exc.value.seq == 1
    assert exc.value.expected is not None
    assert exc.value.actual is not None
    assert exc.value.expected != exc.value.actual


def test_rollback_of_field_is_preserved(ledger: JSONLMergeLedger) -> None:
    """A compensating entry carries rollback_of pointing to the original seq."""
    original = _entry(1, ["dup1"])
    compensating = _entry(2, ["dup1"], rollback_of=1)
    ledger.append(original)
    ledger.append(compensating)

    ledger.verify_chain()
    by_seq = ledger.read_by_seq(2)
    assert by_seq is not None
    assert by_seq.rollback_of == 1


def test_entry_sha256_is_deterministic(ledger: JSONLMergeLedger) -> None:
    """Hashing the same payload twice yields the same digest."""
    entry = _entry(1, ["dup1"])
    ledger.append(entry)

    stored = ledger.read_all()[0]
    from book_graph_rag.domain.merge_ledger_models import compute_entry_sha256

    recomputed = compute_entry_sha256(stored)
    assert recomputed == stored.entry_sha256


def test_read_by_seq_returns_none_for_missing(ledger: JSONLMergeLedger) -> None:
    """Looking up an unknown seq returns None without raising."""
    assert ledger.read_by_seq(999) is None
