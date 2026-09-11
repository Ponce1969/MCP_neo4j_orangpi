"""Read-only integration contract for evaluation/readiness adapters (Slice C, T-C.8).

This test proves that:

* every evaluation/readiness graph adapter is constructed against the running
  testcontainers Neo4j URI, never a production endpoint;
* running a full evaluation layer (retrieval) against that container does not
  mutate graph state;
* running a readiness gate through the real retrieval layer also leaves the
  graph unchanged.

No production endpoint is referenced or contacted.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from book_graph_rag.application.audit_graph_use_case import build_audit_target
from book_graph_rag.application.evaluate_extraction_layer_use_case import (
    EvaluateExtractionLayerUseCase,
)
from book_graph_rag.application.evaluate_gate_use_case import GateEvaluatorUseCase
from book_graph_rag.application.evaluate_generation_layer_use_case import (
    EvaluateGenerationLayerUseCase,
)
from book_graph_rag.application.evaluate_resolution_layer_use_case import (
    EvaluateResolutionLayerUseCase,
)
from book_graph_rag.application.evaluate_retrieval_layer_use_case import (
    EvaluateRetrievalLayerUseCase,
)
from book_graph_rag.application.readiness_gate_evaluator_use_case import (
    ReadinessGateEvaluatorUseCase,
)
from book_graph_rag.config import Settings
from book_graph_rag.domain.audit_models import AuditReport, OverallState
from book_graph_rag.domain.evaluation_models import (
    EvaluationLayerResult,
    LayerRunMetadata,
    LayerStatus,
    ReadinessGatePolicy,
    RequiredLayer,
)
from book_graph_rag.domain.gate_models import GatePolicy, ReadinessGate
from book_graph_rag.infrastructure.evaluation_dataset_loader import (
    JsonlManifestEvaluationDatasetLoader,
)
from book_graph_rag.infrastructure.neo4j_retrieval_adapter import Neo4jRetrievalAdapter
from book_graph_rag.infrastructure.stub_ragas_runner import StubRAGASRunner

pytestmark = pytest.mark.neo4j_integration

_PRODUCTION_HOST = "100.106.85.109"


async def _count_nodes(driver: Any) -> int:
    """Return the number of nodes in the graph."""
    async with driver.session() as session:
        result = await session.run("MATCH (n) RETURN count(n) AS c")
        record = await result.single()
        return int(record["c"])


def _build_retrieval_layer(settings: Settings) -> EvaluateRetrievalLayerUseCase:
    """Wire the retrieval layer against ``settings.neo4j_uri`` (testcontainer)."""
    dataset_port = JsonlManifestEvaluationDatasetLoader(settings.evaluation_manifest_path)
    ragas_port = StubRAGASRunner(Path("tests/fixtures/evaluation/ragas.json"))
    retrieval_port = Neo4jRetrievalAdapter(settings)
    return EvaluateRetrievalLayerUseCase(
        dataset_port=dataset_port,
        retrieval_port=retrieval_port,
        ragas_port=ragas_port,
    )


class _FakeLayer:
    """Deterministic stand-in for resolution/generation layers in the readiness path.

    Keeps the integration test focused on the real retrieval adapter while still
    exercising the full readiness composition use case.
    """

    def __init__(self, layer: str, status: LayerStatus) -> None:
        self._layer = layer
        self._status = status

    async def execute(self, **kwargs: Any) -> EvaluationLayerResult:
        """Return a fixed layer result."""
        _ = kwargs
        return EvaluationLayerResult(
            layer=self._layer,  # type: ignore[arg-type]
            status=self._status,
            project_owned_metrics=(),
            rationale=f"{self._layer} fake layer",
            source_dataset_id="",
            baseline_report_path=None,
            run_metadata=LayerRunMetadata(run_id="fake", code_commit=""),
        )


def _build_readiness_use_case(settings: Settings) -> ReadinessGateEvaluatorUseCase:
    """Wire a readiness gate whose only real layer is retrieval (testcontainer)."""
    audit_gate = ReadinessGate(
        name="test-audit",
        version="1.0.0",
        required_dimensions={"coverage": "pass"},
        max_severity="warning",
    )
    readiness_gate = ReadinessGatePolicy(
        name="test-readiness",
        version="1.0.0",
        required_layers=[
            RequiredLayer(layer="retrieval", blocking=False),
        ],
        optional_layers=[
            RequiredLayer(layer="extraction", blocking=False),
        ],
        audit_gate_ref="test-audit",
    )
    policy = GatePolicy(
        version="1.0.0",
        gates=[audit_gate],
        readiness_gates=[readiness_gate],
    )
    return ReadinessGateEvaluatorUseCase(
        gate_policy=policy,
        audit_evaluator=GateEvaluatorUseCase(policy),
        resolution_layer=cast(
            EvaluateResolutionLayerUseCase,
            _FakeLayer("resolution", LayerStatus.PENDING),
        ),
        generation_layer=cast(
            EvaluateGenerationLayerUseCase,
            _FakeLayer("generation", LayerStatus.PENDING),
        ),
        retrieval_layer=_build_retrieval_layer(settings),
        extraction_layer=EvaluateExtractionLayerUseCase(),
    )


@pytest.mark.neo4j_integration
async def test_evaluation_readiness_adapters_use_testcontainers_only(
    neo4j_settings: Settings,
) -> None:
    """Evaluation/readiness graph adapters point at the testcontainer URI only."""
    retrieval_layer = _build_retrieval_layer(neo4j_settings)
    adapter = retrieval_layer._retrieval_port
    assert isinstance(adapter, Neo4jRetrievalAdapter)
    assert _PRODUCTION_HOST not in adapter._settings.neo4j_uri
    assert adapter._settings.neo4j_uri == neo4j_settings.neo4j_uri

    readiness_use_case = _build_readiness_use_case(neo4j_settings)
    readiness_adapter = readiness_use_case._retrieval_layer._retrieval_port
    assert isinstance(readiness_adapter, Neo4jRetrievalAdapter)
    assert readiness_adapter._settings.neo4j_uri == neo4j_settings.neo4j_uri


@pytest.mark.neo4j_integration
async def test_retrieval_evaluation_does_not_mutate_graph(
    neo4j_settings: Settings,
    neo4j_driver: Any,
) -> None:
    """The retrieval evaluation layer is read-only against testcontainers Neo4j."""
    before = await _count_nodes(neo4j_driver)
    layer = _build_retrieval_layer(neo4j_settings)
    try:
        result = await layer.execute(run_ragas=False)
    finally:
        await layer._retrieval_port.close()  # type: ignore[attr-defined]
    after = await _count_nodes(neo4j_driver)

    assert result.status == LayerStatus.PASSED
    assert before == after


@pytest.mark.neo4j_integration
async def test_readiness_evaluation_does_not_mutate_graph(
    neo4j_settings: Settings,
    neo4j_driver: Any,
) -> None:
    """A readiness gate using the real retrieval layer does not mutate the graph."""
    before = await _count_nodes(neo4j_driver)
    use_case = _build_readiness_use_case(neo4j_settings)
    target = build_audit_target(
        "bookgraph-neo4j",
        neo4j_settings.neo4j_uri,
        neo4j_settings.neo4j_database,
    )
    report = AuditReport(target=target, state=OverallState.PASSED)
    try:
        result = await use_case.execute("test-readiness", report)
    finally:
        await use_case._retrieval_layer._retrieval_port.close()  # type: ignore[attr-defined]
    after = await _count_nodes(neo4j_driver)

    # Retrieval required passes; extraction is optional and fake-PENDING, so
    # the gate passes (optional layers never block per R6).
    assert result.exit_code == 0
    assert before == after
