"""Diagnóstico read-only del retrieval: recall@N del fulltext y A/B contra el reranker.

Modos:
  * default — recall@N: para cada pregunta ``current`` del dataset de retrieval corre
    ``search_chunks`` con limit N y reporta qué fracción de sus ``reference_context_ids``
    aparece en el top-N. Responde: ¿los chunks de referencia están en el pool?
  * ``--rerank`` — A/B de precision@k con el MISMO denominador corregido (contextos con
    ``chunk_id``, sin dilución por entidades): fulltext@k vs reranked@k, con las
    referencias que el reranker promueve o degrada. Responde: ¿el reranker mejora sobre
    el fulltext?

Read-only: solo MATCH + índice fulltext y un cross-encoder local. No escribe el grafo.

Uso:
    uv run python scripts/eval_fulltext_recall.py                 # recall@10/30/50
    uv run python scripts/eval_fulltext_recall.py 40              # recall@40
    NEO4J_URI=bolt://localhost:17687 uv run python scripts/eval_fulltext_recall.py --rerank
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from book_graph_rag.config import Settings
from book_graph_rag.infrastructure.evaluation_dataset_loader import (
    JsonlManifestEvaluationDatasetLoader,
)
from book_graph_rag.infrastructure.neo4j_query_adapter import Neo4jQueryAdapter
from book_graph_rag.infrastructure.neo4j_retrieval_adapter import Neo4jRetrievalAdapter
from book_graph_rag.infrastructure.sentence_transformer_reranker_adapter import (
    SentenceTransformerRerankerAdapter,
)

NS: tuple[int, ...] = (10, 30, 50)
GATE_N = 50
GATE_THRESHOLD = 0.75
DATASET_ID = "retrieval_dataset"


def _parse_args(argv: list[str]) -> tuple[tuple[int, ...], bool]:
    """Return (N sweep, rerank-mode). ``--rerank`` switches to the A/B precision mode."""
    rerank = "--rerank" in argv
    numbers = [arg for arg in argv if arg != "--rerank"]
    if not numbers:
        return NS, rerank
    return tuple(int(arg) for arg in numbers), rerank


def _current_records(settings: Settings) -> list[dict[str, Any]]:
    loader = JsonlManifestEvaluationDatasetLoader(settings.evaluation_manifest_path)
    dataset = loader.load(DATASET_ID)
    return [
        r
        for r in dataset.records
        if r.get("corpus") == "current" and r.get("reference_context_ids")
    ]


async def _recall_one(
    adapter: Neo4jQueryAdapter,
    *,
    question: str,
    reference_ids: set[str],
    limit: int,
) -> tuple[int, float]:
    """Return (hits, recall) of ``reference_ids`` inside the top-``limit`` chunks."""
    chunks: list[dict[str, Any]] = await adapter.search_chunks(question, limit)
    retrieved = {c.get("chunk_id") for c in chunks if c.get("chunk_id")}
    hits = len(reference_ids & retrieved)
    return hits, hits / len(reference_ids)


async def _recall_sweep(
    adapter: Neo4jQueryAdapter, records: list[dict[str, Any]], ns: tuple[int, ...]
) -> None:
    print(f"{'question_id':<10} {'refs':>4} " + " ".join(f"{'hits@' + str(n):>7}" for n in ns))
    recalls: dict[int, list[float]] = {n: [] for n in ns}
    for record in records:
        reference_ids = set(record.get("reference_context_ids", []) or [])
        row: list[str] = []
        for n in ns:
            hits, recall = await _recall_one(
                adapter,
                question=str(record.get("question", "")),
                reference_ids=reference_ids,
                limit=n,
            )
            recalls[n].append(recall)
            row.append(f"{hits:>7}")
        print(f"{str(record.get('question_id', '')):<10} {len(reference_ids):>4} " + " ".join(row))

    print()
    means: dict[int, float] = {}
    for n in ns:
        means[n] = sum(recalls[n]) / len(recalls[n]) if recalls[n] else 0.0
        print(f"recall@{n:<3} = {means[n]:.4f}")
    if GATE_N in means:
        verdict = "PASS" if means[GATE_N] >= GATE_THRESHOLD else "FAIL"
        print()
        print(
            f"GATE: recall@{GATE_N} = {means[GATE_N]:.4f} (threshold {GATE_THRESHOLD}) -> {verdict}"
        )
    else:
        print()
        print(f"GATE no evaluado: recall@{GATE_N} no está en el sweep {ns}")


async def _precision_ab(
    adapter: Neo4jQueryAdapter,
    settings: Settings,
    records: list[dict[str, Any]],
) -> None:
    """Fulltext@top_k vs reranked@top_k with the same chunk-only denominator."""
    top_k = settings.retrieval_top_k
    retrieval = Neo4jRetrievalAdapter(
        settings,
        query_adapter=adapter,
        reranker=SentenceTransformerRerankerAdapter(settings),
    )
    print(
        f"fulltext@{top_k} vs reranked@{top_k} (over-fetch {settings.retrieval_overfetch_n}, "
        f"model {settings.reranker_model_id})"
    )
    print(
        f"{'question_id':<10} {'refs':>4} {'ft_hits':>7} {'ft_prec':>8} "
        f"{'rr_hits':>7} {'rr_prec':>8}  refs movidas (promovidas / degradadas)"
    )
    ft_precisions: list[float] = []
    rr_precisions: list[float] = []
    try:
        for record in records:
            question_id = str(record.get("question_id", ""))
            question = str(record.get("question", ""))
            reference_ids = set(record.get("reference_context_ids", []) or [])

            ft_chunks: list[dict[str, Any]] = await adapter.search_chunks(question, top_k)
            ft_ids = {c.get("chunk_id") for c in ft_chunks if c.get("chunk_id")}
            ft_hits = len(reference_ids & ft_ids)
            ft_prec = ft_hits / len(ft_ids) if ft_ids else 0.0
            ft_precisions.append(ft_prec)

            contexts = await retrieval.fetch_contexts(
                question=question, qtype="local", detail_level=1
            )
            rr_ids = {c.chunk_id for c in contexts if c.chunk_id is not None}
            rr_hits = len(reference_ids & rr_ids)
            rr_prec = rr_hits / len(rr_ids) if rr_ids else 0.0
            rr_precisions.append(rr_prec)

            promoted = sorted(reference_ids & rr_ids - ft_ids)
            dropped = sorted(reference_ids & ft_ids - rr_ids)
            print(
                f"{question_id:<10} {len(reference_ids):>4} {ft_hits:>7} {ft_prec:>8.3f} "
                f"{rr_hits:>7} {rr_prec:>8.3f}  {len(promoted)} / {len(dropped)}"
                + (f"  degradadas={dropped}" if dropped else "")
            )
    finally:
        await retrieval.close()

    n = len(ft_precisions)
    mean_ft = sum(ft_precisions) / n if n else 0.0
    mean_rr = sum(rr_precisions) / n if n else 0.0
    print()
    print(f"precision@{top_k} fulltext (denominador corregido) = {mean_ft:.4f}")
    print(f"precision@{top_k} con reranker                     = {mean_rr:.4f}")
    print(f"delta reranker - fulltext                          = {mean_rr - mean_ft:+.4f}")


async def main() -> None:
    settings = Settings.model_validate({})
    ns, rerank = _parse_args(sys.argv[1:])
    records = _current_records(settings)
    print(f"dataset={DATASET_ID} preguntas current con refs: {len(records)}")
    print(f"neo4j_uri={settings.neo4j_uri}")
    print()

    if rerank:
        adapter = Neo4jQueryAdapter(settings)
        try:
            await _precision_ab(adapter, settings, records)
        finally:
            await adapter.close()
        return

    adapter = Neo4jQueryAdapter(settings)
    try:
        await _recall_sweep(adapter, records, ns)
    finally:
        await adapter.close()


if __name__ == "__main__":
    asyncio.run(main())
