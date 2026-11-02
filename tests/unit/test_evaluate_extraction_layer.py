"""Tests for EvaluateExtractionLayerUseCase — the extraction coverage instrument (front d).

The stub this file replaces pinned a placeholder (``PENDING`` with ``coverage: 0.0,
threshold=None``); per S4 pin 5 it is **rewritten**, not adapted: the pins below hold the
measured behaviour — the quotient over the ACTIVE entity population, ``status=PASSED``,
the below-threshold warning attributed to the corpus, and the dead-letter count read
through the existing checkpoint port.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from book_graph_rag.application.evaluate_extraction_layer_use_case import (
    EvaluateExtractionLayerUseCase,
)
from book_graph_rag.domain.checkpoint_models import (
    Checkpoint,
    CheckpointStatus,
    LeaseResult,
    RedactedError,
    VersionDimensions,
)
from book_graph_rag.domain.evaluation_models import (
    EvaluationDataset,
    EvaluationLayerResult,
    LayerMetricValue,
    LayerStatus,
    UnknownDatasetError,
)
from book_graph_rag.domain.extraction_fidelity import Prediction
from book_graph_rag.infrastructure.evaluation_dataset_loader import (
    JsonlManifestEvaluationDatasetLoader,
)
from book_graph_rag.ports.checkpoint_port import CheckpointPort
from book_graph_rag.ports.evaluation_dataset_port import EvaluationDatasetPort
from book_graph_rag.ports.extraction_coverage_port import (
    BookCoverage,
    ExtractionCoverageMeasurement,
    ExtractionCoveragePort,
)
from book_graph_rag.ports.extraction_prediction_port import (
    ExtractionPredictionMeasurement,
    ExtractionPredictionPort,
)

_VERSIONS = VersionDimensions(
    source_version="abcd1234abcd1234",
    pipeline_version="1.0.0",
    model_version="openai:gpt-4o-mini:2026-09-01",
    schema_version="1.0.0",
)


def _checkpoint(source_id: str, chunk_index: int, status: CheckpointStatus) -> Checkpoint:
    return Checkpoint(
        source_id=source_id,
        chunk_index=chunk_index,
        status=status,
        versions=_VERSIONS,
        updated_at=datetime.now(UTC),
    )


class _FakeCoveragePort(ExtractionCoveragePort):
    """Stands in for the adapter: returns a pre-measured (active-only) population."""

    def __init__(self, measurement: ExtractionCoverageMeasurement) -> None:
        self._measurement = measurement
        self.calls = 0

    async def measure(self) -> ExtractionCoverageMeasurement:
        self.calls += 1
        return self._measurement


class _MergedCarryingCoveragePort(ExtractionCoveragePort):
    """Fake over a tiny entity population that INCLUDES merged entities (pin 3).

    The graph this fake stands for holds 13 entities: 8 active (all of them mentioned)
    and 5 merged. The approved queries exclude ``merged_into``-marked entities from BOTH
    halves of the quotient, so the measurement carries 8/8 — while the naive
    merged-inclusive quotient (8/13) would land below the 0.95 threshold and emit a
    warning. The pin proves the quotient comes out right even though the fake's
    underlying totals include merged entities.
    """

    TOTAL_ENTITIES = 13
    MERGED_ENTITIES = 5
    MENTIONED_ACTIVE = 8

    def __init__(self) -> None:
        self.active_entities = self.TOTAL_ENTITIES - self.MERGED_ENTITIES
        self.calls = 0

    async def measure(self) -> ExtractionCoverageMeasurement:
        self.calls += 1
        return ExtractionCoverageMeasurement(
            active_entities=self.active_entities,
            mentioned_active_entities=self.MENTIONED_ACTIVE,
            books=(
                BookCoverage(
                    book_id="knowledge:test-book",
                    chunk_count=4,
                    mentioned_entities=self.MENTIONED_ACTIVE,
                ),
            ),
        )


class _FakeCheckpointPort(CheckpointPort):
    """In-memory checkpoint store; FAILED rows are pre-seeded per source."""

    def __init__(self, failed: dict[str, set[int]] | None = None) -> None:
        self._failed = failed or {}
        self.fetch_calls: list[tuple[str, list[int]]] = []

    async def fetch_state(self, source_id: str, chunk_indices: list[int]) -> dict[int, Checkpoint]:
        self.fetch_calls.append((source_id, list(chunk_indices)))
        failed_indices = self._failed.get(source_id, set())
        return {
            index: _checkpoint(
                source_id,
                index,
                CheckpointStatus.FAILED if index in failed_indices else CheckpointStatus.PROCESSED,
            )
            for index in chunk_indices
        }

    async def acquire_lease(
        self, source_id: str, chunk_index: int, versions: VersionDimensions
    ) -> LeaseResult:
        raise NotImplementedError("not used by the extraction coverage tests")

    async def release_lease_to_failed(
        self,
        source_id: str,
        chunk_index: int,
        error: RedactedError,
        attempt: int,
    ) -> Checkpoint:
        raise NotImplementedError("not used by the extraction coverage tests")

    async def mark_stale_and_reset(
        self, source_id: str, expected_versions: VersionDimensions
    ) -> int:
        raise NotImplementedError("not used by the extraction coverage tests")

    async def reclaim_stale_leases(self, source_id: str, now: datetime, stale_seconds: int) -> int:
        raise NotImplementedError("not used by the extraction coverage tests")


def _measurement(
    *, active: int, mentioned: int, books: tuple[BookCoverage, ...] = ()
) -> ExtractionCoverageMeasurement:
    return ExtractionCoverageMeasurement(
        active_entities=active,
        mentioned_active_entities=mentioned,
        books=books,
    )


def _metric(result: EvaluationLayerResult, name: str) -> LayerMetricValue:
    return next(m for m in result.project_owned_metrics if m.name == name)


# ── Extraction-fidelity fixtures (protocol §1: unit = one chunk) ─────────────

_BOOK = "knowledge:fixture-book"

#: Two labeled rows in the REAL label-file shape (protocol §5): chunk_id is
#: ``{book_id}:{chunk_index}`` with a colon-bearing book id, entities sorted by
#: canonical, ``completed: true``.
_FIDELITY_ROWS: tuple[dict[str, Any], ...] = (
    {
        "chunk_id": f"{_BOOK}:1",
        "book_id": _BOOK,
        "chunk_index": 1,
        "entities": [
            {
                "surfaces": ["ReAct", "Reasoning and Acting"],
                "canonical": "ReAct",
                "type": "pattern",
                "confidence": "high",
            }
        ],
        "notes": "",
        "completed": True,
    },
    {
        "chunk_id": f"{_BOOK}:2",
        "book_id": _BOOK,
        "chunk_index": 2,
        "entities": [
            {
                "surfaces": ["MCP", "Model Context Protocol"],
                "canonical": "MCP",
                "type": "mcp",
                "confidence": "high",
            }
        ],
        "notes": "",
        "completed": True,
    },
)


def _perfect_predictions() -> dict[str, list[Prediction]]:
    return {
        f"{_BOOK}:1": [
            Prediction(
                id="entity:react-pattern",
                name="ReAct",
                canonical_name="ReAct",
                aliases=[],
                type="pattern",
                merged_into=None,
            )
        ],
        f"{_BOOK}:2": [
            Prediction(
                id="entity:mcp-mcp",
                name="MCP",
                canonical_name=None,
                aliases=["Model Context Protocol"],
                type="mcp",
                merged_into=None,
            )
        ],
    }


class _FakeDatasetPort(EvaluationDatasetPort):
    """Stands in for the MANIFEST loader: canned rows or a load failure."""

    def __init__(
        self,
        records: tuple[dict[str, Any], ...] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._records = _FIDELITY_ROWS if records is None else records
        self._error = error
        self.loaded: list[str] = []

    def load(self, dataset_id: str) -> EvaluationDataset:
        self.loaded.append(dataset_id)
        if self._error is not None:
            raise self._error
        return EvaluationDataset(dataset_id=dataset_id, records=self._records)

    def list_datasets(self) -> tuple[str, ...]:
        return ("extraction_dataset_huyen",)


class _FakePredictionPort(ExtractionPredictionPort):
    """Stands in for the Neo4j adapter: canned predictions per chunk."""

    def __init__(
        self,
        predictions_by_chunk: dict[str, list[Prediction]] | None = None,
        canonical_of: dict[str, str] | None = None,
    ) -> None:
        self._predictions = (
            _perfect_predictions() if predictions_by_chunk is None else predictions_by_chunk
        )
        self._canonical = canonical_of or {}
        self.asked_chunk_ids: list[str] = []

    async def measure(self, labeled_chunks: Any) -> ExtractionPredictionMeasurement:
        self.asked_chunk_ids = [chunk.chunk_id for chunk in labeled_chunks]
        return ExtractionPredictionMeasurement(
            predictions_by_chunk=self._predictions,
            canonical_of=self._canonical,
        )


def _fidelity_use_case(
    *,
    coverage_port: ExtractionCoveragePort | None = None,
    prediction_port: ExtractionPredictionPort | None = None,
    dataset_port: EvaluationDatasetPort | None = None,
) -> EvaluateExtractionLayerUseCase:
    return EvaluateExtractionLayerUseCase(
        coverage_port=coverage_port or _FakeCoveragePort(_measurement(active=10, mentioned=10)),
        checkpoint_port=_FakeCheckpointPort(),
        prediction_port=prediction_port or _FakePredictionPort(),
        dataset_port=dataset_port or _FakeDatasetPort(),
    )


def _coverage_use_case(
    *,
    coverage_port: ExtractionCoveragePort,
    checkpoint_port: CheckpointPort,
) -> EvaluateExtractionLayerUseCase:
    """Coverage-pin construction with the fidelity ports wired to working fakes.

    The original coverage pins predate the fidelity half; they keep testing the
    quotient with a dataset that loads and perfect predictions, so no robustness
    warning dilutes their warning assertions.
    """
    return EvaluateExtractionLayerUseCase(
        coverage_port=coverage_port,
        checkpoint_port=checkpoint_port,
        prediction_port=_FakePredictionPort(),
        dataset_port=_FakeDatasetPort(),
    )


def _fidelity_names(result: EvaluationLayerResult) -> set[str]:
    return {m.name for m in result.project_owned_metrics}


# ── Pin 1: complete coverage ⇒ no warnings, coverage = 1.0, status PASSED ──────


def test_complete_coverage_reports_one_and_no_warnings() -> None:
    """Full :MENTIONS coverage ⇒ coverage = 1.0, zero warnings, status PASSED."""
    coverage_port = _FakeCoveragePort(
        _measurement(
            active=100,
            mentioned=100,
            books=(
                BookCoverage(book_id="knowledge:alpha", chunk_count=10, mentioned_entities=100),
            ),
        )
    )
    checkpoint_port = _FakeCheckpointPort()
    uc = _coverage_use_case(coverage_port=coverage_port, checkpoint_port=checkpoint_port)
    result = asyncio.run(uc.execute())

    assert result.status == LayerStatus.PASSED
    assert result.warnings == ()
    coverage = _metric(result, "coverage")
    assert coverage.value == 1.0
    assert coverage.threshold == 0.95
    assert coverage.comparator == ">="
    failed = _metric(result, "failed_checkpoints")
    assert failed.value == 0.0
    assert failed.threshold is None
    assert coverage_port.calls == 1


# ── Pin 2: below threshold keeps PASSED and warns about the CORPUS ─────────────


def test_below_threshold_keeps_passed_and_warns_about_the_corpus() -> None:
    """Coverage 0.94 < 0.95 ⇒ status stays PASSED and one warning names the corpus."""
    uc = _coverage_use_case(
        coverage_port=_FakeCoveragePort(
            _measurement(
                active=100,
                mentioned=94,
                books=(
                    BookCoverage(book_id="knowledge:alpha", chunk_count=10, mentioned_entities=94),
                ),
            )
        ),
        checkpoint_port=_FakeCheckpointPort(),
    )
    result = asyncio.run(uc.execute())

    assert result.status == LayerStatus.PASSED
    assert len(result.warnings) == 1
    warning = result.warnings[0]
    assert "corpus" in warning
    assert "instrument" not in warning
    assert _metric(result, "coverage").value == 0.94


def test_coverage_exactly_at_threshold_emits_no_warning() -> None:
    """The comparator is ``>=``: exactly 0.95 is not a fall below the threshold."""
    uc = _coverage_use_case(
        coverage_port=_FakeCoveragePort(
            _measurement(
                active=100,
                mentioned=95,
                books=(
                    BookCoverage(book_id="knowledge:alpha", chunk_count=10, mentioned_entities=95),
                ),
            )
        ),
        checkpoint_port=_FakeCheckpointPort(),
    )
    result = asyncio.run(uc.execute())

    assert result.status == LayerStatus.PASSED
    assert result.warnings == ()


# ── Pin 3: the denominator is the ACTIVE population ────────────────────────────


def test_merged_entities_never_enter_the_quotient() -> None:
    """A fake whose totals include merged entities must still yield the right quotient.

    The naive merged-inclusive quotient (8/13) is below the threshold; the correct
    active-only quotient (8/8) is 1.0. If the quotient came out anywhere near 8/13
    the use case would have warned — which is exactly what this pin proves cannot
    happen.
    """
    naive_merged_inclusive = 8 / 13
    assert naive_merged_inclusive < 0.95  # the trap the pin guards against

    coverage_port = _MergedCarryingCoveragePort()
    uc = _coverage_use_case(coverage_port=coverage_port, checkpoint_port=_FakeCheckpointPort())
    result = asyncio.run(uc.execute())

    assert coverage_port.calls == 1
    assert _metric(result, "coverage").value == 1.0
    assert result.warnings == ()
    # The rationale states the quotient with its explicit denominator …
    assert "8/8" in result.rationale
    # … and names the merged-exclusion so no reader subtracts merged entities.
    assert "merged" in result.rationale.lower()


# ── Pin 5 (with the placeholder's removal): never the stub's PENDING again ─────


def test_placeholder_pending_stub_is_gone() -> None:
    """The stub pinned PENDING/0.0/None; the instrument pins PASSED/0.95 instead."""
    uc = _coverage_use_case(
        coverage_port=_FakeCoveragePort(_measurement(active=0, mentioned=0)),
        checkpoint_port=_FakeCheckpointPort(),
    )
    result = asyncio.run(uc.execute())

    assert result.status == LayerStatus.PASSED
    assert "deferred" not in result.rationale.lower()
    coverage = _metric(result, "coverage")
    assert coverage.threshold == 0.95


# ── Triangulation ─────────────────────────────────────────────────────────────


def test_empty_active_population_holds_the_invariant_vacuously() -> None:
    """0/0 is undefined; the invariant holder reports 1.0 and says so in the rationale."""
    uc = _coverage_use_case(
        coverage_port=_FakeCoveragePort(_measurement(active=0, mentioned=0)),
        checkpoint_port=_FakeCheckpointPort(),
    )
    result = asyncio.run(uc.execute())

    assert _metric(result, "coverage").value == 1.0
    assert result.warnings == ()
    assert "vacuous" in result.rationale


def test_failed_checkpoints_are_counted_per_book_through_the_checkpoint_port() -> None:
    """failed_checkpoints = the FAILED rows in each book's chunk range (S2, no threshold)."""
    checkpoint_port = _FakeCheckpointPort(failed={"knowledge:alpha": {3, 7}, "knowledge:beta": {1}})
    uc = _coverage_use_case(
        coverage_port=_FakeCoveragePort(
            _measurement(
                active=10,
                mentioned=10,
                books=(
                    BookCoverage(book_id="knowledge:alpha", chunk_count=10, mentioned_entities=5),
                    BookCoverage(book_id="knowledge:beta", chunk_count=5, mentioned_entities=5),
                ),
            )
        ),
        checkpoint_port=checkpoint_port,
    )
    result = asyncio.run(uc.execute())

    failed = _metric(result, "failed_checkpoints")
    assert failed.value == 3.0
    assert failed.threshold is None  # no target: inventing one would invent a goal
    # The existing port is asked per book with that book's chunk range.
    assert checkpoint_port.fetch_calls == [
        ("knowledge:alpha", list(range(10))),
        ("knowledge:beta", list(range(5))),
    ]
    assert "failed_checkpoints = 3 across 2 source(s)" in result.rationale


