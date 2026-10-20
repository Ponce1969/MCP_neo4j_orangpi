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
  * ``--shape-ab`` — fase 2 (S2-S4 de `odd/tasks/retrieval-phase2-lexical.md`): A/B del shape
    pre-registrado — control = pregunta cruda (**verbatim**, byte a byte lo que hoy se envía),
    variante = la misma pregunta **escapada para Lucene y envuelta en una frase** vía
    ``_shape_query``. Es un transform *caller-side*: el query string se compone en este repo,
    no en el adapter. Sin el escape, una comilla interna cerraría la frase y un operador
    booleano sería sintaxis real de Lucene — se mediría un error de sintaxis, no retrieval.

Corte (S2, fijado por el operador antes de cualquier corrida, ver Log del task doc): las
variantes de fase 2 miden contra la línea base **commiteada** en
``data/evaluation/retrieval_baseline.json`` (leída, nunca hardcodeada) con la fila de control
dentro de la misma corrida (S3): si el control no la reproduce exacto, la corrida es **VOID**
y los números de la variante no se leen. La variante shippea solo con **>= +1 referencia neta
sin perder ninguna** — ganancia monótona. El presupuesto de latencia es el p95 del camino de
grafo que reporta el gate (`latency_p95_graph_floor_ms`, S6), no el envelope de MCP.

Read-only: solo MATCH + índice fulltext y modelos locales. No escribe el grafo.

Uso:
    uv run python scripts/eval_fulltext_recall.py                 # recall@10/30/50
    uv run python scripts/eval_fulltext_recall.py 40              # recall@40
    NEO4J_URI=bolt://localhost:17687 uv run python scripts/eval_fulltext_recall.py --rerank
    NEO4J_URI=bolt://localhost:17687 uv run python scripts/eval_fulltext_recall.py --simulate-hybrid
    ... --simulate-hybrid=50                                      # otro over-fetch para el híbrido
    NEO4J_URI=bolt://localhost:17687 uv run python scripts/eval_fulltext_recall.py --shape-ab
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from neo4j import AsyncGraphDatabase

from book_graph_rag.config import Settings
from book_graph_rag.infrastructure.evaluation_baseline_loader import JsonEvaluationBaselineLoader
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
#: Objetivo formal del retrieval (sin margen: son las 3 referencias fuera del top-10).
OBJECTIVE_PRECISION_AT_K = 0.150
#: Corte S2: una referencia neta minima para shippear (+1/120 con el dataset conmutado).
_S2_MIN_NET_REFERENCES = 1
#: Tolerancia al comparar la fila de control con la linea base (15/120 es 0.125 exacto en
#: coma flotante; la tolerancia solo absorbe el redondeo de la media de precisiones).
_BASELINE_TOLERANCE = 1e-9
#: Especiales del classic query parser de Lucene. Escaparlos es lo que hace parseable la
#: query deformada: en todo este repo la pregunta llegaba VERBATIM a
#: ``db.index.fulltext.queryNodes`` — no existia ningun escape (S4).
_LUCENE_SPECIALS = frozenset('+-&|!(){}[]^"~*?:\\/')
_ALL_CHUNKS_CYPHER = (
    "MATCH (c:Chunk) RETURN c.book_id AS book_id, c.chunk_index AS chunk_index, c.text AS text"
)


def _escape_lucene(text: str) -> str:
    """Backslash-escape every Lucene special so the text carries no syntax."""
    return "".join(f"\\{char}" if char in _LUCENE_SPECIALS else char for char in text)


def _shape_query(question: str) -> str:
    """The single pre-registered shape variant (S2): the escaped question as one phrase.

    Escaped FIRST (S4): unescaped, an inner quote would close the phrase and a boolean
    operator in the question would become real Lucene syntax — the run would measure a
    syntax error, not retrieval quality.
    """
    return f'"{_escape_lucene(question)}"'


