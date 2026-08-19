"""Strict, immutable and secret-safe graph-audit contracts."""
from __future__ import annotations  # noqa: I001
import hashlib
import json
import re
import unicodedata
from datetime import UTC, datetime
from enum import StrEnum
from collections.abc import Mapping, Sequence
from typing import Any, Literal
from urllib.parse import urlsplit
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
class AuditModel(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")
class OverallState(StrEnum):
    PASSED = "passed"
    VIOLATIONS = "violations"
    INCOMPLETE = "incomplete"
    UNREACHABLE = "unreachable"
    FAILED = "failed"
class QueryState(StrEnum):
    EVALUATED = "evaluated"
    NOT_APPLICABLE = "not_applicable"
    UNSUPPORTED = "unsupported"
    UNREACHABLE = "unreachable"
    FAILED = "failed"
class Severity(StrEnum):
    BLOCKING = "blocking"
    WARNING = "warning"
    INCOMPLETE = "incomplete"
RULE_CATALOG = tuple(sorted("DUPLICATE_ENTITY_LOGICAL DUPLICATE_RELATIONSHIP_LOGICAL ENDPOINT_HIERARCHY_INVALID ENDPOINT_MENTIONS_INVALID ENDPOINT_RELATED_INVALID ENTITY_ISOLATED_RELATED ENTITY_UNMENTIONED HIERARCHY_CHAPTER_BOOK_PARENT HIERARCHY_CHUNK_MULTIPLE_PARENT HIERARCHY_CHUNK_PARENT_REQUIRED HIERARCHY_LEVEL_CONTRADICTION HIERARCHY_SECTION_PARENT PAGE_CHAPTER_INVALID_START PAGE_CHUNK_INVALID_RANGE PAGE_SECTION_INVALID_START PROVENANCE_CHUNK_MISSING PROVENANCE_ENTITY_MISSING PROVENANCE_MENTIONS_MISSING PROVENANCE_RELATIONSHIP_MISSING".split()))  # noqa: E501,SIM905
def normalize_key(value: str | None) -> str:
    value = "" if value is None else unicodedata.normalize("NFKC", value)
    return " ".join(value.casefold().split()) or "<empty>"
def duplicate_group_id(namespace: str, key: str) -> str:
    return f"{namespace}-duplicate:{hashlib.sha256(normalize_key(key).encode()).hexdigest()}"
_SECRET_KEY = re.compile(r"pass(word)?|secret|token|authorization|credential|uri|source|text|content|body|exception|error", re.I)  # noqa: E501
_SECRET_VALUE = re.compile(
    r"(?:bearer\s+|basic\s+|(?:pass(word)?|secret|token|api[_-]?key)\s*[:=]|(?:bolt|neo4j(?:\+s|\+ssc)?|https?)://)", re.I  # noqa: E501
)
_MAX_PROPERTY_DEPTH, _MAX_PROPERTY_ITEMS, _MAX_PROPERTY_STRING, _DROP = 4, 20, 200, object()
def _safe_property_value(value: Any, depth: int) -> Any:
    container = isinstance(value, (Mapping, Sequence)) and not isinstance(value, (str, bytes, bytearray))  # noqa: E501
    if depth >= _MAX_PROPERTY_DEPTH and container:
        return "<truncated>"
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, nested in list(value.items())[:_MAX_PROPERTY_ITEMS]:
            name = str(key)
            if _SECRET_KEY.search(name):
                continue
            sanitized = _safe_property_value(nested, depth + 1)
            if sanitized is not _DROP:
                result[name] = sanitized
        return result
    if container:
        return tuple(
            sanitized for item in value[:_MAX_PROPERTY_ITEMS]
            if (sanitized := _safe_property_value(item, depth + 1)) is not _DROP
        )
    if isinstance(value, str):
        return _DROP if _SECRET_VALUE.search(value) else value[:_MAX_PROPERTY_STRING]
    return value
def safe_properties(properties: dict[str, Any]) -> dict[str, Any]:
    sanitized = _safe_property_value(properties, 0)
    return sanitized if isinstance(sanitized, dict) else {}
class AuditTarget(AuditModel):
    selector: Literal["bookgraph-neo4j"] = Field(description="Fixed approved project selector")
    database: str = Field(description="Safe configured database identifier")
    scheme: Literal["bolt", "neo4j", "neo4j+s", "neo4j+ssc"] = Field(description="Safe endpoint scheme")  # noqa: E501
    host: str = Field(description="Sanitized endpoint host")
    port: int | None = Field(default=None, description="Sanitized endpoint port")
    uri: str = Field(exclude=True, repr=False, description="Internal validated URI")
    @model_validator(mode="after")
    def validate_target(self) -> AuditTarget:
        if not self.database or not re.fullmatch(r"[A-Za-z0-9_.-]+", self.database):
            raise ValueError("database must be a safe non-empty identifier")
        parsed = urlsplit(self.uri)
        if parsed.scheme != self.scheme or parsed.hostname != self.host or not parsed.hostname:
            raise ValueError("configured URI does not match target")
        if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path:
            raise ValueError("userinfo, query, fragment, and path are not allowed")
        try:
            parsed_port = parsed.port
        except ValueError as exc:
            raise ValueError("configured URI has an invalid port") from exc
        if self.port is not None and self.port != parsed_port:
            raise ValueError("configured URI does not match port")
        return self
class RuntimeMetadata(AuditModel):
    neo4j_version: str | None = Field(default=None, description="Observed Neo4j version")
    edition: str | None = Field(default=None, description="Observed Neo4j edition")
    repository_revision: str | None = Field(default=None, description="Injected repository revision")  # noqa: E501
class InventoryMetric(AuditModel):
    value: int | None = Field(description="Complete total, including evaluated zero")
    state: Literal["evaluated", "unevaluated", "unsupported", "failed"] = Field(description="Evaluation state")  # noqa: E501
    @model_validator(mode="after")
    def validate_value_state(self) -> InventoryMetric:
        if (self.state == "evaluated") != (self.value is not None):
            raise ValueError("only evaluated metrics may carry a value")
        return self
class FindingSample(AuditModel):
    key: str = Field(description="Stable sample ordering key")
    subject_ids: tuple[str, ...] = Field(default=(), description="Stable affected identifiers")
    classification: str | None = Field(default=None, description="Provenance classification")
    properties: dict[str, Any] = Field(default_factory=dict, description="Bounded safe properties")
    group_id: str | None = Field(default=None, description="Stable logical duplicate group ID")
    native_edge_count: int | None = Field(default=None, description="Exact native edge count")
    @model_validator(mode="before")
    @classmethod
    def sanitize(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = dict(data)
            data["properties"] = safe_properties(data.get("properties", {}))
        return data
FindingCategory = Literal["hierarchy", "endpoints", "provenance", "duplicates", "pages", "coverage"]
def _sample_sort_key(sample: Any) -> tuple[Any, ...]:
    get = sample.get if isinstance(sample, dict) else lambda name, default=None: getattr(sample, name, default)  # noqa: E501
    return (get("key", ""), get("classification") or "", tuple(get("subject_ids", ())), json.dumps(get("properties", {}), sort_keys=True, default=str))  # noqa: E501
class AuditFinding(AuditModel):
    rule_id: str = Field(description="Versioned stable rule identifier")
    category: FindingCategory = Field(description="Stable finding category")
    severity: Severity = Field(description="Finding classification")
    total: int = Field(ge=0, description="Complete finding total")
    samples: tuple[FindingSample, ...] = Field(default=(), description="Bounded ordered sample")
    sample_limit: int = Field(default=50, ge=0, description="Maximum sample size")
    sample_truncated: bool = Field(default=False, description="Whether sample is bounded")
    query_state: QueryState = Field(default=QueryState.EVALUATED, description="Rule query state")
    @field_validator("rule_id")
    @classmethod
    def known_rule(cls, value: str) -> str:
        if value not in RULE_CATALOG:
            raise ValueError("unknown audit rule")
        return value
    @model_validator(mode="before")
    @classmethod
    def bound_and_order(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = dict(data)
            limit = data.get("sample_limit", 50)
            samples = tuple(sorted(data.get("samples", ()), key=_sample_sort_key))
            data["samples"] = samples[:limit]
            data["sample_truncated"] = data.get("total", 0) > limit
        return data
class AuditQueryExecution(AuditModel):
    name: str = Field(description="Named static audit operation")
    state: QueryState = Field(description="Operation execution state")
    error: str | None = Field(default=None, description="Redacted failure reason")
    @field_validator("error")
    @classmethod
    def redact_error(cls, value: str | None) -> str | None:
        return None if value is None else "audit operation failed"
class AuditSummary(AuditModel):
    blocking_total: int = Field(default=0, description="Total blocking findings")
    warning_total: int = Field(default=0, description="Total warning findings")
    incomplete_total: int = Field(default=0, description="Total incomplete findings")
    evaluated_rule_count: int = Field(default=0, description="Evaluated rule count")
def _summary(findings: tuple[AuditFinding, ...]) -> AuditSummary:
    return AuditSummary(
        blocking_total=sum(f.total for f in findings if f.severity == Severity.BLOCKING),
        warning_total=sum(f.total for f in findings if f.severity == Severity.WARNING),
        incomplete_total=sum(f.total for f in findings if f.severity == Severity.INCOMPLETE),
        evaluated_rule_count=sum(f.query_state == QueryState.EVALUATED for f in findings),
    )
class AuditSnapshot(AuditModel):
    inventory: dict[str, InventoryMetric] = Field(default_factory=dict, description="Complete inventory totals")  # noqa: E501  # noqa: E501
    findings: tuple[AuditFinding, ...] = Field(default=(), description="Ordered rule findings")
    queries: tuple[AuditQueryExecution, ...] = Field(default=(), description="Query executions")
    runtime: RuntimeMetadata = Field(default_factory=RuntimeMetadata, description="Observed runtime metadata")  # noqa: E501
    provenance_incomplete: bool = Field(default=False, description="Unknown or legacy provenance")
    failure_state: OverallState | None = Field(default=None, description="Transport or audit failure state")  # noqa: E501
    executed_at: datetime = Field(default_factory=lambda: datetime.now(UTC), description="UTC evidence time")  # noqa: E501
    @field_validator("failure_state")
    @classmethod
    def failure_only(cls, value: OverallState | None) -> OverallState | None:
        if value not in (None, OverallState.UNREACHABLE, OverallState.FAILED):
            raise ValueError("failure_state must be unreachable or failed")
        return value
class AuditExecution(AuditModel):
    state: OverallState = Field(description="Final overall state")
    exit_code: int = Field(description="Stable process exit code")
    queries: tuple[AuditQueryExecution, ...] = Field(default=(), description="Ordered query results")  # noqa: E501
class AuditReport(AuditModel):
    report_schema_version: Literal["1.0"] = Field(default="1.0", description="Report schema version")  # noqa: E501
    audit_query_version: str = Field(default="p1-static-v1", description="Stable audit query version")  # noqa: E501
    target: AuditTarget = Field(description="Secret-safe target projection")
    state: OverallState = Field(description="Final overall state")
    inventory: dict[str, InventoryMetric] = Field(default_factory=dict, description="Complete totals")  # noqa: E501
    findings: tuple[AuditFinding, ...] = Field(default=(), description="Stable ordered findings")
    summary: AuditSummary = Field(default_factory=AuditSummary, description="Finding summary")
    runtime: RuntimeMetadata = Field(default_factory=RuntimeMetadata, description="Observed runtime metadata")  # noqa: E501
    executed_at: datetime = Field(default_factory=lambda: datetime.now(UTC), description="UTC execution time")  # noqa: E501
    execution: AuditExecution | None = Field(default=None, description="Detailed execution state")
    @model_validator(mode="after")
    def stable_report(self) -> AuditReport:
        ordered = tuple(sorted(self.findings, key=lambda finding: finding.rule_id))
        object.__setattr__(self, "findings", ordered)
        if self.summary == AuditSummary() and ordered:
            object.__setattr__(self, "summary", _summary(ordered))
        return self
    def canonical_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
def exit_code(state: OverallState) -> int:
    return {
        OverallState.PASSED: 0,
        OverallState.VIOLATIONS: 10,
        OverallState.INCOMPLETE: 11,
        OverallState.UNREACHABLE: 12,
        OverallState.FAILED: 13,
    }[state]
