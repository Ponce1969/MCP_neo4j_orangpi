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
import warnings
from pathlib import Path
from typing import Any

import click
from pydantic import BaseModel

from book_graph_rag.application.global_query_use_case import GlobalQueryUseCase
from book_graph_rag.config import Settings
from book_graph_rag.infrastructure.community_adapter import Neo4jCommunityAdapter
from book_graph_rag.infrastructure.llm_adapter import (
    LLMAdapter,
    _escape_json_string_control_chars,
)
from book_graph_rag.infrastructure.neo4j_query_adapter import Neo4jQueryAdapter
from book_graph_rag.ports.community_read_port import CommunityReadPort


class _ProxyAnswer(BaseModel):
    """Lightweight model so instructor can return a raw completion as a string."""

    answer: str


# ── Constants ──────────────────────────────────────────────────────────────

_DEFAULT_DATASET = "evaluation_dataset.jsonl"
_RESULTS_OUTPUT = "evaluation_results.jsonl"
_BENCHMARK_DIR = Path("docs") / "benchmarks"
_BASELINE_OUTPUT = _BENCHMARK_DIR / "gr3_baseline.json"

# Local embedding model for AnswerRelevancy: DeepSeek offers no embeddings
# API, and a remote embedder would add rate-limit flakiness to a baseline we
# re-run after every change. all-MiniLM-L6-v2 is small, CPU-only, and stable.
_EMBEDDINGS_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


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