def _chunk_id_list(chunks: list[dict[str, Any]], *, question_id: str, leg: str) -> list[str]:
    """Ordered chunk ids, with the gate's denominator shape armored (S7b).

    The gate (``EvaluateRetrievalLayerUseCase``) divides by ``len(list)`` of chunk-bearing
    contexts while this script historically divided by ``len(set)`` of ids. The two are
    equal iff ids are unique per question, so uniqueness is verified per leg and a
    duplicate aborts the run instead of silently splitting the two instruments.
    """
    ids = [str(chunk["chunk_id"]) for chunk in chunks if chunk.get("chunk_id")]
    if len(ids) != len(set(ids)):
        duplicates = sorted({chunk_id for chunk_id in ids if ids.count(chunk_id) > 1})
        raise SystemExit(
            f"DENOMINATOR DRIFT: duplicate chunk ids in the {leg} leg of {question_id}: "
            f"{duplicates}. The gate divides by len(list) and the script by len(set); "
            "the run is not comparable until ids are unique per question."
        )
    return ids


def _committed_precision_baseline(baseline_dir: Path) -> float:
    """The number the cut criterion applies to, read from the committed artifact (S7a).

    The script used to carry its own ``COMMITTED_BASELINE = 0.1250`` — a second source of
    truth that could drift from ``data/evaluation/retrieval_baseline.json``. Without the
    artifact there is nothing to cut against: fail loudly, never invent a number.
    """
    report = JsonEvaluationBaselineLoader(baseline_dir).load("retrieval")
    if report is None:
        raise SystemExit(
            f"no committed retrieval baseline in {baseline_dir}; commit the artifact "
            "instead of hardcoding a number (S7a)"
        )
    baseline = report.metrics.get("precision_at_k")
    if baseline is None:
        raise SystemExit(f"{baseline_dir / 'retrieval_baseline.json'} has no precision_at_k")
    return baseline


@dataclass(frozen=True)
class _CutDecision:
    """S2's cut criterion, computed once and printed by ``_print_s2_cut``."""

    control_reproduced: bool
    net_references: int
    lost_references: int
    decision: Literal["void", "ship", "hold"]


def _cut_decision(
    *,
    baseline: float,
    control_precision: float,
    control_hits: int,
    variant_hits: int,
    lost_references: int,
) -> _CutDecision:
    """S2 (the operator's verbatim rule): ship only on a monotonic gain.

    The variant ships iff the in-run control reproduces the committed baseline (S3 canary;
    otherwise the run is VOID), it beats that baseline by at least one net reference, and
    it loses no current reference. A gain that cancels with a loss is not an improvement.
    Failing the cut means option C stays and there is no third attempt.
    """
    reproduced = abs(control_precision - baseline) <= _BASELINE_TOLERANCE
    net = variant_hits - control_hits
    if not reproduced:
        decision: Literal["void", "ship", "hold"] = "void"
    elif lost_references == 0 and net >= _S2_MIN_NET_REFERENCES:
        decision = "ship"
    else:
        decision = "hold"
    return _CutDecision(
        control_reproduced=reproduced,
        net_references=net,
        lost_references=lost_references,
        decision=decision,
    )


def _print_s2_cut(
    decision: _CutDecision,
    *,
    baseline: float,
    control_precision: float,
    variant_precision: float,
    top_k: int,
) -> None:
    """Print the control reproduction and S2's ship / hold / void verdict."""
    reproduction = "reproduces" if decision.control_reproduced else "DOES NOT REPRODUCE"
    print()
    print(
        f"control row: precision@{top_k} = {control_precision:.4f} vs committed baseline "
        f"{baseline:.4f} -> {reproduction}"
    )
    if decision.decision == "void":
        print(
            "RUN VOID: the control row did not reproduce the committed baseline. It is the "
            "canary for measurement drift (S3), not a result to interpret — the variant "
            "numbers of this run must not be read."
        )
        return
    print(
        f"variant row: precision@{top_k} = {variant_precision:.4f} "
        f"(net {decision.net_references:+d} reference(s), lost {decision.lost_references})"
    )
    if variant_precision >= OBJECTIVE_PRECISION_AT_K:
        print("objective 0.150 met")
    else:
        print("objective 0.150 still unmet (context, not the ship condition)")
    rule = (
        "S2 cut (>= +1 net reference over the baseline, zero references lost, "
        "control reproduces the baseline)"
    )
    if decision.decision == "ship":
        print(
            f"{rule}: SHIP — the variant is ship-eligible. Shipping it into the production "
            "query path remains the operator's gated decision (risk item 3)."
        )
    else:
        print(f"{rule}: HOLD — option C stays and there is no third attempt.")