def test_zero_chunk_books_never_reach_the_checkpoint_port() -> None:
    """A book with no chunks has no chunk range to ask about."""
    checkpoint_port = _FakeCheckpointPort()
    uc = _coverage_use_case(
        coverage_port=_FakeCoveragePort(
            _measurement(
                active=0,
                mentioned=0,
                books=(
                    BookCoverage(book_id="knowledge:empty", chunk_count=0, mentioned_entities=0),
                ),
            )
        ),
        checkpoint_port=checkpoint_port,
    )
    result = asyncio.run(uc.execute())

    assert checkpoint_port.fetch_calls == []
    assert _metric(result, "failed_checkpoints").value == 0.0


def test_rationale_carries_raw_numbers_the_denominator_and_the_book_breakdown() -> None:
    """S7: the rationale is a deliverable — raw numbers, explicit denominator, breakdown."""
    uc = _coverage_use_case(
        coverage_port=_FakeCoveragePort(
            _measurement(
                active=20,
                mentioned=19,
                books=(
                    BookCoverage(book_id="knowledge:alpha", chunk_count=8, mentioned_entities=12),
                    BookCoverage(book_id="knowledge:beta", chunk_count=4, mentioned_entities=7),
                ),
            )
        ),
        checkpoint_port=_FakeCheckpointPort(failed={"knowledge:alpha": {2}}),
    )
    result = asyncio.run(uc.execute())

    rationale = result.rationale
    assert "19/20 = 0.9500" in rationale  # raw numerator/denominator and the quotient
    assert "merged_into IS NULL OR merged_into = ''" in rationale  # explicit denominator
    assert "knowledge:alpha: 12 mentioned entities / 8 chunks" in rationale
    assert "knowledge:beta: 7 mentioned entities / 4 chunks" in rationale
    assert "failed_checkpoints = 1 across 1 source(s)" in rationale


