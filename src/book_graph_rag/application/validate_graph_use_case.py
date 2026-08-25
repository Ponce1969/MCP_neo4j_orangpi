"""Pre-reindex graph validation use case."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from book_graph_rag.domain.validation_models import (
    ApprovalEvidence,
    BookScope,
    EvidenceBundle,
    EvidenceError,
    ManifestIdentity,
    PolicyResult,
    ProtocolIdentity,
    ReadOnlyAssertion,
    RuleEvidence,
    SmokeManifest,
    SmokeOutcome,
    SmokeResult,
    TargetScope,
    ValidationContext,
    ValidationDecision,
    ValidationStatus,
    evaluate_policy,
    exit_code_for,
    fingerprint_configuration,
)
from book_graph_rag.ports.evidence_port import EvidenceWriterPort, ManifestReaderPort
from book_graph_rag.ports.retrieval_smoke_port import RetrievalSmokePort
from book_graph_rag.ports.validation_read_port import GraphValidationReadPort

_PROTOCOL_NAME = "pre-reindex-graph-validation"
_PROTOCOL_VERSION = "1.0.0"

_DECISION_FOR_STATUS = {
    ValidationStatus.PASSED: ValidationDecision.REINDEX,
    ValidationStatus.VIOLATIONS: ValidationDecision.FIX_GRAPH_CODE_BEFORE_REINDEX,
    ValidationStatus.INCOMPLETE: ValidationDecision.INSTRUMENT_BEFORE_REINDEX,
    ValidationStatus.UNREACHABLE: ValidationDecision.FIX_GRAPH_CODE_BEFORE_REINDEX,
    ValidationStatus.FAILED: ValidationDecision.FIX_GRAPH_CODE_BEFORE_REINDEX,
}


def _manifest_identity(manifest: SmokeManifest) -> ManifestIdentity:
    payload = manifest.model_dump_json()
    sha = hashlib.sha256(payload.encode()).hexdigest()
    return ManifestIdentity(id=manifest.manifest_id, version=manifest.version, sha256=sha)


def _is_unreachable(error: Exception) -> bool:
    name = type(error).__name__.lower()
    return any(
        token in name for token in ("connection", "timeout", "auth", "network", "unavailable")
    )


class ValidateGraphUseCase:
    """Orchestrate context freeze -> audit -> smoke -> policy -> evidence bundle."""

    def __init__(
        self,
        validation: GraphValidationReadPort,
        retrieval: RetrievalSmokePort,
        evidence_writer: EvidenceWriterPort,
        manifest_reader: ManifestReaderPort,
    ) -> None:
        self._validation = validation
        self._retrieval = retrieval
        self._writer = evidence_writer
        self._manifest_reader = manifest_reader

    async def execute(
        self,
        *,
        run_id: str,
        target: TargetScope,
        book_scope: BookScope,
        configuration: Mapping[str, Any],
        audit_version: str,
        smoke_manifest: SmokeManifest,
        sample_limit: int,
        output_path: Path,
        approval_path: Path | None = None,
    ) -> tuple[EvidenceBundle, PolicyResult]:
        if not isinstance(sample_limit, int) or isinstance(sample_limit, bool) or sample_limit < 0:
            raise ValueError("sample_limit must be a non-negative integer")
        if smoke_manifest.book_id != book_scope.book_id:
            raise ValueError("smoke manifest book scope does not match book scope")

        started = datetime.now(UTC)
        fingerprint = fingerprint_configuration(configuration)
        context = ValidationContext(
            run_id=run_id,
            protocol=ProtocolIdentity(name=_PROTOCOL_NAME, version=_PROTOCOL_VERSION),
            started_at_utc=started,
            target=target,
            book_scope=book_scope,
            configuration_fingerprint=fingerprint,
            audit_version=audit_version,
            smoke_manifest=_manifest_identity(smoke_manifest),
            historical_evidence_references=(),
            evidence_bundle_id=self._bundle_id(run_id, book_scope.book_id, fingerprint),
        )

        try:
            audit = await self._validation.collect_rule_evidence(book_scope.book_id, sample_limit)
            coverage = await self._validation.collect_coverage(book_scope.book_id)
        except Exception as error:
            status = (
                ValidationStatus.UNREACHABLE if _is_unreachable(error) else ValidationStatus.FAILED
            )
            bundle = self._bundle(
                context,
                status=status,
                audit=(),
                smoke=(),
                coverage=(),
                approval=self._approval(approval_path),
            )
            policy = evaluate_policy(bundle)
            return self._finish(bundle, policy, output_path)

        smoke = await self._run_smoke(smoke_manifest)

        bundle = self._bundle(
            context,
            status=ValidationStatus.INCOMPLETE,
            audit=audit,
            smoke=smoke,
            coverage=coverage,
            approval=self._approval(approval_path),
        )
        policy = evaluate_policy(bundle)
        return self._finish(bundle, policy, output_path)

    async def _run_smoke(self, manifest: SmokeManifest) -> tuple[SmokeResult, ...]:
        results: list[SmokeResult] = []
        for case in manifest.cases:
            try:
                results.append(await self._retrieval.run_case(case))
            except Exception:
                results.append(
                    SmokeResult(
                        case_id=case.case_id,
                        status=SmokeOutcome.ERROR,
                        request_fingerprint="sha256:" + "0" * 64,
                        query_port="unknown",
                        matched_entity_ids=(),
                        matched_chunks=(),
                        error=EvidenceError(code="SMOKE_ERROR", message=None),
                        evidence_ref="smoke://" + case.case_id,
                    )
                )
        return tuple(results)

    def _approval(self, approval_path: Path | None) -> ApprovalEvidence:
        if approval_path is None:
            return ApprovalEvidence(
                approval_id=None,
                validation_run_id=None,
                evidence_bundle_sha256=None,
                backup_plan_ref=None,
                runner_ref=None,
            )
        return self._manifest_reader.read_approval(approval_path)

    @staticmethod
    def _bundle_id(run_id: str, book_id: str, fingerprint: str) -> str:
        digest = hashlib.sha256(f"{run_id}:{book_id}:{fingerprint}".encode()).hexdigest()
        return "bundle-" + digest[:32]

    @staticmethod
    def _bundle(
        context: ValidationContext,
        *,
        status: ValidationStatus,
        audit: tuple[RuleEvidence, ...],
        smoke: tuple[SmokeResult, ...],
        coverage: tuple[Any, ...],
        approval: ApprovalEvidence,
    ) -> EvidenceBundle:
        return EvidenceBundle(
            context=context,
            status=status,
            exit_code=exit_code_for(status),
            decision=_DECISION_FOR_STATUS[status],
            audit=audit,
            smoke=smoke,
            coverage=coverage,
            approval=approval,
            read_only_assertion=ReadOnlyAssertion(forbidden_operations=()),
            evidence_bundle_id=None,
            decision_basis=(),
            blocking_findings=(),
            evidence_references=(),
        )

    def _finish(
        self, bundle: EvidenceBundle, policy: PolicyResult, output_path: Path
    ) -> tuple[EvidenceBundle, PolicyResult]:
        completed_context = bundle.context.model_copy(
            update={"completed_at_utc": datetime.now(UTC)}
        )
        final = bundle.model_copy(
            update={
                "context": completed_context,
                "status": policy.status,
                "exit_code": policy.exit_code,
                "decision": policy.decision,
                "decision_basis": policy.decision_basis,
            }
        )
        self._writer.write_bundle(final, output_path)
        return final, policy
