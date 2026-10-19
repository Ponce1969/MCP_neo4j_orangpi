"""Tests for EvaluateExtractionLayerUseCase — the extraction coverage instrument (front d).

The stub this file replaces pinned a placeholder (``PENDING`` with ``coverage: 0.0,
threshold=None``); per S4 pin 5 it is **rewritten**, not adapted: the pins below hold the
measured behaviour — the quotient over the ACTIVE entity population, ``status=PASSED``,
the below-threshold warning attributed to the corpus, and the dead-letter count read
through the existing checkpoint port.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

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
    EvaluationLayerResult,
    LayerMetricValue,
    LayerStatus,
)
from book_graph_rag.ports.checkpoint_port import CheckpointPort
from book_graph_rag.ports.extraction_coverage_port import (
    BookCoverage,
    ExtractionCoverageMeasurement,
    ExtractionCoveragePort,
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
    uc = EvaluateExtractionLayerUseCase(
        coverage_port=coverage_port, checkpoint_port=checkpoint_port
    )
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
    uc = EvaluateExtractionLayerUseCase(
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
    uc = EvaluateExtractionLayerUseCase(
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
    uc = EvaluateExtractionLayerUseCase(
        coverage_port=coverage_port, checkpoint_port=_FakeCheckpointPort()
    )
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
    uc = EvaluateExtractionLayerUseCase(
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
    uc = EvaluateExtractionLayerUseCase(
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
    uc = EvaluateExtractionLayerUseCase(
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
    uc = EvaluateExtractionLayerUseCase(
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
    uc = EvaluateExtractionLayerUseCase(
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
