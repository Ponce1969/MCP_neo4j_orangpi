"""Application use case: evaluate the extraction layer (Slice B, U4; front d).

The layer measures the invariant it claims: REQ-PROV-01 guarantees one ``:MENTIONS``
per extracted entity, so the coverage quotient (S1's operator-approved definition)
is 1.0 until the pipeline breaks — the instrument is born as a **guardian of that
invariant**, not as a quality score. Semantic fidelity stays unmeasured (no labeled
extraction dataset exists; spec 06 §11): what this layer measures is connectivity and
provenance, which is why the spec 06 acceptance checkbox stays open.

Status semantics (S3): ``derive_overall_status`` returns PASSED **before** it looks at
the WARNING branch, so a WARNING *status* on this optional layer would be masked;
``warnings`` and ``rationale`` are the durable slots for a below-threshold drop
(precedent: the retrieval layer, measured with a threshold and never failing).
"""

from __future__ import annotations

import uuid

from book_graph_rag.domain.checkpoint_models import CheckpointStatus
from book_graph_rag.domain.evaluation_models import (
    EvaluationLayerResult,
    LayerMetricValue,
    LayerRunMetadata,
    LayerStatus,
)
from book_graph_rag.ports.checkpoint_port import CheckpointPort
from book_graph_rag.ports.extraction_coverage_port import (
    BookCoverage,
    ExtractionCoverageMeasurement,
    ExtractionCoveragePort,
)


class EvaluateExtractionLayerUseCase:
    """Measure :MENTIONS coverage over the ACTIVE entity population; never blocks (spec 06 §11).

    The denominator excludes merged entities in both halves of the quotient (S1); the
    audit inventory, which includes them, is never consulted. The below-threshold path
    reports through ``warnings`` + ``rationale`` and keeps ``status=PASSED`` (S3): the
    warning names a **corpus diagnosis** — the corpus broke the invariant — never the
    instrument that measured it.
    """

    #: The operator-approved threshold (S3): a fall below it is reported, never blocked.
    COVERAGE_THRESHOLD = 0.95

    def __init__(
        self,
        coverage_port: ExtractionCoveragePort,
        checkpoint_port: CheckpointPort,
        *,
        run_id: str | None = None,
        code_commit: str = "",
    ) -> None:
        self._coverage_port = coverage_port
        self._checkpoint_port = checkpoint_port
        self._run_id = run_id or uuid.uuid4().hex
        self._code_commit = code_commit

    async def execute(self) -> EvaluationLayerResult:
        """Return a PASSED, WARNING-capable measurement of the coverage invariant."""
        measurement = await self._coverage_port.measure()
        active = measurement.active_entities
        mentioned = measurement.mentioned_active_entities
        # 0/0 is undefined; with no active entities the invariant holds vacuously, and a
        # fabricated 0.0 would read like a measurement — the exact failure this
        # instrument replaces.
        coverage = mentioned / active if active else 1.0

        warnings: list[str] = []
        if coverage < self.COVERAGE_THRESHOLD:
            unmentioned = active - mentioned
            warnings.append(
                f"corpus diagnosis: extraction coverage {coverage:.4f} below threshold "
                f"{self.COVERAGE_THRESHOLD:.4f} — {unmentioned} active entities lack an "
                "incoming (:Chunk)-[:MENTIONS] relationship; the corpus broke the "
                "REQ-PROV-01 invariant, the measurement is sound"
            )

        failed_checkpoints, sources_with_failures = await self._count_failed_checkpoints(
            measurement.books
        )

        return EvaluationLayerResult(
            layer="extraction",
            status=LayerStatus.PASSED,
            project_owned_metrics=(
                LayerMetricValue(
                    name="coverage",
                    value=coverage,
                    threshold=self.COVERAGE_THRESHOLD,
                    comparator=">=",
                ),
                # Secondary diagnostic metric (S2). No threshold: there is no target for
                # it, and inventing one would be inventing a goal (T3).
                LayerMetricValue(
                    name="failed_checkpoints",
                    value=float(failed_checkpoints),
                    threshold=None,
                ),
            ),
            warnings=tuple(warnings),
            rationale=self._rationale(
                measurement, coverage, failed_checkpoints, sources_with_failures
            ),
            source_dataset_id="",
            baseline_report_path=None,
            run_metadata=LayerRunMetadata(
                run_id=self._run_id,
                code_commit=self._code_commit,
            ),
        )

    async def _count_failed_checkpoints(self, books: tuple[BookCoverage, ...]) -> tuple[int, int]:
        """Count FAILED checkpoints through the EXISTING checkpoint port (T3).

        No second way to ask "how many chunks failed" is built here: the dead-letter
        count reuses ``CheckpointPort.fetch_state`` over each book's chunk range (the
        per-book query already supplies the range), exactly as the indexer does. The
        boundary this admits: a FAILED row left outside a book's *current* chunk range
        (e.g. after a shorter re-extraction) is not part of that book's range and is
        therefore not counted — the number is "failed chunks in the current corpus",
        which is what the diagnostic asks.
        """
        total = 0
        sources_with_failures = 0
        for book in books:
            if book.chunk_count == 0:
                continue
            checkpoints = await self._checkpoint_port.fetch_state(
                book.book_id, list(range(book.chunk_count))
            )
            failed_here = sum(
                1
                for checkpoint in checkpoints.values()
                if checkpoint.status is CheckpointStatus.FAILED
            )
            if failed_here:
                total += failed_here
                sources_with_failures += 1
        return total, sources_with_failures

    @staticmethod
    def _rationale(
        measurement: ExtractionCoverageMeasurement,
        coverage: float,
        failed_checkpoints: int,
        sources_with_failures: int,
    ) -> str:
        """State the raw numbers, the EXPLICIT denominator and the per-book breakdown (S7).

        A reader must never subtract merged entities in their head: the quotient's
        denominator is the active population, printed with its filter, and the per-book
        detail (which excludes merged entities) is spelled out next to it.
        """
        active = measurement.active_entities
        mentioned = measurement.mentioned_active_entities
        parts = [
            f"extraction coverage (diagnostic, optional layer) = {mentioned}/{active} "
            f"= {coverage:.4f}",
            "denominator: active entities (merged_into IS NULL OR merged_into = '') — "
            "merged entities are excluded from BOTH halves of the quotient, so the audit "
            "inventory (which includes merged entities) is never the denominator",
        ]
        if active == 0:
            parts.append(
                "no active entities: the invariant holds vacuously and coverage reports 1.0"
            )
        breakdown = "; ".join(
            f"{book.book_id}: {book.mentioned_entities} mentioned entities / "
            f"{book.chunk_count} chunks"
            for book in measurement.books
        )
        parts.append(f"per-book breakdown: {breakdown or 'no books with chunks'}")
        parts.append(
            f"failed_checkpoints = {failed_checkpoints} across {sources_with_failures} "
            "source(s) (secondary diagnostic metric, no threshold: no target exists for it)"
        )
        return "; ".join(parts)
