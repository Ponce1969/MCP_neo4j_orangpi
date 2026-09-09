"""Readiness gate policy models and evaluation contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import Field, model_validator

from book_graph_rag.domain.audit_models import AuditModel, OverallState

GateDimension = Literal["hierarchy", "endpoints", "provenance", "uniqueness", "coverage"]
MaxSeverity = Literal["blocking", "warning", "incomplete", "none"]

DIMENSION_FROM_CATEGORY: dict[str, GateDimension] = {
    "hierarchy": "hierarchy",
    "endpoints": "endpoints",
    "provenance": "provenance",
    "duplicates": "uniqueness",
    "pages": "hierarchy",
    "coverage": "coverage",
}


class UnknownGateError(LookupError):
    """Raised when a requested gate is not defined in the policy."""

    def __init__(self, name: str) -> None:
        super().__init__(f"Unknown gate: {name!r}")
        self.name = name


class DimensionRequirement(AuditModel):
    """A single dimension requirement (only ``pass`` is supported in v1)."""

    dimension: GateDimension
    requirement: Literal["pass"] = "pass"


class ReadinessGate(AuditModel):
    """A named, versioned readiness gate over a subset of audit dimensions."""

    name: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9-]*$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    required_dimensions: dict[GateDimension, Literal["pass"]]
    max_severity: MaxSeverity

    @model_validator(mode="after")
    def _require_at_least_one_dimension(self) -> ReadinessGate:
        if not self.required_dimensions:
            raise ValueError("required_dimensions cannot be empty")
        return self


class GatePolicy(AuditModel):
    """Versioned catalog of readiness gates loaded from ``gates.yaml``."""

    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    gates: tuple[ReadinessGate, ...]

    @model_validator(mode="after")
    def _unique_gate_names(self) -> GatePolicy:
        names = [g.name for g in self.gates]
        if len(set(names)) != len(names):
            raise ValueError("duplicate gate names")
        return self


class GateDimensionStatus(AuditModel):
    """Per-dimension pass/fail status inside a gate result."""

    dimension: GateDimension
    finding_total: int = Field(ge=0)
    satisfied: bool


class GateResult(AuditModel):
    """Deterministic outcome of evaluating a readiness gate over an audit report."""

    gate_name: str
    gate_version: str
    scope: str | None
    passed: bool
    overall_state: OverallState
    exit_code: int
    rationale: str
    dimension_breakdown: tuple[GateDimensionStatus, ...]
    blocking_findings: tuple[str, ...]
    evaluated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def canonical_json(self) -> str:
        import json

        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
