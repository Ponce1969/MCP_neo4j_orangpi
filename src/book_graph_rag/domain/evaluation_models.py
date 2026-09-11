"""Domain models for resolution evaluation metrics and baseline report."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EvaluationGate(BaseModel):
    """Verdict from comparing a hybrid run against the slug+token baseline."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    beats_baseline_f1: bool
    hard_over_merge_zero: bool
    multilingual_under_merge_ok: bool
    passed: bool


class ThresholdSweepResult(BaseModel):
    """One threshold pair that satisfies the dataset-gated acceptance gates."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    high_cosine: float
    medium_cosine: float
    metrics: EvaluationMetrics
    gate: EvaluationGate


class EvaluationMetrics(BaseModel):
    """Metrics produced by a model evaluation run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str
    input_variant: Literal["A", "B"]
    retrieval_precision: float
    retrieval_recall: float
    retrieval_f1: float
    multilingual_under_merge_rate: float
    multilingual_f1: float
    hard_over_merge_rate: float
    auto_merge_count: int
    auto_merge_correct: int
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class BaselineReport(BaseModel):
    """Committed slug+token resolver baseline."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = "1.0.0"
    resolver: Literal["slug_token"] = "slug_token"
    retrieval_precision: float
    retrieval_recall: float
    retrieval_f1: float
    multilingual_under_merge_rate: float
    multilingual_f1: float
    hard_over_merge_rate: float
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    dataset_manifest_sha256: str


# ── Phase 5 evaluation models ────────────────────────────────────────────────


class LayerStatus(StrEnum):
    """Closed status set for evaluation layers and overall reports."""

    PASSED = "passed"
    FAILED = "failed"
    INCOMPLETE = "incomplete"
    PENDING = "pending"
    NOT_EVALUATED = "not_evaluated"
    NOT_APPLICABLE = "not_applicable"
    UNREACHABLE = "unreachable"
    WARNING = "warning"


class LayerMetricValue(BaseModel):
    """One measured project-owned metric with its threshold and comparator."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    name: str
    value: float
    threshold: float | None = None
    comparator: Literal[">=", "<=", "=="] = ">="


ClaimVerdictLabel = Literal["support", "contradict", "not_verifiable"]


class AtomicClaim(BaseModel):
    """One atomic claim extracted from a generated answer (Claimify-style)."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    claim_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    evidence_refs: tuple[str, ...] = ()
    verdict: ClaimVerdictLabel | None = None
    verdict_rationale: str = ""
    verifier_model_id: str = ""


class ClaimValidationResult(BaseModel):
    """Per-question claim validation output."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    question_id: str
    claims: tuple[AtomicClaim, ...]
    faithfulness: float = Field(ge=0.0, le=1.0)
    extractor_model_id: str
    verifier_model_id: str


PairwiseVerdictLabel = Literal["graph_wins", "tie", "baseline_wins"]


class PairwiseJudgment(BaseModel):
    """Pairwise comparison between graph answer and vector baseline answer."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    question_id: str
    verdict: PairwiseVerdictLabel
    rationale: str = ""
    judge_model_id: str


class RAGASSecondaryMetrics(BaseModel):
    """RAGAS secondary metrics; failure/unavailability is a warning only."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    faithfulness: float | None = None
    answer_relevancy: float | None = None
    context_precision: float | None = None
    previous_faithfulness: float | None = None
    drop_warning: bool = False
    available: bool = False
    notes: str = ""


# ── Evaluation dataset exceptions ─────────────────────────────────────────────


class DatasetLoadError(Exception):
    """Raised when a dataset cannot be loaded or is malformed."""


class UnknownDatasetError(LookupError):
    """Raised when a requested dataset id is absent from the manifest."""

    def __init__(self, dataset_id: str) -> None:
        super().__init__(f"Unknown dataset: {dataset_id!r}")
        self.dataset_id = dataset_id


class DatasetManifestMismatch(Exception):  # noqa: N818
    """Raised when a dataset file sha256 does not match the manifest."""

    def __init__(self, dataset_id: str, expected: str, actual: str) -> None:
        super().__init__(
            f"{dataset_id}: hash mismatch: expected {expected}, got {actual}"
        )
        self.dataset_id = dataset_id
        self.expected = expected
        self.actual = actual


# ── Baseline report (mechanism-first per R9) ──────────────────────────────────


