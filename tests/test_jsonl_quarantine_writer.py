"""Tests for the JSONL quarantine writer adapter (Slice E1)."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from book_graph_rag.domain.quarantine_models import QuarantineDecision, QuarantineRecord
from book_graph_rag.domain.resolution_models import (
    ConfidenceBand,
    ResolutionEvidence,
    S0NormalizedForm,
)
from book_graph_rag.infrastructure.jsonl_quarantine_writer import JSONLQuarantineWriter


def _dummy_evidence() -> ResolutionEvidence:
    anchor_norm = S0NormalizedForm(
        original="Lang Graph",
        nfkc="Lang Graph",
        casefold="lang graph",
        compact="langgraph",
        tokens=("lang", "graph"),
    )
    candidate_norm = S0NormalizedForm(
        original="LangGraph",
        nfkc="LangGraph",
        casefold="langgraph",
        compact="langgraph",
        tokens=("langgraph",),
    )
    return ResolutionEvidence(
        anchor_id="book:ch1:lang-graph",
        candidate_id="book:ch1:langgraph",
        anchor_type="framework",
        candidate_type="framework",
        anchor_namespace="book:ch1",
        candidate_namespace="book:ch1",
        anchor_normalized=anchor_norm,
        candidate_normalized=candidate_norm,
        s0_matched_field="none",
        band=ConfidenceBand.MEDIUM,
        cross_namespace=False,
        cross_type=False,
    )


@pytest.fixture
def writer(tmp_path: Path) -> JSONLQuarantineWriter:
    return JSONLQuarantineWriter(tmp_path / "resolution" / "quarantine.jsonl")


def test_append_creates_parent_directory(writer: JSONLQuarantineWriter) -> None:
    """The writer must lazily create the quarantine file and its parent dir."""
    record = QuarantineRecord(
        seq=1,
        anchor_id="a",
        candidate_id="b",
        canonical_id=None,
        band=ConfidenceBand.MEDIUM,
        evidence=_dummy_evidence(),
        created_at=datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC),
    )

    writer.append(record)

    assert writer._path.exists()
    assert writer._path.parent.is_dir()
    assert writer.read_all() == [record]


def test_read_pending_filters_out_rejected_and_approved(
    writer: JSONLQuarantineWriter,
) -> None:
    """Only pending decisions are returned for human review."""
    base = QuarantineRecord(
        seq=1,
        anchor_id="a",
        candidate_id="b",
        band=ConfidenceBand.MEDIUM,
        evidence=_dummy_evidence(),
        created_at=datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC),
    )
    pending = base
    approved = base.model_copy(
        update={
            "seq": 2,
            "decision": QuarantineDecision.APPROVED,
            "reviewed_by": "alice",
            "reviewed_at": datetime(2026, 9, 7, 13, 0, 0, tzinfo=UTC),
        }
    )
    rejected = base.model_copy(
        update={
            "seq": 3,
            "decision": QuarantineDecision.REJECTED,
            "reviewed_by": "bob",
            "reviewed_at": datetime(2026, 9, 7, 13, 30, 0, tzinfo=UTC),
        }
    )

    writer.append(pending)
    writer.append(approved)
    writer.append(rejected)

    pending_only = writer.read_pending()
    assert len(pending_only) == 1
    assert pending_only[0].seq == 1
    assert pending_only[0].decision == QuarantineDecision.PENDING


def test_update_decision_rewrites_record_atomically(
    writer: JSONLQuarantineWriter,
) -> None:
    """Approving a pending record updates the file without touching other rows."""
    record = QuarantineRecord(
        seq=1,
        anchor_id="a",
        candidate_id="b",
        band=ConfidenceBand.MEDIUM,
        evidence=_dummy_evidence(),
        created_at=datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC),
    )
    writer.append(record)

    reviewed_at = datetime(2026, 9, 7, 14, 0, 0, tzinfo=UTC)
    writer.update_decision(1, QuarantineDecision.APPROVED, "alice", reviewed_at)

    all_records = writer.read_all()
    assert len(all_records) == 1
    updated = all_records[0]
    assert updated.decision == QuarantineDecision.APPROVED
    assert updated.reviewed_by == "alice"
    assert updated.reviewed_at == reviewed_at


def test_update_decision_is_atomic_when_replace_fails(
    writer: JSONLQuarantineWriter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If os.replace raises mid-update, the original file must remain intact."""
    record = QuarantineRecord(
        seq=1,
        anchor_id="a",
        candidate_id="b",
        band=ConfidenceBand.MEDIUM,
        evidence=_dummy_evidence(),
        created_at=datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC),
    )
    writer.append(record)
    original_content = writer._path.read_text(encoding="utf-8")

    real_replace = os.replace
    calls: list[int] = []

    def _boom_once(src: str, dst: str) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise OSError("simulate crash before atomic replace")
        real_replace(src, dst)

    monkeypatch.setattr(
        "book_graph_rag.infrastructure.jsonl_quarantine_writer.os.replace",
        _boom_once,
    )

    with pytest.raises(OSError, match="simulate crash"):
        writer.update_decision(
            1, QuarantineDecision.APPROVED, "alice", datetime.now(UTC)
        )

    # Original file must be untouched.
    assert writer._path.read_text(encoding="utf-8") == original_content
    assert writer.read_all()[0].decision == QuarantineDecision.PENDING

    # A subsequent call succeeds.
    writer.update_decision(1, QuarantineDecision.APPROVED, "alice", datetime.now(UTC))
    assert writer.read_all()[0].decision == QuarantineDecision.APPROVED