async def _proxy_compose(llm_adapter: LLMAdapter, question: str, contexts: list[str]) -> str:
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
        contexts.extend(c.get("text", "") for c in chunks if isinstance(c, dict) and c.get("text"))
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

    ragas 0.4.3 note: ``evaluate()`` only accepts v1 ``Metric`` objects — the
    v2 ``collections`` metrics fail its isinstance gate — so we import the v1
    classes (the same ones behind the deprecated public re-exports, hence the
    targeted warning filter) and pair them with a langchain-compatible local
    embedding model (v1 AnswerRelevancy calls embed_query/embed_documents).

    Requires ``ragas``, ``langchain-openai`` and ``langchain-huggingface`` in
    the dev group.
    """
    try:
        from datasets import Dataset
        from langchain_huggingface import HuggingFaceEmbeddings as LangchainHFEmbeddings
        from langchain_openai import ChatOpenAI
        from ragas import evaluate
        from ragas.llms import LangchainLLMWrapper
        from ragas.run_config import RunConfig
    except ImportError as exc:
        click.echo(
            f"RAGAS not available ({exc}).  Run `uv sync --group dev` and "
            "try again, or pass --no-ragas to skip metric computation.",
            err=True,
        )
        sys.exit(1)

    api_key: str = (
        settings.llm_api_key.get_secret_value() if settings.llm_api_key is not None else "ollama"
    )

    class _SanitizingChatOpenAI(ChatOpenAI):
        """ChatOpenAI whose outputs escape raw control chars before ragas parses them.

        RAGAS metrics parse the LLM's JSON with strict parsers; the same
        NIM/model defect that broke instructor calls (raw newlines inside JSON
        string values) would break metric scoring. Escaping before ragas sees
        the text makes the metric LLM as robust as the retrieval one.
        """

        def _generate(
            self,
            messages: list[Any],
            stop: list[str] | None = None,
            run_manager: Any = None,
            **kwargs: Any,
        ) -> Any:
            result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
            for generation_list in result.generations:
                for generation in generation_list:
                    # langchain types generations loosely; .text is the payload
                    raw_text = generation.text  # type: ignore[attr-defined]
                    generation.text = _escape_json_string_control_chars(  # type: ignore[attr-defined]
                        raw_text
                    )
            return result

    with warnings.catch_warnings():
        # The public ragas.metrics names are the v1 classes evaluate() accepts;
        # they are re-exported with a deprecation warning we do not need.
        warnings.filterwarnings(
            "ignore",
            message=r"Importing .* from 'ragas\.metrics' is deprecated.*",
        )
        from ragas.metrics import (  # noqa: F811 — re-import under the filter
            AnswerRelevancy as _AnswerRelevancy,
        )
        from ragas.metrics import (
            Faithfulness as _Faithfulness,
        )
        from ragas.metrics import (
            LLMContextPrecisionWithoutReference as _LLMContextPrecisionWithoutReference,
        )

    eval_llm = LangchainLLMWrapper(
        _SanitizingChatOpenAI(
            model=settings.llm_model_name,
            base_url=settings.llm_base_url,
            api_key=api_key,  # type: ignore[arg-type]
            temperature=0,
            timeout=300.0,
            max_retries=2,
        )
    )

    try:
        embeddings = LangchainHFEmbeddings(model_name=_EMBEDDINGS_MODEL)
    except ImportError as exc:
        click.echo(
            f"Local embeddings backend not available ({exc}).  Run "
            "`uv sync --group dev` to install langchain-huggingface and "
            "try again.",
            err=True,
        )
        sys.exit(1)

    metric_rows = [r for r in results if r.get("answer") is not None]
    if not metric_rows:
        click.echo(
            f"All {len(results)} questions failed — nothing to score. "
            "Check the per-question errors above.",
            err=True,
        )
        return None

    dataset = Dataset.from_list(
        [
            {
                "user_input": r["question"],
                "response": r["answer"],
                "retrieved_contexts": r["contexts"],
            }
            for r in metric_rows
        ]
    )

    # ContextPrecision (with reference) demands a ``reference`` ground-truth
    # column, which the curated dataset does not carry. Synthesizing references
    # from the same retrieval would be circular and inflate precision; the
    # without-reference variant judges context relevance against the question
    # alone and is the honest baseline while no human reference set exists.
    metrics = [
        _Faithfulness(llm=eval_llm),
        _AnswerRelevancy(llm=eval_llm, embeddings=embeddings),
        _LLMContextPrecisionWithoutReference(llm=eval_llm),
    ]

    # ragas's own typing: v2 collections metrics are BaseMetric while evaluate()
    # annotates Sequence[Metric]; the classes it accepts are exactly these.
    return evaluate(
        dataset,
        metrics=metrics,
        # Slow-but-successful deepseek responses must not be killed by the
        # executor's default 180s timeout, and 16 parallel n=3 calls would
        # saturate the API from a home connection.
        run_config=RunConfig(timeout=600, max_retries=3, max_wait=90, max_workers=4),
    )


# ── Main ───────────────────────────────────────────────────────────────────


@click.command()
@click.option(
    "--dataset",
    "-d",
    default=_DEFAULT_DATASET,
    help="Path to the curated evaluation dataset (JSONL)",
)
@click.option(
    "--detail-level",
    "-l",
    default=1,
    type=click.IntRange(0, 3),
    help="Community summary level for global questions (0-3)",
)
@click.option(
    "--no-ragas",
    is_flag=True,
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
            click.echo(f"[{i}/{total}] {qtype}: {question[:80]}{'…' if len(question) > 80 else ''}")

            try:
                if qtype == "global":
                    answer, contexts = await _evaluate_global(
                        question,
                        global_uc,
                        community_adapter,
                        detail_level,
                    )
                else:
                    answer, contexts = await _evaluate_local(
                        question,
                        query_adapter,
                        llm_adapter,
                    )
            except Exception as exc:  # noqa: BLE001 — one bad question must not kill the batch
                click.echo(f"  FAILED: {type(exc).__name__}: {exc}", err=True)
                results.append(
                    {
                        **sample,
                        "answer": None,
                        "contexts": [],
                        "error": str(exc),
                    }
                )
                continue

            results.append(
                {
                    **sample,
                    "answer": answer,
                    "contexts": contexts,
                    "context_count": len(contexts),
                }
            )
        return results

    async def _run() -> list[dict[str, Any]]:
        try:
            return await _eval_all()
        finally:
            await community_adapter.close()
            await query_adapter.close()

    results = asyncio.run(_run())

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

    failed_rows = [r for r in results if r.get("error")]

    _BENCHMARK_DIR.mkdir(parents=True, exist_ok=True)
    baseline: dict[str, Any] = {
        "dataset": dataset,
        "detail_level": detail_level,
        "total_questions": len(results),
        "global_questions": sum(1 for r in results if r["type"] == "global"),
        "local_questions": sum(1 for r in results if r["type"] == "local"),
        "failed_questions": [{"question": r["question"], "error": r["error"]} for r in failed_rows],
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
