"""CLI entry point for the hybrid evaluation harness.

This script lives outside ``src/book_graph_rag`` so it can import infrastructure
adapters while the ``EvaluationHarness`` class remains hexagonally pure.
"""

from __future__ import annotations

import argparse
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

from book_graph_rag.application.evaluation_harness import EvaluationHarness
from book_graph_rag.config import Settings
from book_graph_rag.domain.evaluation_models import EvaluationMetrics
from book_graph_rag.domain.s4_band_assignment import BandThresholds
from book_graph_rag.infrastructure.brute_force_candidate_retrieval import (
    BruteForceCandidateRetrieval,
)
from book_graph_rag.infrastructure.sentence_transformer_adapter import (
    SentenceTransformerAdapter,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate a semantic entity resolution model against the labeled dataset"
    )
    parser.add_argument(
        "--model",
        required=True,
        help="Embedding model id (local path or Hugging Face hub id)",
    )
    parser.add_argument(
        "--variant",
        choices=("A", "B"),
        default="A",
        help="Embedding input variant: A = names+aliases, B = names+aliases+descriptions",
    )
    parser.add_argument(
        "--emit",
        type=Path,
        default=Path("tests/fixtures/resolution/evaluation_metrics.json"),
        help="Path to write the EvaluationMetrics JSON",
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("tests/fixtures/resolution/pairs.yaml"),
        help="Path to pairs.yaml",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("tests/fixtures/resolution/manifest.json"),
        help="Path to manifest.json",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=Path("tests/fixtures/resolution/baseline_report.json"),
        help="Path to baseline_report.json",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=20,
        help="Candidate retrieval top-k",
    )
    parser.add_argument(
        "--min-similarity",
        type=float,
        default=0.0,
        help="Candidate retrieval minimum cosine similarity",
    )
    args = parser.parse_args(argv)

    settings = Settings.model_validate({})
    embedding = SentenceTransformerAdapter(settings)
    retrieval = BruteForceCandidateRetrieval()

    harness = EvaluationHarness(
        embedding=embedding,
        retrieval=retrieval,
        thresholds=BandThresholds(),
        dataset_path=args.dataset,
        manifest_path=args.manifest,
        baseline_path=args.baseline,
        output_path=args.emit,
        top_k=args.top_k,
        min_similarity=args.min_similarity,
    )

    metrics = run_sync(harness.evaluate(model_id=args.model, input_variant=args.variant))
    gate = harness.compare_to_baseline(metrics)

    print(f"model={metrics.model_id} variant={metrics.input_variant}")
    print(f"retrieval F1={metrics.retrieval_f1:.4f}")
    print(f"hard over-merge={metrics.hard_over_merge_rate:.4f}")
    print(f"multilingual under-merge={metrics.multilingual_under_merge_rate:.4f}")
    print(f"gate passed={gate.passed}")
    print(f"metrics written to {args.emit}")

    if not gate.passed:
        return 1
    return 0


def run_sync(coro: Coroutine[Any, Any, EvaluationMetrics]) -> EvaluationMetrics:
    """Run an async coroutine from a synchronous CLI context."""
    import asyncio

    return asyncio.run(coro)


if __name__ == "__main__":
    raise SystemExit(main())
