"""Diagnóstico read-only del retrieval: recall@N, A/B contra el reranker y simulación híbrida.

Modos:
  * default — recall@N: para cada pregunta ``current`` del dataset de retrieval corre
    ``search_chunks`` con limit N y reporta qué fracción de sus ``reference_context_ids``
    aparece en el top-N. Responde: ¿los chunks de referencia están en el pool?
  * ``--rerank`` — A/B de precision@k con el MISMO denominador corregido (contextos con
    ``chunk_id``, sin dilución por entidades): fulltext@k vs reranked@k, con las
    referencias que el reranker promueve o degrada. Responde: ¿el reranker mejora sobre
    el fulltext?
  * ``--simulate-hybrid[=N]`` — fase 0 del ADR 09 (`docs/spec/09-retrieval-pool-recall.md`):
    embebe los 3468 chunks **en memoria** con el adapter del proyecto, rankea por coseno, corre
    el fulltext real y **fusiona las dos listas con RRF**, todo sin escribir nada. Responde la
    pregunta que decide si la alternativa A se construye: ¿la fusión supera el fulltext puro?
    Criterio de corte, fijado de antemano: si no supera la línea base, **A no se construye**.

Read-only: solo MATCH + índice fulltext y modelos locales. No escribe el grafo.

Uso:
    uv run python scripts/eval_fulltext_recall.py                 # recall@10/30/50
    uv run python scripts/eval_fulltext_recall.py 40              # recall@40
    NEO4J_URI=bolt://localhost:17687 uv run python scripts/eval_fulltext_recall.py --rerank
    NEO4J_URI=bolt://localhost:17687 uv run python scripts/eval_fulltext_recall.py --simulate-hybrid
    ... --simulate-hybrid=50                                      # otro over-fetch para el híbrido
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from neo4j import AsyncGraphDatabase

from book_graph_rag.config import Settings
from book_graph_rag.infrastructure.evaluation_dataset_loader import (
    JsonlManifestEvaluationDatasetLoader,
)
from book_graph_rag.infrastructure.neo4j_query_adapter import Neo4jQueryAdapter
from book_graph_rag.infrastructure.neo4j_retrieval_adapter import Neo4jRetrievalAdapter
from book_graph_rag.infrastructure.sentence_transformer_adapter import SentenceTransformerAdapter
from book_graph_rag.infrastructure.sentence_transformer_reranker_adapter import (
    SentenceTransformerRerankerAdapter,
)
from book_graph_rag.ports.embedding_provider_port import EmbeddingRequest

NS: tuple[int, ...] = (10, 30, 50)
GATE_N = 50
GATE_THRESHOLD = 0.75
DATASET_ID = "retrieval_dataset"
#: Constante clásica de Reciprocal Rank Fusion; sin normalizar scores porque el score de
#: Lucene y el coseno no son comparables (ver el ADR 09).
RRF_K = 60
#: Línea base real y commiteada contra la que se corta (no la de la doc).
COMMITTED_BASELINE = 0.1250
_ALL_CHUNKS_CYPHER = (
    "MATCH (c:Chunk) RETURN c.book_id AS book_id, c.chunk_index AS chunk_index, c.text AS text"
)


def _parse_args(argv: list[str]) -> tuple[tuple[int, ...], str, int | None, tuple[float, float]]:
    """Return (N sweep, modo, over-fetch del híbrido, pesos RRF de (vector, léxico))."""
    mode = "recall"
    if "--rerank" in argv:
        mode = "rerank"
    for arg in argv:
        if arg == "--simulate-hybrid" or arg.startswith("--simulate-hybrid="):
            mode = "hybrid"
    overfetch: int | None = None
    weights: tuple[float, float] = (1.0, 1.0)
    numbers: list[int] = []
    for arg in argv:
        if arg.startswith("--rrf-weights="):
            left, _, right = arg.split("=", 1)[1].partition(",")
            weights = (float(left), float(right))
        elif arg.startswith(("--overfetch=", "--simulate-hybrid=")):
            overfetch = int(arg.split("=", 1)[1])
        elif not arg.startswith("--"):
            numbers.append(int(arg))
    return (tuple(numbers) if numbers else NS), mode, overfetch, weights


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


async def _load_all_chunks(settings: Settings) -> list[tuple[str, str]]:
    """``(chunk_id, text)`` de todo el grafo, con el id compuesto igual que el adapter.

    La regla del id es la de ``Neo4jQueryAdapter._chunk_payload``: el grafo guarda ``book_id``
    y ``chunk_index`` y el id ``{book_id}:{chunk_index}`` se compone desde ellos. Pedir una
    propiedad ``chunk_id`` no encuentra nada (esa propiedad no existe).
    """
    driver = AsyncGraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value()),
    )
    try:
        async with driver.session() as session:
            result = await session.run(_ALL_CHUNKS_CYPHER)
            rows = [dict(record) async for record in result]
    finally:
        await driver.close()

    chunks: list[tuple[str, str]] = []
    for row in rows:
        book_id, index, text = row.get("book_id"), row.get("chunk_index"), row.get("text")
        if book_id is None or index is None or not text:
            continue
        chunks.append((f"{book_id}:{index}", str(text)))
    return chunks


def _dot(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    """Similitud coseno con vectores ya normalizados (el encode del proyecto normaliza)."""
    return sum(a * b for a, b in zip(left, right, strict=True))


def _ranks(ids: list[str]) -> dict[str, int]:
    """Rango 1-based por id; gana la primera aparición (empates deterministas)."""
    ranks: dict[str, int] = {}
    for position, chunk_id in enumerate(ids, start=1):
        ranks.setdefault(chunk_id, position)
    return ranks


def _fuse(
    vector_ids: list[str],
    lexical_ids: list[str],
    top_k: int,
    weights: tuple[float, float] = (1.0, 1.0),
) -> list[str]:
    """Reciprocal Rank Fusion: suma ``peso/(k+rango)`` por lista, sin normalizar scores."""
    scores: dict[str, float] = {}
    for weight, ids in zip(weights, (vector_ids, lexical_ids), strict=True):
        for chunk_id, rank in _ranks(ids).items():
            scores[chunk_id] = scores.get(chunk_id, 0.0) + weight / (RRF_K + rank)
    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    return [chunk_id for chunk_id, _ in ordered[:top_k]]


async def _simulate_hybrid(
    adapter: Neo4jQueryAdapter,
    settings: Settings,
    records: list[dict[str, Any]],
    overfetch: int | None,
    weights: tuple[float, float] = (1.0, 1.0),
) -> None:
    """Fase 0 del ADR 09: ¿la fusión vectores+fulltext supera al fulltext puro?

    Read-only y en memoria: los vectores se calculan acá, la rama vectorial es coseno exacto
    sobre los 3468 chunks (el caso **optimista** frente a un índice ANN real) y la fusión usa
    RRF, que no necesita normalizar el score de Lucene contra un coseno.
    """
    top_k = settings.retrieval_top_k
    n = overfetch or settings.retrieval_overfetch_n
    chunks = await _load_all_chunks(settings)
    ids = [chunk_id for chunk_id, _ in chunks]
    texts = [text for _, text in chunks]
    print(
        f"chunks en el grafo: {len(chunks)} | over-fetch del híbrido N={n} | RRF k={RRF_K} "
        f"| pesos (vector, léxico) = {weights}"
    )

    embedder = SentenceTransformerAdapter(settings)
    print(f"embebiendo con {settings.embedding_model_id} (local, en memoria)…")
    batch = await embedder.embed(
        EmbeddingRequest(texts=tuple(texts), model_id=settings.embedding_model_id)
    )
    matrix = [vector.values for vector in batch.vectors]
    print(f"  vectores: {len(matrix)} × {len(matrix[0]) if matrix else 0} dimensiones")
    print()
    print(
        f"{'question_id':<10} {'refs':>4} {'ft_hits':>7} {'ft_prec':>8} "
        f"{'hy_hits':>7} {'hy_prec':>8}  promovidas / degradadas"
    )
    ft_precisions: list[float] = []
    hy_precisions: list[float] = []
    promoted_total: list[str] = []
    try:
        for record in records:
            question_id = str(record.get("question_id", ""))
            question = str(record.get("question", ""))
            reference_ids = set(record.get("reference_context_ids", []) or [])

            query_batch = await embedder.embed(
                EmbeddingRequest(texts=(question,), model_id=settings.embedding_model_id)
            )
            query_vector = query_batch.vectors[0].values
            scored = sorted(
                (
                    (_dot(query_vector, row), chunk_id)
                    for row, chunk_id in zip(matrix, ids, strict=True)
                ),
                key=lambda item: (-item[0], item[1]),
            )
            vector_ids = [chunk_id for _, chunk_id in scored[:n]]

            lexical_hits = await adapter.search_chunks(question, n)
            lexical_ids = [
                str(chunk.get("chunk_id")) for chunk in lexical_hits if chunk.get("chunk_id")
            ]

            fused = _fuse(vector_ids, lexical_ids, top_k, weights)
            ft_ids = set(lexical_ids[:top_k])
            ft_hits = len(reference_ids & ft_ids)
            hy_hits = len(reference_ids & set(fused))
            ft_prec = ft_hits / len(ft_ids) if ft_ids else 0.0
            hy_prec = hy_hits / len(fused) if fused else 0.0
            ft_precisions.append(ft_prec)
            hy_precisions.append(hy_prec)
            promoted = sorted(reference_ids & set(fused) - ft_ids)
            dropped = sorted(reference_ids & ft_ids - set(fused))
            promoted_total.extend(promoted)
            print(
                f"{question_id:<10} {len(reference_ids):>4} {ft_hits:>7} {ft_prec:>8.3f} "
                f"{hy_hits:>7} {hy_prec:>8.3f}  {len(promoted)} / {len(dropped)}"
                + (f"  degradadas={dropped}" if dropped else "")
            )
    finally:
        await adapter.close()

    count = len(ft_precisions)
    mean_ft = sum(ft_precisions) / count if count else 0.0
    mean_hy = sum(hy_precisions) / count if count else 0.0
    print()
    print(f"precision@{top_k} fulltext (control, mismo denominador) = {mean_ft:.4f}")
    print(f"precision@{top_k} híbrido (vectores + fulltext, RRF)    = {mean_hy:.4f}")
    print(f"delta híbrido - fulltext                              = {mean_hy - mean_ft:+.4f}")
    print(f"referencias que el híbrido trae y el fulltext no      = {sorted(set(promoted_total))}")
    print()
    if mean_hy > mean_ft and mean_hy > COMMITTED_BASELINE:
        print(
            f"CORTE (>{COMMITTED_BASELINE} y > control): PASS — la alternativa A queda "
            "justificada para construir (fase 1 del ADR)"
        )
    else:
        print(
            f"CORTE (>{COMMITTED_BASELINE} y > control): FAIL — por el criterio del ADR, "
            "A NO se construye"
        )


async def main() -> None:
    settings = Settings.model_validate({})
    ns, mode, overfetch, weights = _parse_args(sys.argv[1:])
    records = _current_records(settings)
    print(f"dataset={DATASET_ID} preguntas current con refs: {len(records)}")
    print(f"neo4j_uri={settings.neo4j_uri}")
    print()

    adapter = Neo4jQueryAdapter(settings)
    if mode == "rerank":
        try:
            await _precision_ab(adapter, settings, records)
        finally:
            await adapter.close()
        return
    if mode == "hybrid":
        await _simulate_hybrid(adapter, settings, records, overfetch, weights)
        return
    try:
        await _recall_sweep(adapter, records, ns)
    finally:
        await adapter.close()


if __name__ == "__main__":
    asyncio.run(main())
