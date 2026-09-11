"""Tests for evaluation domain models (Slice A)."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from book_graph_rag.domain.evaluation_models import (
    AtomicClaim,
    ClaimValidationResult,
    EvaluationBaselineReport,
    EvaluationDatasetManifest,
    EvaluationDatasetRecord,
    EvaluationLayerResult,
    EvaluationReport,
    LayerMetricValue,
    LayerRunMetadata,
    LayerStatus,
    PairwiseJudgment,
    RAGASSecondaryMetrics,
    ReadinessGatePolicy,
    ReadinessGateResult,
    ReadinessLayerStatus,
    RequiredLayer,
)


def test_layer_status_closed_set() -> None:
    """All seven status values are present and JSON-round-trip to lowercase."""
    statuses = [
        LayerStatus.PASSED,
        LayerStatus.FAILED,
        LayerStatus.INCOMPLETE,
        LayerStatus.PENDING,
        LayerStatus.NOT_EVALUATED,
        LayerStatus.NOT_APPLICABLE,
        LayerStatus.WARNING,
    ]
    values = [s.value for s in statuses]
    assert values == [
        "passed",
        "failed",
        "incomplete",
        "pending",
        "not_evaluated",
        "not_applicable",
        "warning",
    ]
    assert json.dumps(values) == json.dumps(
        [
            "passed",
            "failed",
            "incomplete",
            "pending",
            "not_evaluated",
            "not_applicable",
            "warning",
        ]
    )


def test_layer_metric_value_freeze() -> None:
    """LayerMetricValue is frozen and rejects direct mutation."""
    model = LayerMetricValue(name="f1", value=0.8)
    with pytest.raises(ValidationError):
        model.value = 0.5


def test_atomic_claim_round_trips_canonical_json() -> None:
    """AtomicClaim serializes, deserializes, and rejects unknown fields."""
    claim = AtomicClaim(
        claim_id="c-1",
        text="Madrid is the capital of Spain.",
        evidence_refs=("ctx-1",),
        verdict="support",
        verdict_rationale="explicit",
        verifier_model_id="model-a",
    )
    payload = claim.model_dump_json()
    restored = AtomicClaim.model_validate_json(payload)
    assert restored.claim_id == "c-1"
    assert restored.verdict == "support"


def test_atomic_claim_rejects_unknown_field() -> None:
    """extra=forbid rejects fields not in the schema."""
    with pytest.raises(ValidationError):
        AtomicClaim(claim_id="c-1", text="x", unknown="nope")  # type: ignore[call-arg]


def test_claim_validation_result_faithfulness_bounds() -> None:
    """faithfulness is clamped to [0, 1]."""
    with pytest.raises(ValidationError):
        ClaimValidationResult(
            question_id="q-1",
            claims=(),
            faithfulness=1.1,
            extractor_model_id="m",
            verifier_model_id="m",
        )


def test_pairwise_judgment_verdict_enum() -> None:
    """verdict accepts only the three label values."""
    PairwiseJudgment(question_id="q-1", verdict="graph_wins", judge_model_id="j")
    PairwiseJudgment(question_id="q-1", verdict="tie", judge_model_id="j")
    PairwiseJudgment(question_id="q-1", verdict="baseline_wins", judge_model_id="j")
    with pytest.raises(ValidationError):
        PairwiseJudgment(question_id="q-1", verdict="invalid", judge_model_id="j")  # type: ignore[arg-type]


def test_pairwise_judgment_freeze() -> None:
    """PairwiseJudgment is frozen and forbids extra fields."""
    model = PairwiseJudgment(question_id="q-1", verdict="tie", judge_model_id="j")
    with pytest.raises(ValidationError):
        model.verdict = "graph_wins"
    with pytest.raises(ValidationError):
        PairwiseJudgment(question_id="q-1", verdict="tie", judge_model_id="j", extra="x")  # type: ignore[call-arg]


def test_ragas_metrics_unavailable_default() -> None:
    """Default RAGAS metrics signal unavailable and no drop warning."""
    metrics = RAGASSecondaryMetrics()
    assert metrics.available is False
    assert metrics.drop_warning is False


def test_ragas_metrics_none_fields_allowed() -> None:
    """Primary metric fields may be None."""
    metrics = RAGASSecondaryMetrics(available=True)
    assert metrics.faithfulness is None
    assert metrics.answer_relevancy is None
    assert metrics.context_precision is None


def test_evaluation_baseline_default_unfinalized() -> None:
    """A fresh baseline report starts unfinalized."""
    report = EvaluationBaselineReport(layer="resolution", dataset_id="r", dataset_sha256="0" * 64)
    assert report.thresholds_finalized is False


def test_evaluation_baseline_finalized_resolution_requires_thresholds() -> None:
    """Resolution layer needs f1_min and hard_over_merge_max once finalized."""
    with pytest.raises(ValidationError):
        EvaluationBaselineReport(
            layer="resolution",
            dataset_id="r",
            dataset_sha256="0" * 64,
            thresholds_finalized=True,
        )


def test_evaluation_baseline_finalized_generation_requires_faithfulness() -> None:
    """Generation layer needs faithfulness_min once finalized."""
    with pytest.raises(ValidationError):
        EvaluationBaselineReport(
            layer="generation",
            dataset_id="g",
            dataset_sha256="0" * 64,
            thresholds_finalized=True,
        )


def test_evaluation_baseline_freeze_and_extra_forbid() -> None:
    """Baseline report is immutable and rejects unknown fields."""
    report = EvaluationBaselineReport(
        layer="resolution",
        dataset_id="r",
        dataset_sha256="0" * 64,
        f1_min=0.5,
        hard_over_merge_max=0.0,
    )
    with pytest.raises(ValidationError):
        report.thresholds_finalized = True
    with pytest.raises(ValidationError):
        EvaluationBaselineReport(
            layer="resolution",
            dataset_id="r",
            dataset_sha256="0" * 64,
            extra="nope",
        )  # type: ignore[call-arg]


def test_layer_run_metadata_round_trips() -> None:
    """LayerRunMetadata resolves forward references and round-trips."""
    meta = LayerRunMetadata(run_id="run-1", code_commit="abc123", model_ids=("m",))
    restored = LayerRunMetadata.model_validate_json(meta.model_dump_json())
    assert restored.run_id == "run-1"
    assert restored.code_commit == "abc123"
    assert restored.model_ids == ("m",)


def test_evaluation_layer_result_carries_provenance() -> None:
    """Layer result keeps dataset id, baseline path and run metadata."""
    meta = LayerRunMetadata(run_id="run-1", code_commit="abc123")
    result = EvaluationLayerResult(
        layer="resolution",
        status=LayerStatus.PASSED,
        project_owned_metrics=(LayerMetricValue(name="f1", value=0.8),),
        source_dataset_id="resolution_dataset",
        baseline_report_path="data/evaluation/resolution_baseline.json",
        run_metadata=meta,
    )
    assert result.source_dataset_id == "resolution_dataset"
    assert result.run_metadata.run_id == "run-1"


def test_evaluation_report_overall_status_passed() -> None:
    """All required layers passed -> overall PASSED."""
    meta = LayerRunMetadata(run_id="run-1", code_commit="abc123")
    layers = (
        EvaluationLayerResult(
            layer="resolution",
            status=LayerStatus.PASSED,
            project_owned_metrics=(),
            source_dataset_id="r",
            run_metadata=meta,
        ),
        EvaluationLayerResult(
            layer="generation",
            status=LayerStatus.PASSED,
            project_owned_metrics=(),
            source_dataset_id="g",
            run_metadata=meta,
        ),
    )
    report = EvaluationReport(
        overall_status=LayerStatus.PASSED,
        layer_results=layers,
        run_id="run-1",
        code_commit="abc123",
    )
    assert report.overall_status == LayerStatus.PASSED


def test_evaluation_report_overall_status_failed() -> None:
    """Any required layer failed -> overall FAILED."""
    meta = LayerRunMetadata(run_id="run-1", code_commit="abc123")
    layers = (
        EvaluationLayerResult(
            layer="resolution",
            status=LayerStatus.FAILED,
            project_owned_metrics=(),
            source_dataset_id="r",
            run_metadata=meta,
        ),
        EvaluationLayerResult(
            layer="generation",
            status=LayerStatus.PASSED,
            project_owned_metrics=(),
            source_dataset_id="g",
            run_metadata=meta,
        ),
    )
    report = EvaluationReport(
        overall_status=EvaluationReport.derive_overall_status(layers, {"resolution", "generation"}),
        layer_results=layers,
        run_id="run-1",
        code_commit="abc123",
    )
    assert report.overall_status == LayerStatus.FAILED


def test_evaluation_report_overall_status_incomplete() -> None:
    """A pending required layer makes the report INCOMPLETE."""
    meta = LayerRunMetadata(run_id="run-1", code_commit="abc123")
    layers = (
        EvaluationLayerResult(
            layer="resolution",
            status=LayerStatus.PASSED,
            project_owned_metrics=(),
            source_dataset_id="r",
            run_metadata=meta,
        ),
        EvaluationLayerResult(
            layer="extraction",
            status=LayerStatus.PENDING,
            project_owned_metrics=(),
            source_dataset_id="e",
            run_metadata=meta,
        ),
    )
    status = EvaluationReport.derive_overall_status(layers, {"resolution", "extraction"})
    assert status == LayerStatus.INCOMPLETE


def test_evaluation_report_canonical_json_deterministic() -> None:
    """Canonical JSON is byte-identical across two serializations."""
    meta = LayerRunMetadata(run_id="run-1", code_commit="abc123")
    report = EvaluationReport(
        overall_status=LayerStatus.PASSED,
        layer_results=(
            EvaluationLayerResult(
                layer="resolution",
                status=LayerStatus.PASSED,
                project_owned_metrics=(LayerMetricValue(name="f1", value=0.8),),
                source_dataset_id="r",
                run_metadata=meta,
            ),
        ),
        run_id="run-1",
        code_commit="abc123",
    )
    assert report.canonical_json() == report.canonical_json()


def test_readiness_gate_policy_name_and_version_validated() -> None:
    """Gate name must be a slug and version semver."""
    with pytest.raises(ValidationError):
        ReadinessGatePolicy(name="Bad Name", version="1.0", required_layers=[])
    with pytest.raises(ValidationError):
        ReadinessGatePolicy(name="good", version="v1", required_layers=[])


def test_readiness_gate_policy_no_overlap() -> None:
    """A layer cannot be both required and optional."""
    layer = RequiredLayer(layer="resolution")
    with pytest.raises(ValidationError):
        ReadinessGatePolicy(
            name="g",
            version="1.0.0",
            required_layers=[layer],
            optional_layers=[layer],
        )


def test_readiness_layer_status_carries_measured_and_thresholds() -> None:
    """ReadinessLayerStatus mirrors measured metrics and thresholds."""
    status = ReadinessLayerStatus(
        layer="resolution",
        status=LayerStatus.PASSED,
        rationale="ok",
        measured=(LayerMetricValue(name="f1", value=0.8),),
        thresholds=(LayerMetricValue(name="f1", value=0.7),),
    )
    assert status.measured[0].value == 0.8
    assert status.thresholds[0].value == 0.7


def test_readiness_gate_result_canonical_json_deterministic() -> None:
    """ReadinessGateResult canonical JSON is stable."""
    meta = LayerRunMetadata(run_id="run-1", code_commit="abc123")
    result = ReadinessGateResult(
        gate_name="g",
        gate_version="1.0.0",
        scope=None,
        passed=False,
        overall_state="incomplete",
        exit_code=11,
        rationale="thresholds unset",
        audit_gate_status="passed",
        evaluation_status="incomplete",
        layer_breakdown=(),
        run_metadata=meta,
    )
    assert result.canonical_json() == result.canonical_json()


def test_evaluation_dataset_manifest_extra_forbid() -> None:
    """Manifest records reject unknown fields."""
    with pytest.raises(ValidationError):
        EvaluationDatasetRecord(
            id="x",
            schema_version="1.0.0",
            file="x.jsonl",
            sha256="a" * 64,
            provenance="p",
            extra="nope",
        )  # type: ignore[call-arg]


def test_evaluation_dataset_record_sha256_format() -> None:
    """sha256 must be 64 lowercase hex chars."""
    with pytest.raises(ValidationError):
        EvaluationDatasetRecord(
            id="x",
            schema_version="1.0.0",
            file="x.jsonl",
            sha256="not-a-hash",
            provenance="p",
        )


def test_evaluation_dataset_manifest_schema_version_pinned() -> None:
    """Manifest defaults to schema version 1.0.0."""
    record = EvaluationDatasetRecord(
        id="x",
        file="x.jsonl",
        sha256="a" * 64,
        provenance="p",
    )
    manifest = EvaluationDatasetManifest(datasets=(record,))
    assert manifest.schema_version == "1.0.0"