def test_measurement_model_rejects_a_numerator_above_the_denominator() -> None:
    """S1: only a quotient bounded in [0,1] is algebraically valid."""
    with pytest.raises(ValidationError):
        ExtractionCoverageMeasurement(
            active_entities=8,
            mentioned_active_entities=9,
            books=(),
        )


# ── Extraction fidelity: measurement only, never blocking (protocol §7) ──────


def test_prediction_and_dataset_ports_are_required() -> None:
    """Wiring omission is a construction error, never a silent degraded rung.

    A ``None`` default would let a composition root forget one port and watch
    the fidelity half vanish into the rationale while coverage keeps reporting.
    """
    with pytest.raises(TypeError):
        # The omitted ports ARE the test: construction must fail, not degrade.
        EvaluateExtractionLayerUseCase(  # type: ignore[call-arg]
            coverage_port=_FakeCoveragePort(_measurement(active=1, mentioned=1)),
            checkpoint_port=_FakeCheckpointPort(),
        )


def test_perfect_predictions_over_two_chunks_give_f1_one() -> None:
    """Perfect predictions over the two-chunk fixture: every rate is 1.0, no threshold."""
    result = asyncio.run(_fidelity_use_case().execute())

    assert result.status == LayerStatus.PASSED
    for name in (
        "entity_precision",
        "entity_recall",
        "entity_f1",
        "entity_type_agreement",
    ):
        metric = _metric(result, name)
        assert metric.value == 1.0, name
        assert metric.threshold is None, name  # measurement only, nothing blocking
    # Coverage keeps working alongside the fidelity numbers.
    assert _metric(result, "coverage").value == 1.0
    assert result.warnings == ()
    # The rationale carries the raw counts (S7).
    assert "labels=2" in result.rationale
    assert "predictions=2" in result.rationale
    assert "matched=2" in result.rationale
    assert "matching_types=2" in result.rationale


