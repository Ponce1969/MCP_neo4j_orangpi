"""Run RAGAS evaluation against the book-graph-rag system.

Two evaluation paths (matching the dataset's ``type`` field):

* ``global`` → ``GlobalQueryUseCase.ask()`` → NL answer + summary contexts
  → evaluated by RAGAS directly.
* ``local``  → ``search_chunks`` + ``find_entity`` → structured contexts
  → a proxy LLM composes an NL answer → evaluated by RAGAS.

The script wires the same adapters as the MCP server (*composition root* pattern)
so it tests the real retrieval pipeline, not mocks.

Produces ``docs/benchmarks/gr3_baseline.json`` with raw metrics and
per-question results. All intermediate tuples are preserved in
``evaluation_results.jsonl`` for reproducibility.

Usage::

    uv sync --group dev          # install ragas + langchain-openai once
    uv run python scripts/run_ragas_evaluation.py
    uv run python scripts/run_ragas_evaluation.py --no-ragas  # skip metric calc
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import click

from book_graph_rag.application.global_query_use_case import GlobalQueryUseCase
from book_graph_rag.config import Settings
from book_graph_rag.infrastructure.community_adapter import Neo4jCommunityAdapter
from book_graph_rag.infrastructure.llm_adapter import LLMAdapter
from book_graph_rag.infrastructure.neo4j_query_adapter import Neo4jQueryAdapter
from book_graph_rag.ports.community_read_port import CommunityReadPort

from pydantic import BaseModel


class _ProxyAnswer(BaseModel):
    """Lightweight model so instructor can return a raw completion as a string."""

    answer: str


# ── Constants ──────────────────────────────────────────────────────────────

_DEFAULT_DATASET = "evaluation_dataset.jsonl"
_RESULTS_OUTPUT = "evaluation_results.jsonl"
_BENCHMARK_DIR = Path("docs") / "benchmarks"
_BASELINE_OUTPUT = _BENCHMARK_DIR / "gr3_baseline.json"


# ── Helpers ────────────────────────────────────────────────────────────────


def _load_dataset(path: str) -> list[dict[str, str]]:
    """Load the curated evaluation dataset (one JSON object per line)."""
    samples: list[dict[str, str]] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                samples.append(json.loads(line))
    return samples


async def _proxy_compose(
    llm_adapter: LLMAdapter, question: str, contexts: list[str]
) -> str:
    """Generate a short NL answer from retrieved contexts.

    This is the proxy for entity-level questions — the real system returns
    structured JSON, so a lightweight LLM call bridges the gap for RAGAS.
    """
    joined = "\n---\n".join(contexts[:15])  # cap at 15 contexts to stay in window
    prompt = (
        f"Question: {question}\n\n"
        f"Relevant contexts:\n{joined}\n\n"
        "Answer the question concisely based ONLY on the contexts above. "
        "If the answer cannot be found, say so."
    )
    response = await llm_adapter._client.chat.completions.create(  # noqa: SLF001
        model=llm_adapter._settings.llm_model_name,  # noqa: SLF001
        response_model=_ProxyAnswer,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.answer


# ── Evaluation paths ───────────────────────────────────────────────────────


async def _evaluate_global(
    question: str,
    global_uc: GlobalQueryUseCase,
    community_read: CommunityReadPort,
    detail_level: int,
) -> tuple[str, list[str]]:
    """Answer a global question via map-reduce community summaries."""
    result = await global_uc.ask(question, detail_level)
    answer: str = result["answer"]
    citation_ids: list[str] = result["citations"]

    # Resolve citation ids to actual summary texts for RAGAS contexts.
    summaries = await community_read.get_summaries_by_level(detail_level)
    summary_map = {s.id: s.summary for s in summaries}
    contexts = [summary_map[cid] for cid in citation_ids if cid in summary_map]
    return answer, contexts


async def _evaluate_local(
    question: str,
    query_adapter: Neo4jQueryAdapter,
    llm_adapter: LLMAdapter,
    chunk_limit: int = 10,
) -> tuple[str, list[str]]:
    """Answer a local question via chunk + entity search + proxy compose."""
    chunks, entities = await asyncio.gather(
        query_adapter.search_chunks(question, limit=chunk_limit),
        query_adapter.find_entity(question, None),
        return_exceptions=True,
    )

    contexts: list[str] = []
    if isinstance(chunks, list):
        contexts.extend(
            c.get("text", "") for c in chunks if isinstance(c, dict) and c.get("text")
        )
    if isinstance(entities, list):
        contexts.extend(
            f"{e.entity.name}: {e.entity.description}"
            for e in entities
            if hasattr(e, "entity") and e.entity.description
        )

    answer = await _proxy_compose(llm_adapter, question, contexts)
    return answer, contexts


# ── RAGAS integration ──────────────────────────────────────────────────────


def _run_ragas(
    results: list[dict[str, Any]],
    settings: Settings,
) -> Any:  # ragas returns EvaluationResult (a dict-like dataclass)
    """Compute Faithfulness, AnswerRelevancy, and ContextPrecision.

    Returns a dict with per-metric averages and per-question scores.
    Requires ``ragas`` and ``langchain-openai`` in the dev group.
    """
    try:
        from datasets import Dataset
        from langchain_openai import ChatOpenAI
        from ragas import evaluate
        from ragas.llms import LangchainLLMWrapper
        from ragas.metrics import (
            AnswerRelevancy,
            ContextPrecision,
            Faithfulness,
        )
    except ImportError as exc:
        click.echo(
            f"RAGAS not available ({exc}).  Run `uv sync --group dev` and "
            "try again, or pass --no-ragas to skip metric computation.",
            err=True,
        )
        sys.exit(1)

    api_key: str = (
        settings.llm_api_key.get_secret_value()
        if settings.llm_api_key is not None
        else "ollama"
    )
    eval_llm = LangchainLLMWrapper(
        ChatOpenAI(
            model=settings.llm_model_name,
            base_url=settings.llm_base_url,
            api_key=api_key,  # type: ignore[arg-type]
            temperature=0,
        )
    )

    dataset = Dataset.from_list(
        [
            {
                "user_input": r["question"],
                "response": r["answer"],
                "retrieved_contexts": r["contexts"],
            }
            for r in results
        ]
    )

    metrics = [Faithfulness(), AnswerRelevancy(), ContextPrecision()]
    for m in metrics:
        m.llm = eval_llm

    return evaluate(dataset, metrics=metrics)


# ── Main ───────────────────────────────────────────────────────────────────


@click.command()
@click.option(
    "--dataset", "-d",
    default=_DEFAULT_DATASET,
    help="Path to the curated evaluation dataset (JSONL)",
)
@click.option(
    "--detail-level", "-l",
    default=1,
    type=click.IntRange(0, 3),
    help="Community summary level for global questions (0-3)",
)
@click.option(
    "--no-ragas", is_flag=True,
    help="Skip RAGAS metric computation (only generate result tuples)",
)
def main(dataset: str, detail_level: int, no_ragas: bool) -> None:
    """Run the RAGAS evaluation pipeline."""
    settings = Settings()  # type: ignore[call-arg]

    click.echo(f"Loading dataset: {dataset}")
    samples = _load_dataset(dataset)
    click.echo(f"  {len(samples)} questions")

    # ── Composition root (same wiring as mcp_server_main.py) ────────────
    query_adapter = Neo4jQueryAdapter(settings)
    community_adapter = Neo4jCommunityAdapter(settings)
    llm_adapter = LLMAdapter(settings)
    global_uc = GlobalQueryUseCase(
        read_port=community_adapter,
        llm_port=llm_adapter,
        max_concurrency=settings.summary_max_concurrency,
    )

    async def _eval_all() -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        total = len(samples)
        for i, sample in enumerate(samples, 1):
            qtype = sample["type"]
            question = sample["question"]
            click.echo(
                f"[{i}/{total}] {qtype}: {question[:80]}{'…' if len(question) > 80 else ''}"
            )

            if qtype == "global":
                answer, contexts = await _evaluate_global(
                    question, global_uc, community_adapter, detail_level,
                )
            else:
                answer, contexts = await _evaluate_local(
                    question, query_adapter, llm_adapter,
                )

            results.append(
                {
                    **sample,
                    "answer": answer,
                    "contexts": contexts,
                    "context_count": len(contexts),
                }
            )
        return results

    try:
        results = asyncio.run(_eval_all())
    finally:
        asyncio.run(community_adapter.close())
        asyncio.run(query_adapter.close())

    # Persist raw tuples for reproducibility.
    with open(_RESULTS_OUTPUT, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    click.echo(f"\nRaw results saved: {_RESULTS_OUTPUT}")

    # ── RAGAS metrics ──────────────────────────────────────────────────
    if no_ragas:
        click.echo("Skipping RAGAS (--no-ragas).")
        return

    click.echo("Computing RAGAS metrics…")
    score = _run_ragas(results, settings)

    _BENCHMARK_DIR.mkdir(parents=True, exist_ok=True)
    baseline: dict[str, Any] = {
        "dataset": dataset,
        "detail_level": detail_level,
        "total_questions": len(results),
        "global_questions": sum(1 for r in results if r["type"] == "global"),
        "local_questions": sum(1 for r in results if r["type"] == "local"),
        "metrics": score,
    }
    with open(_BASELINE_OUTPUT, "w", encoding="utf-8") as f:
        json.dump(baseline, f, ensure_ascii=False, indent=2)
    click.echo(f"Baseline saved: {_BASELINE_OUTPUT}")

    # Print quick summary.
    click.echo("\n── RAGAS Baseline ──")
    for metric_name, value in baseline["metrics"].items():
        click.echo(f"  {metric_name}: {value}")


if __name__ == "__main__":
    main()
