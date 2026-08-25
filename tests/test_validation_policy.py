from datetime import UTC, datetime
from typing import Any

import pytest

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


def rule(outcome: v.AuditRuleOutcome = v.AuditRuleOutcome.PASS) -> v.RuleEvidence:
    return v.RuleEvidence(
        rule_id="schema.required.book",
        category=v.AuditRuleCategory.SCHEMA,
        mandatory=True,
        outcome=outcome,
        expected={"state": "present"},
        observed={"state": "present"},
        evidence_ref="audit://rule/1",
    )


def smoke(status: v.SmokeOutcome = v.SmokeOutcome.PASS, book_id: str = "book-1") -> v.SmokeResult:
    return v.SmokeResult(
        case_id="entity-1",
        status=status,
        request_fingerprint="sha256:" + "d" * 64,
        query_port="entity_lookup",
        matched_chunks=(
            v.MatchedChunk(chunk_id="chunk-1", book_id=book_id, page_start=1, page_end=2),
        ),
        evidence_ref="smoke://entity-1",
    )


def bundle(**changes: Any) -> v.EvidenceBundle:
    values: dict[str, Any] = {
        "context": context(),
        "audit": (rule(),),
        "smoke": (smoke(),),
        "coverage": tuple(
            v.CoverageEvidence(scope=name, valid=1, total=1, percentage=100.0)
            for name in ("chunks", "mentions", "related_occurrences")
        ),
        "approval": v.ApprovalEvidence(),
        "read_only_assertion": v.ReadOnlyAssertion(),
    }
    values.update(changes)
    return v.EvidenceBundle(**values)


def test_clean_evidence_recommends_reindex_without_approval() -> None:
    result = v.evaluate_policy(bundle())
    assert result == v.PolicyResult(
        status=v.ValidationStatus.PASSED,
        decision=v.ValidationDecision.REINDEX,
        exit_code=0,
        decision_basis=("strict_gate_passed",),
    )


@pytest.mark.parametrize(
    "outcome",
    [v.AuditRuleOutcome.UNKNOWN, v.AuditRuleOutcome.INCOMPLETE, v.AuditRuleOutcome.UNSUPPORTED],
)
def test_unobservable_evidence_recommends_instrumentation(outcome: v.AuditRuleOutcome) -> None:
    result = v.evaluate_policy(bundle(audit=(rule(outcome),)))
    assert (result.status, result.decision, result.exit_code) == (
        v.ValidationStatus.INCOMPLETE,
        v.ValidationDecision.INSTRUMENT_BEFORE_REINDEX,
        11,
    )


def test_observed_coverage_shortfall_recommends_fix() -> None:
    coverage = tuple(
        v.CoverageEvidence(
            scope=name,
            valid=9999 if name == "chunks" else 1,
            total=10000 if name == "chunks" else 1,
            percentage=99.99 if name == "chunks" else 100.0,
        )
        for name in ("chunks", "mentions", "related_occurrences")
    )
    result = v.evaluate_policy(bundle(coverage=coverage))
    assert (result.status, result.decision) == (
        v.ValidationStatus.VIOLATIONS,
        v.ValidationDecision.FIX_GRAPH_CODE_BEFORE_REINDEX,
    )


def test_unreachable_smoke_leakage_warning_and_write_are_fail_closed() -> None:
    assert v.evaluate_policy(bundle(audit=(rule(v.AuditRuleOutcome.UNREACHABLE),))).exit_code == 12
    assert (
        v.evaluate_policy(bundle(smoke=(smoke(v.SmokeOutcome.ERROR),))).decision
        == v.ValidationDecision.INSTRUMENT_BEFORE_REINDEX
    )
    assert (
        v.evaluate_policy(bundle(smoke=(smoke(book_id="other"),))).status
        == v.ValidationStatus.VIOLATIONS
    )
    warning = v.WarningEvidence(
        rule_id="warning-1", observed_total=1, impact="bounded", acceptance_rationale="reviewed"
    )
    assert (
        v.evaluate_policy(bundle(warnings=(warning,))).decision
        == v.ValidationDecision.INSTRUMENT_BEFORE_REINDEX
    )
    accepted = warning.model_copy(update={"accepted": True, "acceptance_reference": "maintainer-1"})
    assert v.evaluate_policy(bundle(warnings=(accepted,))).decision == v.ValidationDecision.REINDEX
    assert (
        v.evaluate_policy(
            bundle(read_only_assertion=v.ReadOnlyAssertion(graph_writes_attempted=True))
        ).decision
        == v.ValidationDecision.FIX_GRAPH_CODE_BEFORE_REINDEX
    )


def test_missing_coverage_category_is_not_a_pass() -> None:
    coverage = tuple(
        v.CoverageEvidence(scope=name, valid=1, total=1, percentage=100.0)
        for name in ("chunks", "mentions")
    )
    assert v.evaluate_policy(bundle(coverage=coverage)).status == v.ValidationStatus.INCOMPLETE
