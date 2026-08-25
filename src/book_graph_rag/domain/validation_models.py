"""Frozen, secret-safe contracts and policy for pre-reindex validation."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ValidationModel(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")


ID = Annotated[str, Field(min_length=1, description="Stable contract identifier")]
TEXT = Annotated[str, Field(min_length=1, max_length=500, description="Bounded contract text")]
HASH = Annotated[str, Field(pattern=r"sha256:[0-9a-f]{64}", description="SHA-256 fingerprint")]
OPT_ID = Annotated[str | None, Field(default=None, description="Optional identifier")]
STRINGS = Annotated[tuple[str, ...], Field(default=(), description="Ordered identifiers")]


class ValidationStatus(StrEnum):
    PASSED = "passed"
    VIOLATIONS = "violations"
    INCOMPLETE = "incomplete"
    UNREACHABLE = "unreachable"
    FAILED = "failed"


class ValidationDecision(StrEnum):
    REINDEX = "REINDEX"
    INSTRUMENT_BEFORE_REINDEX = "INSTRUMENT_BEFORE_REINDEX"
    FIX_GRAPH_CODE_BEFORE_REINDEX = "FIX_GRAPH_CODE_BEFORE_REINDEX"


class AuditRuleCategory(StrEnum):
    CONTEXT = "context"
    INVENTORY = "inventory"
    SCHEMA = "schema"
    HIERARCHY = "hierarchy"
    ENDPOINT = "endpoint"
    PAGE = "page"
    PROVENANCE = "provenance"
    RELATIONSHIP = "relationship"
    ISOLATION = "isolation"
    ACCOUNTING = "accounting"


class AuditRuleOutcome(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    WARNING = "warning"
    INCOMPLETE = "incomplete"
    UNKNOWN = "unknown"
    UNREACHABLE = "unreachable"
    UNSUPPORTED = "unsupported"
    NOT_APPLICABLE = "not_applicable"


class SmokeOutcome(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    WARNING = "warning"
    UNKNOWN = "unknown"
    INCOMPLETE = "incomplete"
    UNREACHABLE = "unreachable"
    ERROR = "error"


class ApprovalState(StrEnum):
    NOT_GRANTED = "not_granted"
    GRANTED = "granted"
    INVALID = "invalid"
    EXPIRED = "expired"


class ProtocolIdentity(ValidationModel):
    name: ID
    version: TEXT


class TargetScope(ValidationModel):
    name: ID
    database: TEXT
    environment: TEXT


class BookScope(ValidationModel):
    book_id: ID
    source_identity: ID
    source_revision: str | None = Field(default=None, description="Source revision")
    path: str | None = Field(default=None, description="Permitted source path")
    path_fingerprint: HASH


class ManifestIdentity(ValidationModel):
    id: ID
    version: TEXT
    sha256: Annotated[str, Field(pattern=r"[0-9a-f]{64}", description="Manifest SHA-256")]


class ValidationContext(ValidationModel):
    run_id: ID
    protocol: ProtocolIdentity
    started_at_utc: datetime
    completed_at_utc: datetime | None = Field(default=None, description="Completion time")
    target: TargetScope
    book_scope: BookScope
    configuration_fingerprint: HASH
    audit_version: TEXT
    smoke_manifest: ManifestIdentity
    read_only_mode: Literal[True] = True
    historical_evidence_references: STRINGS
    evidence_bundle_id: ID


class EvidenceError(ValidationModel):
    code: str | None = Field(default=None, description="Bounded stable error code")
    message: str | None = Field(default=None, description="Sanitized error message")

    @model_validator(mode="before")
    @classmethod
    def sanitize(cls, data: Any) -> Any:
        if not isinstance(data, Mapping):
            return data
        code = data.get("code")
        safe_code = (
            code
            if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_.-]{0,63}", code)
            else None
        )
        return {
            "code": safe_code,
            "message": "validation operation failed" if data.get("message") else None,
        }


class RuleEvidence(ValidationModel):
    rule_id: ID
    category: AuditRuleCategory
    mandatory: bool
    outcome: AuditRuleOutcome
    expected: Any = Field(description="Machine-readable expectation")
    observed: Any = Field(description="Current-run observation or null")
    violation_count: int = Field(default=0, ge=0, description="Observed violation count")
    sample_limit: int = Field(default=0, ge=0, description="Maximum sample rows")
    sample_rows: tuple[dict[str, Any], ...] = Field(default=(), description="Bounded safe samples")
    sample_truncated: bool = Field(default=False, description="Whether samples were bounded")
    evidence_ref: ID
    duration_ms: float = Field(default=0.0, ge=0, description="Observed duration")
    error: EvidenceError | None = Field(default=None, description="Sanitized operation error")

    @model_validator(mode="before")
    @classmethod
    def sanitize_samples(cls, data: Any) -> Any:
        if not isinstance(data, Mapping):
            return data
        limit = data.get("sample_limit", 0)
        rows = tuple(data.get("sample_rows", ()))
        if not isinstance(limit, int) or limit < 0:
            raise ValueError("sample_limit must be non-negative")
        safe_rows = tuple(safe_properties(row) for row in rows if isinstance(row, Mapping))
        return {**data, "sample_rows": safe_rows[:limit], "sample_truncated": len(rows) > limit}

    @model_validator(mode="after")
    def consistent_outcome(self) -> RuleEvidence:
        if self.outcome == AuditRuleOutcome.PASS and self.violation_count:
            raise ValueError("passing rule cannot report violations")
        return self


class SmokeCase(ValidationModel):
    case_id: ID
    kind: Literal["entity_lookup", "relationship_lookup", "two_hop_path", "chunk_search"]
    request: dict[str, Any] = Field(description="Deterministic retrieval request")
    book_scope: ID
    assertion: dict[str, Any] = Field(description="Expected identity assertion")
    required_provenance: bool

    @model_validator(mode="before")
    @classmethod
    def secret_safe_inputs(cls, data: Any) -> Any:
        if isinstance(data, Mapping):
            return {
                **data,
                "request": safe_properties(data.get("request", {})),
                "assertion": safe_properties(data.get("assertion", {})),
            }
        return data

    @model_validator(mode="after")
    def requires_expected_result(self) -> SmokeCase:
        expected = {
            "entity_lookup": "expected_entity_ids",
            "relationship_lookup": "expected_triples",
            "two_hop_path": "expected_paths",
            "chunk_search": "expected_chunk_ids",
        }[self.kind]
        if not self.assertion.get(expected):
            raise ValueError(f"{self.kind} requires {expected}")
        return self


class SmokeManifest(ValidationModel):
    manifest_id: ID
    version: TEXT
    book_id: ID
    cases: tuple[SmokeCase, ...] = Field(description="Deterministic smoke cases")

    @model_validator(mode="after")
    def validate_cases(self) -> SmokeManifest:
        ids = [case.case_id for case in self.cases]
        if len(ids) != len(set(ids)) or any(case.book_scope != self.book_id for case in self.cases):
            raise ValueError("smoke cases require unique ids and the manifest book scope")
        return self


class MatchedChunk(ValidationModel):
    chunk_id: ID
    book_id: ID
    chapter_id: OPT_ID
    section_id: OPT_ID
    page_start: int | None = Field(default=None, description="First source page")
    page_end: int | None = Field(default=None, description="Last source page")

    @model_validator(mode="after")
    def valid_page_range(self) -> MatchedChunk:
        page_start = self.page_start
        page_end = self.page_end
        if (page_start is None) != (page_end is None):
            raise ValueError("matched chunk page range is invalid")
        if (
            page_start is not None
            and page_end is not None
            and (page_start < 1 or page_start > page_end)
        ):
            raise ValueError("matched chunk page range is invalid")
        return self


class SmokeResult(ValidationModel):
    case_id: ID
    status: SmokeOutcome
    request_fingerprint: HASH
    query_port: ID
    matched_entity_ids: STRINGS
    matched_relationship_triples: tuple[tuple[str, str, str], ...] = Field(
        default=(), description="Observed relationship triples"
    )
    matched_paths: tuple[Any, ...] = Field(default=(), description="Observed paths")
    matched_chunks: tuple[MatchedChunk, ...]
    match_tier: Literal["exact", "case_insensitive", "alias", "substring", "fulltext"] | None = (
        Field(default=None, description="Observed match tier")
    )
    scores: tuple[float, ...] = Field(default=(), description="Observed scores")
    duplicate_count: int = Field(default=0, ge=0, description="Observed duplicate count")
    latency_ms: float = Field(default=0.0, ge=0, description="Observed latency")
    error: EvidenceError | None = Field(default=None, description="Sanitized retrieval error")
    evidence_ref: ID


class CoverageEvidence(ValidationModel):
    scope: ID
    valid: int = Field(ge=0, description="Valid observed records")
    total: int = Field(ge=0, description="Applicable observed records")
    percentage: float = Field(ge=0, le=100, description="Observed coverage percentage")
    outcome: AuditRuleOutcome = Field(default=AuditRuleOutcome.PASS, description="Coverage outcome")
    applicable: bool = Field(default=True, description="Whether records are applicable")
    reason: str | None = Field(default=None, description="Reason for an inapplicable category")

    @model_validator(mode="after")
    def validate_coverage(self) -> CoverageEvidence:
        expected = self.valid / self.total * 100 if self.total else 100.0
        if self.valid > self.total or abs(self.percentage - expected) > 1e-9:
            raise ValueError("coverage does not match observed counts")
        if self.total == 0 and self.applicable:
            raise ValueError("zero denominator must be explicitly inapplicable")
        if self.total > 0 and not self.applicable:
            raise ValueError("nonzero denominator must be applicable")
        if self.total and self.percentage < 100 and self.outcome == AuditRuleOutcome.PASS:
            object.__setattr__(self, "outcome", AuditRuleOutcome.FAIL)
        return self


class WarningEvidence(ValidationModel):
    rule_id: ID
    observed_total: int = Field(ge=0, description="Bounded warning total")
    impact: TEXT
    acceptance_rationale: TEXT
    accepted: bool = Field(default=False, description="Explicit maintainer acceptance")
    acceptance_reference: OPT_ID
    structural_impact: bool = Field(default=False, description="Whether warning affects integrity")

    @model_validator(mode="after")
    def validate_acceptance(self) -> WarningEvidence:
        if self.accepted and not self.acceptance_reference:
            raise ValueError("accepted warning requires acceptance_reference")
        return self


class ApprovalEvidence(ValidationModel):
    required: bool = Field(default=True, description="Whether separate approval is required")
    status: ApprovalState = Field(default=ApprovalState.NOT_GRANTED, description="Approval state")
    approval_id: OPT_ID
    approved_by: str | None = Field(default=None, description="Maintainer identity")
    approved_at_utc: datetime | None = Field(default=None, description="Approval timestamp")
    approved_decision: ValidationDecision | None = Field(
        default=None, description="Approved decision"
    )
    validation_run_id: OPT_ID
    evidence_bundle_sha256: OPT_ID
    backup_plan_ref: OPT_ID
    runner_ref: OPT_ID
    scope: str | None = Field(default=None, description="Approved scope")

    @model_validator(mode="after")
    def granted_is_complete(self) -> ApprovalEvidence:
        if self.status == ApprovalState.GRANTED and (
            not self.approval_id
            or not self.approved_by
            or not self.approved_at_utc
            or self.approved_decision != ValidationDecision.REINDEX
            or not self.validation_run_id
            or not self.evidence_bundle_sha256
            or not self.backup_plan_ref
            or not self.runner_ref
            or not self.scope
        ):
            raise ValueError("granted approval must bind the complete reindex record")
        return self


class ReadOnlyAssertion(ValidationModel):
    graph_writes_attempted: bool = Field(
        default=False, description="Whether graph writes were attempted"
    )
    forbidden_operations: STRINGS


class EvidenceBundle(ValidationModel):
    evidence_bundle_id: OPT_ID
    context: ValidationContext
    status: ValidationStatus = Field(
        default=ValidationStatus.INCOMPLETE, description="Overall validation status"
    )
    exit_code: int = Field(default=11, description="Stable process exit code")
    decision: ValidationDecision = Field(
        default=ValidationDecision.INSTRUMENT_BEFORE_REINDEX, description="Policy recommendation"
    )
    decision_basis: STRINGS
    audit: tuple[RuleEvidence, ...] = Field(description="Current-run audit evidence")
    smoke: tuple[SmokeResult, ...] = Field(description="Current-run smoke evidence")
    coverage: tuple[CoverageEvidence, ...] = Field(description="Current-run coverage evidence")
    warnings: tuple[WarningEvidence, ...] = Field(
        default=(), description="Accepted warning evidence"
    )
    blocking_findings: STRINGS
    approval: ApprovalEvidence
    evidence_references: STRINGS
    read_only_assertion: ReadOnlyAssertion

    @model_validator(mode="after")
    def normalize_bundle(self) -> EvidenceBundle:
        expected_decision = {
            ValidationStatus.PASSED: ValidationDecision.REINDEX,
            ValidationStatus.VIOLATIONS: ValidationDecision.FIX_GRAPH_CODE_BEFORE_REINDEX,
            ValidationStatus.INCOMPLETE: ValidationDecision.INSTRUMENT_BEFORE_REINDEX,
            ValidationStatus.UNREACHABLE: ValidationDecision.FIX_GRAPH_CODE_BEFORE_REINDEX,
            ValidationStatus.FAILED: ValidationDecision.FIX_GRAPH_CODE_BEFORE_REINDEX,
        }[self.status]
        if self.evidence_bundle_id not in (None, self.context.evidence_bundle_id):
            raise ValueError("bundle identity must match context")
        if self.exit_code != exit_code_for(self.status) or self.decision != expected_decision:
            raise ValueError("status, exit code, and decision are inconsistent")
        if len({item.scope for item in self.coverage}) != len(self.coverage):
            raise ValueError("coverage scopes must be unique")
        object.__setattr__(self, "evidence_bundle_id", self.context.evidence_bundle_id)
        object.__setattr__(self, "audit", tuple(sorted(self.audit, key=lambda item: item.rule_id)))
        object.__setattr__(self, "smoke", tuple(sorted(self.smoke, key=lambda item: item.case_id)))
        object.__setattr__(
            self, "coverage", tuple(sorted(self.coverage, key=lambda item: item.scope))
        )
        return self


class PolicyResult(ValidationModel):
    status: ValidationStatus
    decision: ValidationDecision
    exit_code: int
    decision_basis: STRINGS


_SECRET_KEY = re.compile(
    r"(?:^|_)(?:password|passphrase|secret|token|api[_-]?key|authorization|credential|private[_-]?key|connection(?:_string)?|uri|url)(?:$|_)",
    re.IGNORECASE,
)
_SECRET_VALUE = re.compile(
    r"(?:bearer|basic)\s+|(?:password|secret|token|api[_-]?key)\s*[:=]|(?:bolt|neo4j(?:\+s|\+ssc)?|https?)://[^\s/@]+:[^\s/@]+@",
    re.IGNORECASE,
)


def safe_properties(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): item
        for key, raw in value.items()
        if not _SECRET_KEY.search(str(key)) and (item := _safe_value(raw)) is not None
    }


def _safe_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return safe_properties(value)
    if isinstance(value, (list, tuple)):
        return tuple(item for raw in value if (item := _safe_value(raw)) is not None)
    if isinstance(value, str):
        return None if _SECRET_VALUE.search(value) else value[:200]
    return value


def _fingerprint(value: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        safe_properties(value), sort_keys=True, separators=(",", ":"), default=str
    )
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def fingerprint_configuration(value: Mapping[str, Any]) -> str:
    """Return a reproducible fingerprint of effective non-secret configuration."""
    return _fingerprint(value)


def fingerprint_request(value: Mapping[str, Any]) -> str:
    """Return a reproducible fingerprint of a request without secret fields."""
    return _fingerprint(value)


def exit_code_for(status: ValidationStatus) -> int:
    return {
        ValidationStatus.PASSED: 0,
        ValidationStatus.VIOLATIONS: 10,
        ValidationStatus.INCOMPLETE: 11,
        ValidationStatus.UNREACHABLE: 12,
        ValidationStatus.FAILED: 13,
    }[status]


def _policy(status: ValidationStatus, decision: ValidationDecision, basis: str) -> PolicyResult:
    return PolicyResult(
        status=status, decision=decision, exit_code=exit_code_for(status), decision_basis=(basis,)
    )


_REQUIRED_COVERAGE = {"chunks", "mentions", "related_occurrences"}
_UNCERTAIN_RULES = {
    AuditRuleOutcome.UNKNOWN,
    AuditRuleOutcome.INCOMPLETE,
    AuditRuleOutcome.UNSUPPORTED,
}
_UNCERTAIN_SMOKE = {
    SmokeOutcome.UNKNOWN,
    SmokeOutcome.INCOMPLETE,
    SmokeOutcome.UNREACHABLE,
    SmokeOutcome.ERROR,
}


def evaluate_policy(bundle: EvidenceBundle) -> PolicyResult:
    """Evaluate the complete evidence bundle without side effects or authorization."""
    if bundle.status == ValidationStatus.UNREACHABLE:
        return _policy(
            ValidationStatus.UNREACHABLE,
            ValidationDecision.FIX_GRAPH_CODE_BEFORE_REINDEX,
            "target_unreachable",
        )
    if bundle.status == ValidationStatus.FAILED:
        return _policy(
            ValidationStatus.FAILED,
            ValidationDecision.FIX_GRAPH_CODE_BEFORE_REINDEX,
            "protocol_failed",
        )
    if bundle.status == ValidationStatus.VIOLATIONS:
        return _policy(
            ValidationStatus.VIOLATIONS,
            ValidationDecision.FIX_GRAPH_CODE_BEFORE_REINDEX,
            "blocking_findings_present",
        )
    if (
        bundle.read_only_assertion.graph_writes_attempted
        or bundle.read_only_assertion.forbidden_operations
    ):
        return _policy(
            ValidationStatus.VIOLATIONS,
            ValidationDecision.FIX_GRAPH_CODE_BEFORE_REINDEX,
            "read_only_boundary_breached",
        )
    if not bundle.audit:
        return _policy(
            ValidationStatus.INCOMPLETE,
            ValidationDecision.INSTRUMENT_BEFORE_REINDEX,
            "audit_evidence_missing",
        )
    rules = bundle.audit
    if any(rule.mandatory and rule.outcome == AuditRuleOutcome.UNREACHABLE for rule in rules):
        return _policy(
            ValidationStatus.UNREACHABLE,
            ValidationDecision.FIX_GRAPH_CODE_BEFORE_REINDEX,
            "mandatory_audit_unreachable",
        )
    if any(rule.mandatory and rule.outcome == AuditRuleOutcome.FAIL for rule in rules):
        return _policy(
            ValidationStatus.VIOLATIONS,
            ValidationDecision.FIX_GRAPH_CODE_BEFORE_REINDEX,
            "mandatory_rule_failed",
        )
    if any(rule.mandatory and rule.outcome in _UNCERTAIN_RULES for rule in rules):
        return _policy(
            ValidationStatus.INCOMPLETE,
            ValidationDecision.INSTRUMENT_BEFORE_REINDEX,
            "audit_evidence_incomplete",
        )
    warning_records = {item.rule_id: item for item in bundle.warnings}
    warning_ids = {rule.rule_id for rule in rules if rule.outcome == AuditRuleOutcome.WARNING}
    if any(item.structural_impact for item in bundle.warnings):
        return _policy(
            ValidationStatus.VIOLATIONS,
            ValidationDecision.FIX_GRAPH_CODE_BEFORE_REINDEX,
            "structural_warning_unresolved",
        )
    if warning_ids - warning_records.keys() or any(not item.accepted for item in bundle.warnings):
        return _policy(
            ValidationStatus.INCOMPLETE,
            ValidationDecision.INSTRUMENT_BEFORE_REINDEX,
            "warning_acceptance_missing",
        )
    coverage_scopes = {item.scope for item in bundle.coverage}
    if _REQUIRED_COVERAGE - coverage_scopes:
        return _policy(
            ValidationStatus.INCOMPLETE,
            ValidationDecision.INSTRUMENT_BEFORE_REINDEX,
            "coverage_evidence_missing",
        )
    for item in bundle.coverage:
        if item.applicable and item.outcome in _UNCERTAIN_RULES:
            return _policy(
                ValidationStatus.INCOMPLETE,
                ValidationDecision.INSTRUMENT_BEFORE_REINDEX,
                "coverage_evidence_incomplete",
            )
        if item.applicable and (item.outcome != AuditRuleOutcome.PASS or item.percentage < 100):
            return _policy(
                ValidationStatus.VIOLATIONS,
                ValidationDecision.FIX_GRAPH_CODE_BEFORE_REINDEX,
                "provenance_coverage_below_100_percent",
            )
    book_id = bundle.context.book_scope.book_id
    if any(chunk.book_id != book_id for result in bundle.smoke for chunk in result.matched_chunks):
        return _policy(
            ValidationStatus.VIOLATIONS,
            ValidationDecision.FIX_GRAPH_CODE_BEFORE_REINDEX,
            "cross_book_result",
        )
    if any(result.status == SmokeOutcome.FAIL for result in bundle.smoke):
        return _policy(
            ValidationStatus.VIOLATIONS,
            ValidationDecision.FIX_GRAPH_CODE_BEFORE_REINDEX,
            "smoke_assertion_failed",
        )
    if not bundle.smoke or any(
        result.status in _UNCERTAIN_SMOKE or result.status == SmokeOutcome.WARNING
        for result in bundle.smoke
    ):
        return _policy(
            ValidationStatus.INCOMPLETE,
            ValidationDecision.INSTRUMENT_BEFORE_REINDEX,
            "smoke_evidence_incomplete",
        )
    return _policy(ValidationStatus.PASSED, ValidationDecision.REINDEX, "strict_gate_passed")
