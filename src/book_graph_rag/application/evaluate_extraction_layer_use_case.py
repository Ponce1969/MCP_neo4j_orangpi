"""Application use case: evaluate the extraction layer (Slice B, U4; front d).

The layer measures the invariant it claims: REQ-PROV-01 guarantees one ``:MENTIONS``
per extracted entity, so the coverage quotient (S1's operator-approved definition)
is 1.0 until the pipeline breaks — the instrument is born as a **guardian of that
invariant**, not as a quality score. On top of it sits the fidelity half (protocol
``docs/evaluation/extraction-labeling-protocol.md``): precision/recall/F1 over the
labeled ``extraction_dataset_huyen`` rows, with the type axis reported separately
(§4.5). Both halves are **measurement only, never blocking** (§7): the first
measurement is a baseline, and a threshold derived before it would be invented.

Status semantics (S3): ``derive_overall_status`` returns PASSED **before** it looks at
the WARNING branch, so a WARNING *status* on this optional layer would be masked;
``warnings`` and ``rationale`` are the durable slots for a below-threshold drop
(precedent: the retrieval layer, measured with a threshold and never failing) and for
a fidelity half that could not run (dataset missing, malformed or pending), which
must never be reported as a number.
"""

from __future__ import annotations

import uuid

from book_graph_rag.domain.checkpoint_models import CheckpointStatus
from book_graph_rag.domain.evaluation_models import (
    EvaluationLayerResult,
    FidelityChunkDetail,
    FidelityMatchedPairDetail,
    FidelityUnmatchedLabelDetail,
    FidelityUnmatchedPredictionDetail,
    LayerMetricValue,
    LayerRunMetadata,
    LayerStatus,
)
from book_graph_rag.domain.extraction_fidelity import (
    ChunkFidelityDetail,
    ExtractionFidelityResult,
    calculate_extraction_fidelity,
)
from book_graph_rag.domain.extraction_labels import load_labeled_chunks
from book_graph_rag.ports.checkpoint_port import CheckpointPort
from book_graph_rag.ports.evaluation_dataset_port import EvaluationDatasetPort
from book_graph_rag.ports.extraction_coverage_port import (
    BookCoverage,
    ExtractionCoverageMeasurement,
    ExtractionCoveragePort,
)
from book_graph_rag.ports.extraction_prediction_port import ExtractionPredictionPort


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

    #: The committed label file (protocol §5): LOCAL to one book, a pilot — never a
    #: corpus baseline. The fidelity half loads it through ``EvaluationDatasetPort``
    #: so the MANIFEST sha256 gate applies before any number is computed.
    FIDELITY_DATASET_ID = "extraction_dataset_huyen"

    def __init__(
        self,
        coverage_port: ExtractionCoveragePort,
        checkpoint_port: CheckpointPort,
        *,
        prediction_port: ExtractionPredictionPort,
        dataset_port: EvaluationDatasetPort,
        run_id: str | None = None,
        code_commit: str = "",
    ) -> None:
        #: Required on purpose: a ``None`` default would turn a wiring omission
        #: into a silent degraded rung — coverage keeps reporting while the
        #: fidelity half disappears into the rationale. A composition root that
        #: forgets a port must fail at construction, not at interpretation.
        self._coverage_port = coverage_port
        self._checkpoint_port = checkpoint_port
        self._prediction_port = prediction_port
        self._dataset_port = dataset_port
        self._run_id = run_id or uuid.uuid4().hex
        self._code_commit = code_commit

    async def execute(self, *, include_fidelity_detail: bool = False) -> EvaluationLayerResult:
        """Return a PASSED, WARNING-capable measurement of the coverage invariant.

        ``include_fidelity_detail`` is a per-RUN measurement option, so it lives here
        and not in the constructor: the same wired instance serves default-off calls
        (the readiness gate calls ``execute()`` without it and keeps its report lean),
        and no composition root has to know the flag exists. When off, the fidelity
        half reports exactly what it reports today — ``fidelity_detail`` stays
        ``None`` and renders as ``null``, like the report's other optional sections
        (``ragas_secondary``, ``answer_grounding``).
        """
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

        (
            fidelity_metrics,
            fidelity_warnings,
            fidelity_rationale,
            fidelity_detail,
        ) = await self._measure_fidelity(include_fidelity_detail)
        warnings.extend(fidelity_warnings)

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
                *fidelity_metrics,
            ),
            warnings=tuple(warnings),
            rationale=self._rationale(
                measurement, coverage, failed_checkpoints, sources_with_failures
            )
            + f"; {fidelity_rationale}",
            source_dataset_id="",
            baseline_report_path=None,
            run_metadata=LayerRunMetadata(
                run_id=self._run_id,
                code_commit=self._code_commit,
            ),
            fidelity_detail=fidelity_detail,
        )

    async def _measure_fidelity(
        self, include_fidelity_detail: bool
    ) -> tuple[
        tuple[LayerMetricValue, ...], list[str], str, tuple[FidelityChunkDetail, ...] | None
    ]:
        """Run the protocol-§4 fidelity half; never raise, never fake a number.

        Returns ``(metrics, warnings, rationale, detail)``. On ANY failure to obtain
        trustworthy labels — dataset missing or malformed, pending
        (``completed: false``) rows — the layer reports the reason in
        ``warnings`` + ``rationale``, keeps coverage, and returns NO fidelity
        metrics AND NO detail: a pending row is not a label (protocol §5), and a
        number computed over unlabeled rows would be the exact failure spec 06 §11
        refused. The detail, when requested, is produced by the SAME
        ``calculate_extraction_fidelity`` call that produces the metrics — never a
        second matching pass that could disagree with the reported counts.
        """
        try:
            dataset = self._dataset_port.load(self.FIDELITY_DATASET_ID)
        except Exception as exc:  # noqa: BLE001 — the port raises several types on purpose
            reason = f"dataset {self.FIDELITY_DATASET_ID!r} unavailable: {exc}"
            return (
                (),
                [f"extraction fidelity not measured: {reason}"],
                f"extraction fidelity not measured: {reason}",
                None,
            )

        try:
            labeled_chunks = load_labeled_chunks(dataset.records)
        except Exception as exc:  # noqa: BLE001 — ValidationError/ValueError from the schema
            reason = f"dataset {self.FIDELITY_DATASET_ID!r} malformed: {exc}"
            return (
                (),
                [f"extraction fidelity not measured: {reason}"],
                f"extraction fidelity not measured: {reason}",
                None,
            )

        pending = [chunk.chunk_id for chunk in labeled_chunks if not chunk.completed]
        if pending:
            reason = (
                f"dataset {self.FIDELITY_DATASET_ID!r} has {len(pending)} pending row(s) "
                "(completed: false); a pending row is not a label (protocol §5): "
                f"{', '.join(pending)}"
            )
            return (
                (),
                [f"extraction fidelity not measured: {reason}"],
                f"extraction fidelity not measured: {reason}",
                None,
            )

        prediction_measurement = await self._prediction_port.measure(labeled_chunks)
        fidelity = calculate_extraction_fidelity(
            labeled_chunks,
            prediction_measurement.predictions_by_chunk,
            prediction_measurement.canonical_of,
            include_detail=include_fidelity_detail,
        )
        return (
            self._fidelity_metrics(fidelity),
            [],
            self._fidelity_rationale(fidelity),
            self._fidelity_detail(fidelity.detail),
        )

    @staticmethod
    def _fidelity_detail(
        detail: tuple[ChunkFidelityDetail, ...] | None,
    ) -> tuple[FidelityChunkDetail, ...] | None:
        """Translate the domain records into the report's JSON shape. No logic here."""
        if detail is None:
            return None
        return tuple(
            FidelityChunkDetail(
                chunk_id=record.chunk_id,
                matched=tuple(
                    FidelityMatchedPairDetail(
                        label_canonical=pair.label_canonical,
                        prediction_canonical=pair.prediction_canonical,
                        prediction_id=pair.prediction_id,
                        type_matches=pair.type_matches,
                    )
                    for pair in record.matched
                ),
                unmatched_predictions=tuple(
                    FidelityUnmatchedPredictionDetail(
                        id=prediction.id,
                        name=prediction.name,
                        canonical_name=prediction.canonical_name,
                        aliases=prediction.aliases,
                        type=prediction.type,
                        merged_into=prediction.merged_into,
                    )
                    for prediction in record.unmatched_predictions
                ),
                unmatched_labels=tuple(
                    FidelityUnmatchedLabelDetail(
                        canonical=label.canonical,
                        type=label.type,
                        surfaces=label.surfaces,
                    )
                    for label in record.unmatched_labels
                ),
            )
            for record in detail
        )

    @staticmethod
    def _fidelity_metrics(fidelity: ExtractionFidelityResult) -> tuple[LayerMetricValue, ...]:
        """The four fidelity rates as threshold-less metrics (protocol §7).

        An undefined rate (``None``: e.g. precision with zero predictions) is
        OMITTED rather than reported as 0.0 — a fabricated zero reads like a
        measurement of bad extraction instead of "nothing to divide".
        """
        rates = (
            ("entity_precision", fidelity.precision),
            ("entity_recall", fidelity.recall),
            ("entity_f1", fidelity.f1),
            ("entity_type_agreement", fidelity.type_agreement),
        )
        return tuple(
            LayerMetricValue(name=name, value=value, threshold=None)
            for name, value in rates
            if value is not None
        )

    @staticmethod
    def _fidelity_rationale(fidelity: ExtractionFidelityResult) -> str:
        """Raw counts, per-book breakdown and the pessimistic bound (S7, §5).

        The rationale is the durable slot when a status is masked: it must state
        what was counted so a reader never has to re-derive it. The pessimistic
        bound drops ``confidence: "low"`` labels while holding predictions fixed —
        it prices "every contested label was an error", not a second estimate.
        """
        headline = (
            "extraction fidelity (measurement only, threshold none — protocol §7): "
            f"labels={fidelity.labels} predictions={fidelity.predictions} "
            f"matched={fidelity.matched} matching_types={fidelity.matching_types}; "
            f"precision={_format_rate(fidelity.precision)} "
            f"recall={_format_rate(fidelity.recall)} f1={_format_rate(fidelity.f1)} "
            f"type_agreement={_format_rate(fidelity.type_agreement)}"
        )
        breakdown = "; ".join(
            f"{book_id}: labels={m.labels} predictions={m.predictions} "
            f"matched={m.matched} matching_types={m.matching_types} "
            f"f1={_format_rate(m.f1)}"
            for book_id, m in fidelity.per_book.items()
        )
        stable = fidelity.excluding_low_confidence
        bound = (
            "pessimistic bound (confidence:low labels dropped, predictions held fixed — "
            "prices 'every contested label was an error', NOT a second estimate): "
            f"labels={stable.labels} predictions={stable.predictions} "
            f"matched={stable.matched} matching_types={stable.matching_types} "
            f"precision={_format_rate(stable.precision)} "
            f"recall={_format_rate(stable.recall)} f1={_format_rate(stable.f1)}"
        )
        return (
            f"{headline}; per-book: {breakdown or 'none'}; {bound}; pilot dataset "
            f"{EvaluateExtractionLayerUseCase.FIDELITY_DATASET_ID!r} is LOCAL to its "
            "sampled book, never a corpus baseline"
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
            "source(s), counted within each book's current chunk range (secondary "
            "diagnostic metric, no threshold: no target exists for it). A FAILED row left "
            "outside a shorter re-extraction's range is not part of that number, so it can "
            "differ from a raw count of Checkpoint.failed_at"
        )
        return "; ".join(parts)


def _format_rate(value: float | None) -> str:
    """Format a rate, naming ``None`` instead of printing a number that is not one."""
    return "undefined" if value is None else f"{value:.4f}"
