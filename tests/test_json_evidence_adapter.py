"""Tests for JSONEvidenceAdapter."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from book_graph_rag.domain.validation_models import (
    ApprovalEvidence,
    ApprovalState,
    BookScope,
    EvidenceBundle,
    ManifestIdentity,
    ProtocolIdentity,
    ReadOnlyAssertion,
    SmokeCase,
    SmokeManifest,
    TargetScope,
    ValidationContext,
    ValidationDecision,
    ValidationStatus,
)
from book_graph_rag.infrastructure.json_evidence_adapter import JSONEvidenceAdapter


def _context() -> ValidationContext:
    return ValidationContext(
        run_id="run-1",
        protocol=ProtocolIdentity(name="pre-reindex-graph-validation", version="1.0.0"),
        started_at_utc=datetime(2026, 1, 1, tzinfo=UTC),
        target=TargetScope(name="bookgraph-neo4j", database="neo4j", environment="test"),
        book_scope=BookScope(
            book_id="book-1",
            source_identity="source-1",
            path_fingerprint="sha256:" + "a" * 64,
        ),
        configuration_fingerprint="sha256:" + "b" * 64,
        audit_version="audit-1",
        smoke_manifest=ManifestIdentity(
            id="manifest-1", version="1.0.0", sha256="c" * 64
        ),
        evidence_bundle_id="bundle-1",
    )


def _bundle() -> EvidenceBundle:
    return EvidenceBundle(
        context=_context(),
        status=ValidationStatus.PASSED,
        exit_code=0,
        decision=ValidationDecision.REINDEX,
        audit=(),
        smoke=(),
        coverage=(),
        approval=ApprovalEvidence(),
        read_only_assertion=ReadOnlyAssertion(),
    )


def test_write_bundle_roundtrip(tmp_path: Path) -> None:
    adapter = JSONEvidenceAdapter()
    path = tmp_path / "bundle.json"
    adapter.write_bundle(_bundle(), path)
    loaded = EvidenceBundle.model_validate_json(path.read_text(encoding="utf-8"))
    assert loaded.evidence_bundle_id == "bundle-1"
    assert loaded.status == ValidationStatus.PASSED


def test_read_smoke_manifest(tmp_path: Path) -> None:
    adapter = JSONEvidenceAdapter()
    path = tmp_path / "manifest.json"
    manifest = SmokeManifest(
        manifest_id="manifest-1",
        version="1.0.0",
        book_id="book-1",
        cases=(
            SmokeCase(
                case_id="c1",
                kind="entity_lookup",
                request={"text": "x"},
                book_scope="book-1",
                assertion={"expected_entity_ids": ["e1"]},
                required_provenance=True,
            ),
        ),
    )
    path.write_text(manifest.model_dump_json(), encoding="utf-8")
    loaded = adapter.read_smoke_manifest(path)
    assert loaded.manifest_id == "manifest-1"
    assert len(loaded.cases) == 1


def test_read_approval(tmp_path: Path) -> None:
    adapter = JSONEvidenceAdapter()
    path = tmp_path / "approval.json"
    path.write_text("{}", encoding="utf-8")
    loaded = adapter.read_approval(path)
    assert loaded.status == ApprovalState.NOT_GRANTED