class EvaluationBaselineReport(BaseModel):
    """Committed layer baseline + threshold source (R4.3, R5.4, R9.1).

    A required layer's readiness gate reads ``thresholds_finalized``:
    while false, the gate returns INCOMPLETE (11) even if metrics pass (R9.2).
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    schema_version: str = "1.0.0"
    layer: Literal["resolution", "retrieval", "generation"]
    dataset_id: str
    dataset_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    committed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    code_commit: str = ""
    model_ids: tuple[str, ...] = ()
    metrics: dict[str, float] = Field(default_factory=dict)
    thresholds_finalized: bool = False
    f1_min: float | None = None
    hard_over_merge_max: float | None = None
    faithfulness_min: float | None = None
    pairwise_win_rate_min: float | None = None

    @model_validator(mode="after")
    def _threshold_consistency(self) -> EvaluationBaselineReport:
        if self.thresholds_finalized:
            if self.layer == "resolution" and (
                self.f1_min is None or self.hard_over_merge_max is None
            ):
                raise ValueError(
                    "resolution baseline requires f1_min + hard_over_merge_max when finalized"
                )
            if self.layer == "generation" and self.faithfulness_min is None:
                raise ValueError(
                    "generation baseline requires faithfulness_min when finalized"
                )
        return self


# ── Run metadata and per-layer result (R2.1) ──────────────────────────────────


class LayerRunMetadata(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    run_id: str
    code_commit: str
    evaluated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    model_ids: tuple[str, ...] = ()


class EvaluationLayerResult(BaseModel):
    """Per-layer outcome (R2.1, R2.2)."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    schema_version: str = "1.0.0"
    layer: Literal["structure", "extraction", "resolution", "retrieval", "generation"]
    status: LayerStatus
    project_owned_metrics: tuple[LayerMetricValue, ...]
    ragas_secondary: RAGASSecondaryMetrics | None = None
    warnings: tuple[str, ...] = ()
    rationale: str = ""
    source_dataset_id: str
    baseline_report_path: str | None = None
    run_metadata: LayerRunMetadata


# ── Top-level report (R2.1, R4) ──────────────────────────────────────────────


class EvaluationReport(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    schema_version: str = "1.0.0"
    overall_status: LayerStatus
    layer_results: tuple[EvaluationLayerResult, ...]
    run_id: str
    code_commit: str
    evaluated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    scope: str | None = None
    rationale: str = ""

    @staticmethod
    def derive_overall_status(
        layer_results: tuple[EvaluationLayerResult, ...],
        required_layers: set[str],
    ) -> LayerStatus:
        """Derive overall status from required-layer statuses (R2.2)."""
        required = [r for r in layer_results if r.layer in required_layers]
        if any(r.status == LayerStatus.FAILED for r in required):
            return LayerStatus.FAILED
        if any(
            r.status
            in {
                LayerStatus.INCOMPLETE,
                LayerStatus.PENDING,
                LayerStatus.NOT_EVALUATED,
                LayerStatus.NOT_APPLICABLE,
                LayerStatus.UNREACHABLE,
            }
            for r in required
        ):
            return LayerStatus.INCOMPLETE
        if required and all(r.status == LayerStatus.PASSED for r in required):
            return LayerStatus.PASSED
        if any(r.status == LayerStatus.WARNING for r in layer_results):
            return LayerStatus.WARNING
        return LayerStatus.INCOMPLETE

    def canonical_json(self) -> str:
        import json

        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


# ── Readiness gate policy + result (R7) ───────────────────────────────────────


class RequiredLayer(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    layer: Literal["structure", "extraction", "resolution", "retrieval", "generation"]
    blocking: bool = True


class ReadinessGatePolicy(BaseModel):
    """Versioned readiness gate definition (R7.1, R7.2)."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    name: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9-]*$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    required_layers: list[RequiredLayer]
    optional_layers: list[RequiredLayer] = Field(default_factory=list)
    audit_gate_ref: str = "expose-mcp"
    baseline_glob: str = "data/evaluation/*.json"

    @model_validator(mode="after")
    def _no_overlap(self) -> ReadinessGatePolicy:
        req = {r.layer for r in self.required_layers}
        opt = {r.layer for r in self.optional_layers}
        if req & opt:
            raise ValueError("layer cannot be both required and optional")
        return self


class ReadinessLayerStatus(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    layer: str
    status: LayerStatus
    rationale: str
    measured: tuple[LayerMetricValue, ...] = ()
    thresholds: tuple[LayerMetricValue, ...] = ()


class ReadinessGateResult(BaseModel):
    """Composite readiness result (R7.1, R7.4)."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    schema_version: str = "1.0.0"
    gate_name: str
    gate_version: str
    scope: str | None
    passed: bool
    overall_state: Literal[
        "passed", "violations", "incomplete", "unreachable", "failed"
    ]
    exit_code: int
    rationale: str
    audit_gate_status: str
    evaluation_status: str
    layer_breakdown: tuple[ReadinessLayerStatus, ...]
    run_metadata: LayerRunMetadata
    warnings: tuple[str, ...] = ()

    def canonical_json(self) -> str:
        import json

        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


# ── Dataset manifest models (R1.1, R1.2) ──────────────────────────────────────


class EvaluationDatasetRecord(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    id: str
    schema_version: str = "1.0.0"
    file: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    provenance: str
    model_id: str = ""
    record_count: int = Field(default=0, ge=0)


class EvaluationDatasetManifest(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    schema_version: str = "1.0.0"
    datasets: tuple[EvaluationDatasetRecord, ...]


class EvaluationDataset(BaseModel):
    """Loaded evaluation dataset: validated records plus provenance."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    dataset_id: str
    records: tuple[dict[str, Any], ...]