def test_wrong_type_prediction_moves_only_the_type_agreement_axis() -> None:
    """A type error changes only type_agreement: never precision/recall/f1 (§4.5)."""
    predictions = _perfect_predictions()
    predictions[f"{_BOOK}:2"] = [
        Prediction(
            id="entity:mcp-mcp",
            name="MCP",
            canonical_name=None,
            aliases=["Model Context Protocol"],
            type="tool",  # wrong type, right surfaces
            merged_into=None,
        )
    ]
    result = asyncio.run(
        _fidelity_use_case(prediction_port=_FakePredictionPort(predictions)).execute()
    )

    assert _metric(result, "entity_f1").value == 1.0
    assert _metric(result, "entity_precision").value == 1.0
    assert _metric(result, "entity_recall").value == 1.0
    assert _metric(result, "entity_type_agreement").value == 0.5
    assert "matching_types=1" in result.rationale  # one of two matched pairs wrong-typed


def test_merged_duplicate_predictions_are_collapsed_to_one() -> None:
    """Predictions collapsing to the same canonical entity count ONCE (§4.3)."""
    predictions = _perfect_predictions()
    predictions[f"{_BOOK}:1"] = [
        *predictions[f"{_BOOK}:1"],
        Prediction(
            id="entity:react-alias-pattern",
            name="Reasoning and Acting",
            canonical_name="ReAct",
            aliases=[],
            type="pattern",
            merged_into="entity:react-pattern",
        ),
    ]
    result = asyncio.run(
        _fidelity_use_case(
            prediction_port=_FakePredictionPort(
                predictions, canonical_of={"entity:react-alias-pattern": "entity:react-pattern"}
            )
        ).execute()
    )

    # Without collapse: predictions=3, precision=2/3. With it: 1.0.
    assert _metric(result, "entity_precision").value == 1.0
    assert _metric(result, "entity_recall").value == 1.0
    assert _metric(result, "entity_f1").value == 1.0
    assert "predictions=2" in result.rationale  # pooled: 1 collapsed + 1


