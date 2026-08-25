from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

import book_graph_rag.domain.validation_models as v


def context() -> v.ValidationContext:
    return v.ValidationContext(
        run_id="run-1",
        protocol=v.ProtocolIdentity(name="pre-reindex-graph-validation", version="1.0.0"),
        started_at_utc=datetime(2026, 1, 1, tzinfo=UTC),
        target=v.TargetScope(name="bookgraph-neo4j", database="neo4j", environment="test"),
        book_scope=v.BookScope(
            book_id="book-1", source_identity="source-1", path_fingerprint="sha256:" + "a" * 64
        ),
        configuration_fingerprint="sha256:" + "b" * 64,
        audit_version="audit-1",
        smoke_manifest=v.ManifestIdentity(id="manifest-1", version="1.0.0", sha256="c" * 64),
        evidence_bundle_id="bundle-1",
    )


def test_context_is_frozen_and_fingerprints_preserve_safe_identifiers() -> None:
    value = context()
    with pytest.raises((ValidationError, TypeError)):
        value.run_id = "other"
    assert v.fingerprint_configuration(
        {"database": "neo4j", "password": "one"}
    ) == v.fingerprint_configuration({"password": "two", "database": "neo4j"})
    assert v.fingerprint_request(
        {"source_id": "entity-1", "token": "first"}
    ) == v.fingerprint_request({"token": "second", "source_id": "entity-1"})
    assert v.safe_properties({"source_id": "entity-1", "password": "secret"}) == {
        "source_id": "entity-1"
    }
    with pytest.raises(ValidationError):
        v.ValidationContext(**{**value.model_dump(), "read_only_mode": False})


def test_manifest_and_evidence_reject_missing_or_unsafe_required_values() -> None:
    with pytest.raises(ValidationError):
        v.ManifestIdentity(id="manifest", version="1.0.0", sha256="bad")
    with pytest.raises(ValidationError):
        v.SmokeCase(
            case_id="entity-1",
            kind="entity_lookup",
            request={"text": "book"},
            book_scope="book-1",
            assertion={},
            required_provenance=True,
        )
    with pytest.raises(ValidationError):
        v.CoverageEvidence(scope="chunks", valid=0, total=0, percentage=100.0)
    error = v.EvidenceError(code="DB_ERROR", message="bolt://user:password@host/db")
    assert error.message == "validation operation failed"


def test_rule_smoke_coverage_and_bundle_contracts_are_bounded() -> None:
    rule = v.RuleEvidence(
        rule_id="schema.required.book",
        category=v.AuditRuleCategory.SCHEMA,
        mandatory=True,
        outcome=v.AuditRuleOutcome.PASS,
        expected={"state": "present"},
        observed={"id": "book-1"},
        sample_limit=1,
        sample_rows=({"password": "secret", "id": "book-1"}, {"id": "book-2"}),
        evidence_ref="audit://rule/1",
    )
    result = v.SmokeResult(
        case_id="entity-1",
        status=v.SmokeOutcome.PASS,
        request_fingerprint="sha256:" + "d" * 64,
        query_port="entity_lookup",
        matched_chunks=(
            v.MatchedChunk(chunk_id="chunk-1", book_id="book-1", page_start=1, page_end=2),
        ),
        evidence_ref="smoke://entity-1",
    )
    assert rule.sample_rows == ({"id": "book-1"},)
    assert rule.sample_truncated is True
    assert result.matched_chunks[0].page_start == 1
    assert (
        v.CoverageEvidence(scope="chunks", valid=9999, total=10000, percentage=99.99).outcome
        == v.AuditRuleOutcome.FAIL
    )
    with pytest.raises(ValidationError):
        v.MatchedChunk(chunk_id="chunk-1", book_id="book-1", page_start=3, page_end=2)


def test_bundle_requires_explicit_passing_status_and_keeps_approval_separate() -> None:
    bundle = v.EvidenceBundle(
        context=context(),
        status=v.ValidationStatus.PASSED,
        exit_code=0,
        decision=v.ValidationDecision.REINDEX,
        audit=(),
        smoke=(),
        coverage=(),
        approval=v.ApprovalEvidence(),
        read_only_assertion=v.ReadOnlyAssertion(),
    )
    assert bundle.approval.status == v.ApprovalState.NOT_GRANTED
    assert bundle.read_only_assertion.graph_writes_attempted is False
    with pytest.raises(ValidationError):
        v.EvidenceBundle(
            context=context(),
            status=v.ValidationStatus.PASSED,
            audit=(),
            smoke=(),
            coverage=(),
            approval=v.ApprovalEvidence(),
            read_only_assertion=v.ReadOnlyAssertion(),
        )