def _parse_args(argv: list[str]) -> tuple[tuple[int, ...], str, int | None, tuple[float, float]]:
    """Return (N sweep, modo, over-fetch del híbrido, pesos RRF de (vector, léxico))."""
    mode = "recall"
    if "--rerank" in argv:
        mode = "rerank"
    for arg in argv:
        if arg == "--simulate-hybrid" or arg.startswith("--simulate-hybrid="):
            mode = "hybrid"
        if arg == "--shape-ab":
            mode = "shape"
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
            ft_id_list = _chunk_id_list(ft_chunks, question_id=question_id, leg="control")
            ft_ids = set(ft_id_list)
            ft_hits = len(reference_ids & ft_ids)
            ft_prec = ft_hits / len(ft_id_list) if ft_id_list else 0.0
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
    *,
    baseline: float,
) -> None:
    """Fase 0 del ADR 09: ¿la fusión vectores+fulltext supera al fulltext puro?

    Read-only y en memoria: los vectores se calculan acá, la rama vectorial es coseno exacto
    sobre los 3468 chunks (el caso **optimista** frente a un índice ANN real) y la fusión usa
    RRF, que no necesita normalizar el score de Lucene contra un coseno. El corte se imprime
    con la regla S2 vigente (monotonicidad contra la línea base commiteada + fila de control).
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
    dropped_total: list[str] = []
    ft_total_hits = 0
    hy_total_hits = 0
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
            lexical_ids = _chunk_id_list(lexical_hits, question_id=question_id, leg="control")

            fused = _fuse(vector_ids, lexical_ids, top_k, weights)
            ft_id_list = lexical_ids[:top_k]
            ft_ids = set(ft_id_list)
            ft_hits = len(reference_ids & ft_ids)
            hy_hits = len(reference_ids & set(fused))
            ft_prec = ft_hits / len(ft_id_list) if ft_id_list else 0.0
            hy_prec = hy_hits / len(fused) if fused else 0.0
            ft_precisions.append(ft_prec)
            hy_precisions.append(hy_prec)
            ft_total_hits += ft_hits
            hy_total_hits += hy_hits
            promoted = sorted(reference_ids & set(fused) - ft_ids)
            dropped = sorted(reference_ids & ft_ids - set(fused))
            promoted_total.extend(promoted)
            dropped_total.extend(dropped)
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
    print(f"delta híbrido - fulltext                                = {mean_hy - mean_ft:+.4f}")
    print(
        f"referencias que el híbrido trae y el fulltext no        = {sorted(set(promoted_total))}"
    )
    _print_s2_cut(
        _cut_decision(
            baseline=baseline,
            control_precision=mean_ft,
            control_hits=ft_total_hits,
            variant_hits=hy_total_hits,
            lost_references=len(dropped_total),
        ),
        baseline=baseline,
        control_precision=mean_ft,
        variant_precision=mean_hy,
        top_k=top_k,
    )


async def _shape_ab(
    adapter: Neo4jQueryAdapter,
    settings: Settings,
    records: list[dict[str, Any]],
    baseline: float,
) -> None:
    """Fase 2 (S2-S4): pregunta cruda (control) vs frase Lucene escapada (variante).

    Control y variante corren en la MISMA corrida con el mismo denominador chunk-only: la
    fila de control es la canaria que reproduce (o no) la línea base commiteada (S3), y
    recién entonces se lee el corte S2. El transform es caller-side (S4): el adapter sigue
    recibiendo el string verbatim; este modo decide qué string armar.
    """
    top_k = settings.retrieval_top_k
    print(
        "control = pregunta cruda (verbatim a db.index.fulltext.queryNodes); "
        f"variante = frase Lucene escapada (_shape_query), top-{top_k}"
    )
    if records:
        print(f"ejemplo de query deformada: {_shape_query(str(records[0].get('question', '')))}")
    print(
        f"{'question_id':<10} {'refs':>4} {'ct_hits':>7} {'ct_prec':>8} "
        f"{'sh_hits':>7} {'sh_prec':>8}  referencias ganadas / perdidas"
    )
    ct_precisions: list[float] = []
    sh_precisions: list[float] = []
    ct_total_hits = 0
    sh_total_hits = 0
    lost_total = 0
    gained_ids: list[str] = []
    try:
        for record in records:
            question_id = str(record.get("question_id", ""))
            question = str(record.get("question", ""))
            reference_ids = set(record.get("reference_context_ids", []) or [])

            ct_chunks = await adapter.search_chunks(question, top_k)
            sh_chunks = await adapter.search_chunks(_shape_query(question), top_k)
            ct_id_list = _chunk_id_list(ct_chunks, question_id=question_id, leg="control")
            sh_id_list = _chunk_id_list(sh_chunks, question_id=question_id, leg="shape-variant")
            ct_ids = set(ct_id_list)
            sh_ids = set(sh_id_list)

            ct_hits = len(reference_ids & ct_ids)
            sh_hits = len(reference_ids & sh_ids)
            ct_prec = ct_hits / len(ct_id_list) if ct_id_list else 0.0
            sh_prec = sh_hits / len(sh_id_list) if sh_id_list else 0.0
            ct_precisions.append(ct_prec)
            sh_precisions.append(sh_prec)
            ct_total_hits += ct_hits
            sh_total_hits += sh_hits

            gained = sorted(reference_ids & sh_ids - ct_ids)
            lost = sorted(reference_ids & ct_ids - sh_ids)
            lost_total += len(lost)
            gained_ids.extend(gained)
            print(
                f"{question_id:<10} {len(reference_ids):>4} {ct_hits:>7} {ct_prec:>8.3f} "
                f"{sh_hits:>7} {sh_prec:>8.3f}  {len(gained)} / {len(lost)}"
                + (f"  perdidas={lost}" if lost else "")
            )
    finally:
        await adapter.close()

    n = len(ct_precisions)
    mean_ct = sum(ct_precisions) / n if n else 0.0
    mean_sh = sum(sh_precisions) / n if n else 0.0
    print()
    print(f"precision@{top_k} control (fulltext crudo)  = {mean_ct:.4f}")
    print(f"precision@{top_k} variante (frase escapada) = {mean_sh:.4f}")
    print(f"delta variante - control                     = {mean_sh - mean_ct:+.4f}")
    print(f"referencias netas ganadas                    = {sorted(set(gained_ids))}")
    print()
    print(
        "determinism caveat (S8): ORDER BY score DESC no tiene tie-breaker; un empate de "
        "score de Lucene en el borde del top-10 puede cambiar la membresía y la métrica "
        "es set-based; la fila de control es la canaria, no se asume estabilidad."
    )
    _print_s2_cut(
        _cut_decision(
            baseline=baseline,
            control_precision=mean_ct,
            control_hits=ct_total_hits,
            variant_hits=sh_total_hits,
            lost_references=lost_total,
        ),
        baseline=baseline,
        control_precision=mean_ct,
        variant_precision=mean_sh,
        top_k=top_k,
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
        baseline = _committed_precision_baseline(settings.evaluation_baseline_dir)
        await _simulate_hybrid(adapter, settings, records, overfetch, weights, baseline=baseline)
        return
    if mode == "shape":
        baseline = _committed_precision_baseline(settings.evaluation_baseline_dir)
        await _shape_ab(adapter, settings, records, baseline)
        return
    try:
        await _recall_sweep(adapter, records, ns)
    finally:
        await adapter.close()


if __name__ == "__main__":
    asyncio.run(main())