def test_missing_dataset_warns_and_keeps_coverage_without_fidelity_numbers() -> None:
    """Dataset cannot load ⇒ warning + rationale, coverage intact, no entity_* (robustness)."""
    use_case = _fidelity_use_case(
        dataset_port=_FakeDatasetPort(error=UnknownDatasetError("extraction_dataset_huyen"))
    )
    result = asyncio.run(use_case.execute())

    assert result.status == LayerStatus.PASSED
    assert _metric(result, "coverage").value == 1.0
    names = _fidelity_names(result)
    assert "coverage" in names
    assert not any(name.startswith("entity_") for name in names)
    assert any("extraction fidelity not measured" in warning for warning in result.warnings)
    assert "extraction fidelity not measured" in result.rationale


def test_pending_rows_are_not_labels_and_suppress_the_fidelity_numbers() -> None:
    """A ``completed: false`` row is not a label (protocol §5): warn, no fidelity numbers."""
    pending_rows = tuple(dict(row, completed=False) for row in _FIDELITY_ROWS)
    use_case = _fidelity_use_case(dataset_port=_FakeDatasetPort(records=pending_rows))
    result = asyncio.run(use_case.execute())

    assert result.status == LayerStatus.PASSED
    assert _metric(result, "coverage").value == 1.0
    names = _fidelity_names(result)
    assert not any(name.startswith("entity_") for name in names)
    assert any("pending" in warning for warning in result.warnings)
    assert "completed: false" in result.rationale


