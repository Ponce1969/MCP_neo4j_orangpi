"""Run the generation layer one question at a time, keeping every failure visible.

The layer's own command runs all 35 questions in one pass and writes nothing until the end, so a
single failure costs an hour and reports only an exit code. This drives the **same** use case and
the **same** adapters, one record at a time, and prints per-question progress plus the full
traceback of whatever fails, then a summary. Read-only: no graph writes.

Usage::

    NEO4J_URI=bolt://localhost:17687 uv run python scripts-ops/diagnose_generation_layer.py
    # only the last five questions, to reproduce a failure that appears at the end
    ... --only gen-031 gen-032 gen-033 gen-034 gen-035
    # every question from an id on
    ... --from gen-030
"""

from __future__ import annotations

import argparse
import asyncio
import time
import traceback
from collections.abc import Sequence
from typing import Any

from book_graph_rag.application.evaluate_generation_layer_use_case import (
    EvaluateGenerationLayerUseCase,
)
from book_graph_rag.config import Settings
from book_graph_rag.domain.evaluation_models import (
    EvaluationDataset,
    RAGASSecondaryMetrics,
)
from book_graph_rag.infrastructure.evaluation_baseline_loader import JsonEvaluationBaselineLoader
from book_graph_rag.infrastructure.evaluation_dataset_loader import (
    JsonlManifestEvaluationDatasetLoader,
)
from book_graph_rag.infrastructure.llm_claim_validator import LLMClaimValidator
from book_graph_rag.infrastructure.llm_pairwise_judge import LLMPairwiseJudge
from book_graph_rag.infrastructure.neo4j_retrieval_adapter import Neo4jRetrievalAdapter
from book_graph_rag.infrastructure.sentence_transformer_reranker_adapter import reranker_for
from book_graph_rag.ports.evaluation_dataset_port import EvaluationDatasetPort
from book_graph_rag.ports.ragas_runner_port import RAGASRunnerPort


class _SingleRecordDataset(EvaluationDatasetPort):
    """Serves exactly one record under the dataset id the layer asks for."""

    def __init__(self, record: dict[str, Any]) -> None:
        self._record = record

    def load(self, dataset_id: str) -> EvaluationDataset:
        return EvaluationDataset(dataset_id=dataset_id, records=(self._record,))

    def list_datasets(self) -> tuple[str, ...]:
        return ("generation_dataset",)


class _RagasOff(RAGASRunnerPort):
    """Skips RAGAS: its subprocess is noise here and takes its own minute per question."""

    async def run(
        self,
        *,
        dataset_id: str,
        generation_results: tuple[tuple[str, str, tuple[str, ...]], ...],
        previous_metrics: RAGASSecondaryMetrics | None = None,
    ) -> RAGASSecondaryMetrics:
        return RAGASSecondaryMetrics(available=False, notes="skipped by the diagnostic")


async def _run(
    settings: Settings, only: Sequence[str], start_from: str | None, limit: int | None
) -> int:
    dataset = JsonlManifestEvaluationDatasetLoader(settings.evaluation_manifest_path).load(
        "generation_dataset"
    )
    records: list[dict[str, Any]] = [dict(record) for record in dataset.records]
    if only:
        wanted = set(only)
        records = [r for r in records if str(r.get("question_id")) in wanted]
    elif start_from:
        ids = [str(r.get("question_id")) for r in records]
        records = records[ids.index(start_from) :]
    if limit is not None:
        records = records[:limit]
    if not records:
        print("no records selected")
        return 1

    # One set of adapters for the whole run, exactly as the composition root builds them.
    retrieval = Neo4jRetrievalAdapter(settings, reranker=reranker_for(settings))
    claim_port = LLMClaimValidator(settings)
    pairwise_port = LLMPairwiseJudge(settings)
    baseline_port = JsonEvaluationBaselineLoader(settings.evaluation_baseline_dir)

    failures: list[tuple[str, str]] = []
    started = time.time()
    try:
        for index, record in enumerate(records, start=1):
            question_id = str(record.get("question_id", ""))
            question = str(record.get("question", ""))
            print(f"[{index}/{len(records)}] {question_id}: {question[:70]}", flush=True)
            per_question = time.time()
            use_case = EvaluateGenerationLayerUseCase(
                dataset_port=_SingleRecordDataset(record),
                retrieval_port=retrieval,
                claim_port=claim_port,
                pairwise_port=pairwise_port,
                ragas_port=_RagasOff(),
                baseline_port=baseline_port,
                settings=settings,
                run_id=f"diagnose-{question_id}",
            )
            try:
                result = await use_case.execute(run_ragas=False)
            except Exception as exc:  # noqa: BLE001 - the whole point is to keep going
                failures.append((question_id, f"{type(exc).__name__}: {exc}"))
                print(f"    FAILED after {time.time() - per_question:.0f}s", flush=True)
                traceback.print_exc()
                continue
            grounding = result.answer_grounding.questions[0] if result.answer_grounding else None
            metrics = {metric.name: metric.value for metric in result.project_owned_metrics}
            detail = (
                f"contexts={grounding.contexts_provided} ids={grounding.contexts_with_ids} "
                f"cited={grounding.cited_context_share} term={grounding.term_grounding:.3f}"
                if grounding
                else "no grounding"
            )
            print(
                f"    {result.status.value} in {time.time() - per_question:.0f}s | "
                f"faithfulness={metrics.get('faithfulness')} "
                f"pairwise={metrics.get('pairwise_win_rate')} | {result.rationale} | {detail}",
                flush=True,
            )
    finally:
        await retrieval.close()

    print(f"\ntotal {time.time() - started:.0f}s for {len(records)} question(s)")
    if failures:
        print(f"FAILURES ({len(failures)}):")
        for question_id, message in failures:
            print(f"  {question_id}: {message}")
        return 1
    print("no failures")
    return 0


def main() -> int:
    """Entrypoint for the per-question generation-layer diagnostic."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", nargs="+", default=None, help="Question ids to run.")
    parser.add_argument("--from", dest="start_from", default=None, help="First question id to run.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum number of questions.")
    args = parser.parse_args()
    settings = Settings.model_validate({})
    return asyncio.run(_run(settings, args.only or (), args.start_from, args.limit))


if __name__ == "__main__":
    raise SystemExit(main())
