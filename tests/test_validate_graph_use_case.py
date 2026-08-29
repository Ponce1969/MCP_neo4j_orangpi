"""Tests for ValidateGraphUseCase orchestration."""
from __future__ import annotations

from pathlib import Path

import pytest

from book_graph_rag.application.validate_graph_use_case import ValidateGraphUseCase
from book_graph_rag.domain.validation_models import (
    ApprovalEvidence,
    AuditRuleCategory,
    AuditRuleOutcome,
    BookScope,
    CoverageEvidence,
    EvidenceBundle,
    PolicyResult,
    RuleEvidence,
    SmokeCase,
    SmokeManifest,
    SmokeOutcome,
    SmokeResult,
    TargetScope,
    ValidationDecision,
    ValidationStatus,
)
from book_graph_rag.ports.evidence_port import EvidenceWriterPort, ManifestReaderPort
from book_graph_rag.ports.retrieval_smoke_port import RetrievalSmokePort
from book_graph_rag.ports.validation_read_port import GraphValidationReadPort


class _FakeValidationRead(GraphValidationReadPort):
    def __init__(
        self,
        evidence: tuple[RuleEvidence, ...] = (),
        coverage: tuple[CoverageEvidence, ...] = (),
    ) -> None:
        self._evidence = evidence
        self._coverage = coverage

    async def collect_rule_evidence(
        self, book_id: str, sample_limit: int
    ) -> tuple[RuleEvidence, ...]:
        return self._evidence

    async def collect_coverage(self, book_id: str) -> tuple[CoverageEvidence, ...]:
        return self._coverage


class _FakeRetrieval(RetrievalSmokePort):
    def __init__(self, result: SmokeResult) -> None:
        self._result = result

    async def run_case(self, case: SmokeCase) -> SmokeResult:
        return self._result


class _FakeEvidenceWriter(EvidenceWriterPort):
    def __init__(self) -> None:
        self.written: list[EvidenceBundle] = []

    def write_bundle(self, bundle: EvidenceBundle, path: Path) -> None:
        self.written.append(bundle)


class _FakeManifestReader(ManifestReaderPort):
    def read_smoke_manifest(self, path: Path) -> SmokeManifest:
        raise NotImplementedError

    def read_approval(self, path: Path) -> ApprovalEvidence:
        raise NotImplementedError


def _pass_rule() -> RuleEvidence:
    return RuleEvidence(
        rule_id="schema.required.book",
        category=AuditRuleCategory.SCHEMA,
        mandatory=True,
        outcome=AuditRuleOutcome.PASS,
        expected={"state": "present"},
        observed={"state": "present"},
        evidence_ref="audit://rule/1",
    )


def _full_coverage() -> tuple[CoverageEvidence, ...]:
    return tuple(
        CoverageEvidence(scope=name, valid=1, total=1, percentage=100.0)
        for name in ("chunks", "mentions", "related_occurrences")
    )


def _target() -> TargetScope:
    return TargetScope(name="bookgraph-neo4j", database="neo4j", environment="test")


def _book_scope() -> BookScope:
    return BookScope(
        book_id="book-1",
        source_identity="source-1",
        path_fingerprint="sha256:" + "a" * 64,
    )


def _manifest() -> SmokeManifest:
    case = SmokeCase(
        case_id="entity-1",
        kind="entity_lookup",
        request={"text": "book"},
        book_scope="book-1",
        assertion={"expected_entity_ids": ["entity-1"]},
        required_provenance=True,
    )
    return SmokeManifest(
        manifest_id="manifest-1", version="1.0.0", book_id="book-1", cases=(case,)
    )


def _pass_smoke_result(case: SmokeCase) -> SmokeResult:
    return SmokeResult(
        case_id=case.case_id,
        status=SmokeOutcome.PASS,
        request_fingerprint="sha256:" + "b" * 64,
        query_port="entity_lookup",
        matched_entity_ids=(),
        matched_chunks=(),
        evidence_ref="smoke://" + case.case_id,
    )


def _build_use_case(
    evidence: tuple[RuleEvidence, ...],
    coverage: tuple[CoverageEvidence, ...],
    result: SmokeResult,
) -> tuple[ValidateGraphUseCase, _FakeEvidenceWriter]:
    writer = _FakeEvidenceWriter()
    use_case = ValidateGraphUseCase(
        _FakeValidationRead(evidence, coverage),
        _FakeRetrieval(result),
        writer,
        _FakeManifestReader(),
    )
    return use_case, writer


async def _run(
    evidence: tuple[RuleEvidence, ...],
    coverage: tuple[CoverageEvidence, ...],
    result: SmokeResult,
) -> tuple[EvidenceBundle, PolicyResult, _FakeEvidenceWriter]:
    use_case, writer = _build_use_case(evidence, coverage, result)
    bundle, policy = await use_case.execute(
        run_id="run-1",
        target=_target(),
        book_scope=_book_scope(),
        configuration={},
        audit_version="audit-1",
        smoke_manifest=_manifest(),
        sample_limit=50,
        output_path=Path("out.json"),
    )
    return bundle, policy, writer


async def test_clean_evidence_recommends_reindex() -> None:
    case = _manifest().cases[0]
    bundle, policy, writer = await _run(
        (_pass_rule(),), _full_coverage(), _pass_smoke_result(case)
    )
    assert policy == PolicyResult(
        status=ValidationStatus.PASSED,
        decision=ValidationDecision.REINDEX,
        exit_code=0,
        decision_basis=("strict_gate_passed",),
    )
    assert bundle.status == ValidationStatus.PASSED
    assert bundle.decision == ValidationDecision.REINDEX
    assert bundle.exit_code == 0
    assert len(writer.written) == 1


async def test_manifest_book_scope_mismatch_is_rejected() -> None:
    use_case, _ = _build_use_case((), (), _pass_smoke_result(_manifest().cases[0]))
    manifest = SmokeManifest(
        manifest_id="manifest-1", version="1.0.0", book_id="other-book", cases=()
    )
    with pytest.raises(ValueError, match="book scope"):
        await use_case.execute(
            run_id="run-1",
            target=_target(),
            book_scope=_book_scope(),
            configuration={},
            audit_version="audit-1",
            smoke_manifest=manifest,
            sample_limit=50,
            output_path=Path("out.json"),
        )