def test_committed_huyen_dataset_rows_flow_through_the_layer() -> None:
    """The REAL extraction_dataset_huyen.jsonl rows validate and produce fidelity numbers."""
    loader = JsonlManifestEvaluationDatasetLoader(Path("data/evaluation/MANIFEST.json"))
    dataset = loader.load("extraction_dataset_huyen")
    result = asyncio.run(
        _fidelity_use_case(dataset_port=_FakeDatasetPort(records=dataset.records)).execute()
    )

    assert result.status == LayerStatus.PASSED
    names = _fidelity_names(result)
    assert "entity_recall" in names
    assert "entity_f1" in names
    assert not any("extraction fidelity not measured" in warning for warning in result.warnings)
    # The pilot labels all six chunks; with an empty prediction set precision is
    # undefined (no predictions) and is OMITTED rather than faked as 0.0.
    assert "entity_precision" not in names
    assert "labels=6" in result.rationale


def test_committed_huyen_dataset_matches_its_manifest_hash() -> None:
    """The real file's sha256 still matches MANIFEST.json (loader validates it)."""
    loader = JsonlManifestEvaluationDatasetLoader(Path("data/evaluation/MANIFEST.json"))
    dataset = loader.load("extraction_dataset_huyen")
    assert dataset.dataset_id == "extraction_dataset_huyen"
    assert len(dataset.records) == 6


# ── Opt-in fidelity detail (measurement only, protocol §7) ───────────────────


def _headline_counts(result: EvaluationLayerResult) -> tuple[int, int, int, int]:
    """(labels, predictions, matched, matching_types) as the rationale headline reports them."""
    match = re.search(
        r"labels=(\d+) predictions=(\d+) matched=(\d+) matching_types=(\d+)",
        result.rationale,
    )
    assert match is not None, result.rationale
    return (
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
        int(match.group(4)),
    )


def test_fidelity_detail_is_off_by_default_and_the_four_values_do_not_move() -> None:
    """Flag off ⇒ ``fidelity_detail`` is None and the four values match a run without the parameter.

    The absent detail renders as ``null`` in the report — exactly like the sibling
    optional sections ``ragas_secondary`` and ``answer_grounding`` (the real gate
    report carries both as ``null``), rather than as a key missing from one run and
    present in another.
    """
    without_parameter = asyncio.run(_fidelity_use_case().execute())
    explicit_off = asyncio.run(_fidelity_use_case().execute(include_fidelity_detail=False))

    assert without_parameter.fidelity_detail is None
    assert explicit_off.fidelity_detail is None
    for name in (
        "entity_precision",
        "entity_recall",
        "entity_f1",
        "entity_type_agreement",
    ):
        assert _metric(without_parameter, name).value == _metric(explicit_off, name).value, name
        assert _metric(without_parameter, name).threshold is None, name
    # Absent data renders as null, as it does for the other optional sections.
    assert json.loads(without_parameter.model_dump_json())["fidelity_detail"] is None
    assert json.loads(explicit_off.model_dump_json())["fidelity_detail"] is None


def _detail_flag_predictions() -> dict[str, list[Prediction]]:
    """Two-chunk fixture extended: a wrong-typed match and a merged unmatched prediction."""
    predictions = _perfect_predictions()
    predictions[f"{_BOOK}:2"] = [
        Prediction(
            id="entity:mcp-mcp",
            name="MCP",
            canonical_name=None,
            aliases=["Model Context Protocol"],
            type="tool",  # right entity, wrong type ⇒ matched, type_matches False
            merged_into=None,
        ),
        Prediction(
            id="entity:old-graphrag",
            name="GraphRAG",
            canonical_name=None,
            aliases=[],
            type="pattern",
            merged_into="entity:graphrag",  # merged and unmatched: must NOT be dropped
        ),
    ]
    return predictions


_DETAIL_CANONICAL_OF = {
    "entity:old-graphrag": "entity:graphrag",
}


def test_fidelity_detail_flag_on_carries_the_right_entities_per_group() -> None:
    """Flag on ⇒ per-chunk matched/unmatched groups hold exactly the entities counted."""
    use_case = _fidelity_use_case(
        prediction_port=_FakePredictionPort(
            _detail_flag_predictions(), canonical_of=dict(_DETAIL_CANONICAL_OF)
        )
    )
    result = asyncio.run(use_case.execute(include_fidelity_detail=True))

    detail = result.fidelity_detail
    assert detail is not None
    by_chunk = {record.chunk_id: record for record in detail}
    assert set(by_chunk) == {f"{_BOOK}:1", f"{_BOOK}:2"}

    chunk_one = by_chunk[f"{_BOOK}:1"]
    (pair,) = chunk_one.matched
    assert pair.label_canonical == "ReAct"
    assert pair.prediction_canonical == "entity:react-pattern"
    assert pair.prediction_id == "entity:react-pattern"
    assert pair.type_matches is True
    assert chunk_one.unmatched_predictions == ()
    assert chunk_one.unmatched_labels == ()

    chunk_two = by_chunk[f"{_BOOK}:2"]
    (mcp_pair,) = chunk_two.matched
    assert mcp_pair.label_canonical == "MCP"
    assert mcp_pair.prediction_id == "entity:mcp-mcp"
    assert mcp_pair.type_matches is False  # right entity, wrong type (§4.5)
    (unmatched,) = chunk_two.unmatched_predictions
    assert unmatched.id == "entity:old-graphrag"
    assert unmatched.name == "GraphRAG"
    assert unmatched.type == "pattern"
    assert unmatched.merged_into == "entity:graphrag"  # visible, not silently dropped
    assert chunk_two.unmatched_labels == ()


def test_fidelity_detail_reconciles_with_the_reported_counts() -> None:
    """THE pin: summed detail equals the counted numbers — one measurement, not two.

    The fixture includes a collapsed duplicate (§4.3) so the unmatched total is checked
    against the COLLAPSED prediction count the rationale reports.
    """
    predictions = _detail_flag_predictions()
    predictions[f"{_BOOK}:1"] = [
        *predictions[f"{_BOOK}:1"],
        Prediction(
            id="entity:react-alias-pattern",
            name="Reasoning and Acting",
            canonical_name="ReAct",
            aliases=[],
            type="pattern",
            merged_into="entity:react-pattern",
        ),
    ]
    use_case = _fidelity_use_case(
        prediction_port=_FakePredictionPort(
            predictions,
            canonical_of={
                "entity:react-alias-pattern": "entity:react-pattern",
                **_DETAIL_CANONICAL_OF,
            },
        )
    )
    result = asyncio.run(use_case.execute(include_fidelity_detail=True))

    detail = result.fidelity_detail
    assert detail is not None
    labels, predictions_count, matched_count, matching_types_count = _headline_counts(result)

    summed_matched = sum(len(record.matched) for record in detail)
    summed_unmatched_predictions = sum(len(record.unmatched_predictions) for record in detail)
    summed_unmatched_labels = sum(len(record.unmatched_labels) for record in detail)

    assert summed_matched == matched_count
    assert summed_unmatched_predictions == predictions_count - matched_count
    # Every label is either matched or listed — nothing vanishes between the two views.
    assert summed_matched + summed_unmatched_labels == labels
    assert (
        sum(1 for record in detail for pair in record.matched if pair.type_matches)
        == matching_types_count
    )


def test_missing_dataset_with_detail_flag_reports_no_numbers_and_no_detail() -> None:
    """Dataset unavailable ⇒ even with the flag on: no fidelity numbers, no detail."""
    use_case = _fidelity_use_case(
        dataset_port=_FakeDatasetPort(error=UnknownDatasetError("extraction_dataset_huyen"))
    )
    result = asyncio.run(use_case.execute(include_fidelity_detail=True))

    assert result.status == LayerStatus.PASSED
    assert result.fidelity_detail is None
    assert not any(name.startswith("entity_") for name in _fidelity_names(result))
    assert any("extraction fidelity not measured" in warning for warning in result.warnings)


def test_pending_rows_with_detail_flag_report_no_numbers_and_no_detail() -> None:
    """A pending row is not a label (§5): with the flag on it still reports no detail."""
    pending_rows = tuple(dict(row, completed=False) for row in _FIDELITY_ROWS)
    use_case = _fidelity_use_case(dataset_port=_FakeDatasetPort(records=pending_rows))
    result = asyncio.run(use_case.execute(include_fidelity_detail=True))

    assert result.status == LayerStatus.PASSED
    assert result.fidelity_detail is None
    assert not any(name.startswith("entity_") for name in _fidelity_names(result))
    assert any("pending" in warning for warning in result.warnings)
