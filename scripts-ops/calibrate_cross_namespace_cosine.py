"""Calibracion READ-ONLY del cosine opcional sobre muestra estratificada (T8).

Historia: la retro-auditoria de los 302 merges cross-namespace
(``scripts-ops/audit_applied_cross_namespace.py``) encontro CERO pares con
evidencia lexica positiva (143 ambiguos, 156 sin wording compartido, 23
silenciosos). El modelo lexico es ciego a la parafrasis (mismo concepto, otras
palabras), y por eso el mantenedor aprobo un cosine OPCIONAL para una lista
corta, nunca como pasada masiva. Este guion mide, sobre una muestra pequena
estratificada, cuanto del "sin evidencia lexica" es en realidad identidad
verdadera que el numero lexico no vio, para que los veredictos de la
retro-auditoria descansen en datos y no en un numero que sabemos ciego en ese
eje.

Que hace:

  1. Lee un payload de la retro-auditoria (``--in``, esquema
     ``applied-cross-namespace-audit/2``; tambien acepta el ``/1`` ya
     comprometido, con aviso) y toma de ahi los ids de cada par y su estrato
     lexico (``evidence.strength``).
  2. Muestreo determinista: ``--per-stratum`` por estrato lexico con
     ``--seed`` fijo. El plan se IMPRIME ANTES de gastar nada.
  3. Las descripciones COMPLETAS de ambos lados del par se leen del grafo por
     id (Cypher MATCH unico, incluye soft-deleted: el duplicado conserva su
     descripcion), nunca la copia truncada del payload.
  4. El cosine pasa por el adaptador del proyecto (``SentenceTransformerAdapter``
     con el ``embedding_model_id`` del pipeline de resolucion): el numero es
     comparable con la senal S1. Cada lado se embebe en su propia llamada -
     2 llamadas por par - y el total se imprime y se guarda (transparencia de
     costo).
  5. ``--dry-run`` imprime exactamente que se embeberia y cuantas llamadas se
     harian, y NO llama ni al proveedor ni al grafo. Es la forma documentada de
     revisar el costo antes de gastar.
  6. Informe: matriz estrato lexico x banda cosine (bandas de
     ``BandThresholds()``: ``high_cosine`` / ``medium_cosine`` / ``low_cosine``;
     este guion no define numeros propios), conteos de acuerdo/desacuerdo, el
     detalle por par con veredicto (``cosine rescues it`` con lectura debil y
     cosine alto; ``cosine confirms the doubt`` con ambos debiles; ``agree``
     en el resto) y el titular: cuantos de los muestreados "sin evidencia
     lexica" rescata el cosine.

Garantias:

  - grafo SOLO LECTURA: las unicas consultas son MATCH; ninguna escritura de
    nodos, aristas ni estado;
  - el unico archivo que escribe es el ``--out`` JSON (payload auditable con
    modelo, llamadas, matriz y pares);
  - nunca imprime claves ni secretos (solo el id del modelo, nunca credenciales).

Uso (desde la raiz del repo, con el .env sourceado):
    uv run --no-sync python scripts-ops/calibrate_cross_namespace_cosine.py --help
    uv run --no-sync python scripts-ops/calibrate_cross_namespace_cosine.py --dry-run
    uv run --no-sync python scripts-ops/calibrate_cross_namespace_cosine.py \\
        --in /tmp/applied_cross_namespace_audit.json --per-stratum 5 \\
        --out /tmp/cosine_calibration.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from neo4j import AsyncGraphDatabase, AsyncSession

from book_graph_rag.config import Settings
from book_graph_rag.domain.quarantine_review_models import (
    EvidenceReading,
    mention_snippet,
    reading_for,
)
from book_graph_rag.domain.s4_band_assignment import BandThresholds
from book_graph_rag.infrastructure.brute_force_candidate_retrieval import cosine_similarity
from book_graph_rag.infrastructure.sentence_transformer_adapter import SentenceTransformerAdapter
from book_graph_rag.ports.embedding_provider_port import EmbeddingProviderPort, EmbeddingRequest

CALIBRATION_SCHEMA = "cosine-calibration/1"
#: El payload de la retro-auditoria que este guion consume. El ``/2`` es el
#: estrato language-aware (T8b); el ``/1`` comprometido se acepta con aviso
#: porque sus estratos ``none_silent`` confluyen idiomas.
ACCEPTED_INPUT_SCHEMAS = (
    "applied-cross-namespace-audit/2",
    "applied-cross-namespace-audit/1",
)
LANGUAGE_AWARE_INPUT_SCHEMA = "applied-cross-namespace-audit/2"
DEFAULT_IN = Path("evidence-bundles/applied-cross-namespace-audit-20261003.json")
DEFAULT_OUT = Path("/tmp/cosine_calibration.json")
DEFAULT_PER_STRATUM = 5
DEFAULT_SEED = 20261003

#: Nombres de banda tomados de los campos de ``BandThresholds()`` (los VALORES
#: nunca viven en este guion: salen de ``thresholds`` en cada corrida).
BAND_HIGH = "high_cosine"
BAND_MEDIUM = "medium_cosine"
BAND_LOW = "low_cosine"
COSINE_BANDS: tuple[str, ...] = (BAND_HIGH, BAND_MEDIUM, BAND_LOW)

#: Los tres veredictos del informe (contrato del deliverable T8).
VERDICT_RESCUE = "cosine rescues it"
VERDICT_DOUBT = "cosine confirms the doubt"
VERDICT_AGREE = "agree"
VERDICT_ORDER: tuple[str, ...] = (VERDICT_RESCUE, VERDICT_DOUBT, VERDICT_AGREE)

BATCH_SIZE = 500

#: Texto que se embebe por lado: la descripcion completa del grafo.
EMBEDDING_INPUT = "entity.description (full, read from graph by id)"

# ── Lectura (solo Cypher MATCH; ninguna decision de dominio vive aca) ─────────

# Descripciones completas por id, ambos lados del par. Incluye soft-deleted:
# el merge no borra nada, ``merged_into`` marca al duplicado pero su
# descripcion sigue siendo el texto que se calibra.
_QUERY_DESCRIPTIONS_BY_IDS = """
MATCH (e:Entity)
WHERE e.id IN $ids
RETURN e.id AS id, coalesce(e.description, '') AS description
"""


@dataclass(frozen=True)
class SampledPair:
    """Un par muestreado: lo que el payload dice antes de tocar grafo/proveedor."""

    stratum: str
    seq: int
    canonical_id: str
    candidate_id: str
    label: str
    reading: EvidenceReading
    description_overlap: float


@dataclass(frozen=True)
class CalibrationResult:
    """Un par medido: descripciones completas + cosine + banda + veredicto."""

    pair: SampledPair
    canonical_description: str
    candidate_description: str
    cosine: float | None
    cosine_band: str | None
    verdict: str | None
    provider_calls: int


def cosine_band(cosine: float, thresholds: BandThresholds) -> str:
    """Banda del cosine usando SOLO los umbrales compartidos de S4."""
    if cosine >= thresholds.high_cosine:
        return BAND_HIGH
    if cosine >= thresholds.medium_cosine:
        return BAND_MEDIUM
    return BAND_LOW


def verdict_for(reading: EvidenceReading, band: str) -> str:
    """Veredicto lexico vs cosine (contrato T8, tres salidas).

    - lectura debil (no ``identity``) + ``high_cosine`` -> rescata;
    - lectura debil + ``low_cosine`` (por debajo de ``medium_cosine``) ->
      confirma la duda;
    - todo lo demas (incluida la banda ``medium_cosine``, que no es ni alta
      ni baja para el esquema de tres veredictos) -> ``agree``.
    """
    if reading is not EvidenceReading.IDENTITY:
        if band == BAND_HIGH:
            return VERDICT_RESCUE
        if band == BAND_LOW:
            return VERDICT_DOUBT
    return VERDICT_AGREE


def _reading_from_string(fallback: str) -> EvidenceReading:
    """Fallback cuando el payload no trae ``description_overlap``."""
    try:
        return EvidenceReading(fallback)
    except ValueError:
        return EvidenceReading.UNDECIDED


def _pair_rows(
    payload: dict[str, Any], thresholds: BandThresholds
) -> tuple[list[SampledPair], int]:
    """Pares del payload (ids + estrato) sin la copia truncada de descripciones.

    Devuelve ``(pares, pares_descartados_por_entidad_ausente)``.
    """
    pairs: list[SampledPair] = []
    skipped_missing = 0
    for raw in payload.get("pairs", []):
        if raw.get("missing_entities"):
            skipped_missing += 1
            continue
        evidence = raw.get("evidence", {})
        raw_overlap = evidence.get("description_overlap")
        if raw_overlap is None:
            overlap = 0.0
            reading = _reading_from_string(str(evidence.get("reading", "")))
        else:
            overlap = float(raw_overlap)
            reading = reading_for(overlap, thresholds)
        stratum = str(evidence.get("strength", ""))
        if not stratum:
            print(
                f"ERROR: par seq {raw.get('seq')} sin evidence.strength (estrato lexico). "
                "Usar un payload de la retro-auditoria (esquema /2).",
                file=sys.stderr,
            )
            raise SystemExit(2)
        pairs.append(
            SampledPair(
                stratum=stratum,
                seq=int(raw.get("seq", 0)),
                canonical_id=str(raw["canonical_id"]),
                candidate_id=str(raw["candidate_id"]),
                label=str(raw.get("label", "")),
                reading=reading,
                description_overlap=overlap,
            )
        )
    return pairs, skipped_missing


def _group_strata(
    pairs: Sequence[SampledPair], columns: Sequence[str]
) -> dict[str, list[SampledPair]]:
    """Agrupa por estrato respetando el orden ``strength_columns`` del payload."""
    grouped: dict[str, list[SampledPair]] = {stratum: [] for stratum in columns}
    for pair in pairs:
        grouped.setdefault(pair.stratum, []).append(pair)
    return grouped


def _sample_strata(
    strata: dict[str, list[SampledPair]], per_stratum: int, seed: int
) -> dict[str, list[SampledPair]]:
    """Muestra determinista por estrato: mismo seed, misma muestra.

    El orden de poblacion es estable (seq, canonical, candidate) y un unico
    ``random.Random(seed)`` recorre los estratos en orden del payload, asi que
    la corrida completa es reproducible byte a byte.
    """
    rng = random.Random(seed)
    sampled: dict[str, list[SampledPair]] = {}
    for stratum, population in strata.items():
        ordered = sorted(population, key=lambda p: (p.seq, p.canonical_id, p.candidate_id))
        if len(ordered) <= per_stratum:
            picked = ordered
        else:
            picked = sorted(
                rng.sample(ordered, per_stratum),
                key=lambda p: (p.seq, p.canonical_id, p.candidate_id),
            )
        sampled[stratum] = picked
    return sampled


async def _measure(
    sampled: dict[str, list[SampledPair]],
    descriptions: dict[str, str],
    provider: EmbeddingProviderPort,
    model_id: str,
    thresholds: BandThresholds,
) -> tuple[list[CalibrationResult], int, int]:
    """Embebe ambos lados de cada par y computa cosine/banda/veredicto.

    Devuelve ``(resultados, llamadas al proveedor, pares sin descripcion)``.
    """
    results: list[CalibrationResult] = []
    provider_calls = 0
    skipped_no_description = 0
    for pairs in sampled.values():
        for pair in pairs:
            canonical_description = descriptions.get(pair.canonical_id, "")
            candidate_description = descriptions.get(pair.candidate_id, "")
            if not canonical_description.strip() or not candidate_description.strip():
                skipped_no_description += 1
                continue
            # 2 llamadas por par (un texto por llamada) — costo transparente.
            canonical_batch = await provider.embed(
                EmbeddingRequest(texts=(canonical_description,), model_id=model_id)
            )
            provider_calls += 1
            candidate_batch = await provider.embed(
                EmbeddingRequest(texts=(candidate_description,), model_id=model_id)
            )
            provider_calls += 1
            cosine = cosine_similarity(
                canonical_batch.vectors[0].values, candidate_batch.vectors[0].values
            )
            band = cosine_band(cosine, thresholds)
            results.append(
                CalibrationResult(
                    pair=pair,
                    canonical_description=canonical_description,
                    candidate_description=candidate_description,
                    cosine=cosine,
                    cosine_band=band,
                    verdict=verdict_for(pair.reading, band),
                    provider_calls=2,
                )
            )
    return results, provider_calls, skipped_no_description


def _build_matrix(
    results: Sequence[CalibrationResult], strata_order: Sequence[str]
) -> dict[str, dict[str, int]]:
    """Matriz estrato lexico x banda cosine (celdas + total por fila)."""
    matrix: dict[str, dict[str, int]] = {}
    for stratum in strata_order:
        row = dict.fromkeys(COSINE_BANDS, 0)
        row["total"] = 0
        matrix[stratum] = row
    for result in results:
        row = matrix.setdefault(result.pair.stratum, dict.fromkeys(COSINE_BANDS, 0))
        row.setdefault("total", 0)
        if result.cosine_band is None:
            continue
        row[result.cosine_band] += 1
        row["total"] += 1
    return matrix


def _verdict_counts(results: Sequence[CalibrationResult]) -> dict[str, int]:
    counts = dict.fromkeys(VERDICT_ORDER, 0)
    for result in results:
        if result.verdict in counts:
            counts[result.verdict] += 1
    return counts


def _headline(results: Sequence[CalibrationResult]) -> dict[str, int]:
    """Cuantos "sin evidencia lexica" muestreados rescata el cosine."""
    no_lexical = [
        result for result in results if result.pair.reading is EvidenceReading.NO_SHARED_CONTEXT
    ]
    rescued = [result for result in no_lexical if result.verdict == VERDICT_RESCUE]
    return {
        "no_lexical_evidence_sampled": len(no_lexical),
        "cosine_rescued": len(rescued),
    }


async def _dicts(session: AsyncSession, query: str, **params: Any) -> list[dict[str, Any]]:
    result = await session.run(query, **params)
    return [record.data() async for record in result]


def _batched(items: Sequence[str]) -> list[list[str]]:
    return [list(items[i : i + BATCH_SIZE]) for i in range(0, len(items), BATCH_SIZE)]


async def _fetch_descriptions(settings: Settings, ids: Sequence[str]) -> dict[str, str]:
    """Descripciones completas por id — unico contacto con el grafo (MATCH)."""
    driver = AsyncGraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value()),
    )
    descriptions: dict[str, str] = {}
    try:
        async with driver.session() as session:
            for batch in _batched(ids):
                for row in await _dicts(session, _QUERY_DESCRIPTIONS_BY_IDS, ids=batch):
                    descriptions[str(row["id"])] = str(row.get("description") or "")
    finally:
        await driver.close()
    return descriptions


def _graph_fetcher(
    settings: Settings,
) -> Callable[[Sequence[str]], Awaitable[dict[str, str]]]:
    """Closure que abre el driver SOLO cuando se le piden descripciones."""

    async def fetch(ids: Sequence[str]) -> dict[str, str]:
        return await _fetch_descriptions(settings, ids)

    return fetch


def _read_input(path: Path) -> dict[str, Any]:
    if not path.exists():
        print(f"ERROR: no existe el payload de entrada: {path}", file=sys.stderr)
        raise SystemExit(2)
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    schema = str(payload.get("schema", ""))
    if schema not in ACCEPTED_INPUT_SCHEMAS:
        print(
            f"ERROR: esquema de entrada {schema!r} no soportado; "
            f"aceptados: {', '.join(ACCEPTED_INPUT_SCHEMAS)}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    if schema != LANGUAGE_AWARE_INPUT_SCHEMA:
        print(
            f"AVISO: entrada en esquema {schema} (pre-T8b): sus estratos "
            "'none_silent' confluyen idiomas; regenerar la auditoria con el "
            "guion actual para los estratos language-aware.",
            file=sys.stderr,
        )
    return payload


def _input_columns(payload: dict[str, Any]) -> list[str]:
    """Orden de estratos segun ``stratification.strength_columns`` del payload."""
    stratification = payload.get("stratification", {})
    columns = stratification.get("strength_columns")
    if isinstance(columns, list) and columns:
        return [str(column) for column in columns]
    return []


def _print_plan(
    strata: dict[str, list[SampledPair]],
    sampled: dict[str, list[SampledPair]],
    *,
    seed: int,
    per_stratum: int,
    dry_run: bool,
) -> int:
    """Plan ANTES de gastar: poblacion, muestra y llamadas previstas."""
    print("\n-- PLAN DE MUESTREO (antes de gastar) --")
    print(f"  {'estrato':<26}{'poblacion':>11}{'muestreados':>13}{'llamadas':>10}")
    total_sampled = 0
    for stratum, population in strata.items():
        picked = sampled.get(stratum, [])
        total_sampled += len(picked)
        print(f"  {stratum:<26}{len(population):>11}{len(picked):>13}{2 * len(picked):>10}")
    planned_calls = 2 * total_sampled
    print(
        f"  {'total':<26}{sum(len(p) for p in strata.values()):>11}"
        f"{total_sampled:>13}{planned_calls:>10}"
    )
    print(f"  seed {seed} · per-stratum {per_stratum} · 2 llamadas por par")
    print(f"  llamadas previstas al proveedor: {planned_calls}")
    print("  texto a embeber: descripcion COMPLETA de cada lado (grafo por id)")
    if dry_run:
        print("  DRY RUN: no se llamara al proveedor ni al grafo")
    return planned_calls


def _print_dry_run_pairs(sampled: dict[str, list[SampledPair]]) -> None:
    print("\n-- QUE SE EMBEBERIA (dry run) --")
    for stratum, pairs in sampled.items():
        for pair in pairs:
            print(
                f"  [{stratum}] seq {pair.seq} · lectura {pair.reading.value} "
                f"· overlap {pair.description_overlap:.3f}"
            )
            print(f"    {pair.canonical_id}  ->  {pair.candidate_id}")
            print("    texto: descripcion completa por id (2 llamadas)")


def _print_matrix(matrix: dict[str, dict[str, int]]) -> None:
    print("\n-- MATRIZ: estrato lexico x banda cosine --")
    header = f"  {'estrato':<26}" + "".join(f"{band:>15}" for band in (*COSINE_BANDS, "total"))
    print(header)
    for stratum, row in matrix.items():
        cells = "".join(f"{row[key]:>15}" for key in (*COSINE_BANDS, "total"))
        print(f"  {stratum:<26}{cells}")
    totals = dict.fromkeys((*COSINE_BANDS, "total"), 0)
    for row in matrix.values():
        for key in totals:
            totals[key] += row[key]
    cells = "".join(f"{totals[key]:>15}" for key in (*COSINE_BANDS, "total"))
    print(f"  {'total':<26}{cells}")


def _print_result(result: CalibrationResult) -> None:
    cosine_text = f"{result.cosine:.3f}" if result.cosine is not None else "-"
    band_text = result.cosine_band or "-"
    print(
        f"  [{result.pair.stratum}] seq {result.pair.seq} "
        f"· lectura {result.pair.reading.value} "
        f"· overlap {result.pair.description_overlap:.3f} "
        f"· cosine {cosine_text} · banda {band_text}"
    )
    print(f"    veredicto: {result.verdict}")
    print(f"    {result.pair.canonical_id}  ->  {result.pair.candidate_id}")
    print(
        f"      desc canon ({len(result.canonical_description)} chars): "
        f"{mention_snippet(result.canonical_description) or '(sin descripcion)'}"
    )
    print(
        f"      desc cand. ({len(result.candidate_description)} chars): "
        f"{mention_snippet(result.candidate_description) or '(sin descripcion)'}"
    )


def _pair_payload(result: CalibrationResult | None, pair: SampledPair) -> dict[str, Any]:
    base: dict[str, Any] = {
        "stratum": pair.stratum,
        "seq": pair.seq,
        "canonical_id": pair.canonical_id,
        "candidate_id": pair.candidate_id,
        "label": pair.label,
        "reading": pair.reading.value,
        "description_overlap": pair.description_overlap,
    }
    if result is None:
        base.update(
            {
                "canonical_description": None,
                "candidate_description": None,
                "cosine": None,
                "cosine_band": None,
                "verdict": None,
                "provider_calls": 0,
            }
        )
        return base
    base.update(
        {
            "canonical_description": result.canonical_description,
            "candidate_description": result.candidate_description,
            "cosine": result.cosine,
            "cosine_band": result.cosine_band,
            "verdict": result.verdict,
            "provider_calls": result.provider_calls,
        }
    )
    return base


def _write_payload(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False, default=str)
        handle.write("\n")


async def calibrate(
    *,
    input_path: Path,
    per_stratum: int,
    seed: int,
    dry_run: bool,
    out_path: Path,
    model_id: str,
    thresholds: BandThresholds,
    provider: EmbeddingProviderPort,
    fetch_descriptions: Callable[[Sequence[str]], Awaitable[dict[str, str]]],
) -> dict[str, Any]:
    """Flujo completo: payload -> muestra -> (descripcion, cosine) -> informe.

    En ``dry_run`` NO se llama ``fetch_descriptions`` ni ``provider.embed``:
    solo se imprime y se escribe el plan.
    """
    if per_stratum < 1:
        print("ERROR: --per-stratum debe ser >= 1", file=sys.stderr)
        raise SystemExit(2)

    payload_in = _read_input(input_path)
    input_schema = str(payload_in.get("schema", ""))
    pairs, skipped_missing = _pair_rows(payload_in, thresholds)
    strata = _group_strata(pairs, _input_columns(payload_in))
    sampled = _sample_strata(strata, per_stratum, seed)

    print("== CALIBRACION DEL COSINE CROSS-NAMESPACE (SOLO LECTURA) ==")
    print(f"  entrada: {input_path} (esquema {input_schema})")
    print(f"  modelo: {model_id} · entrada: {EMBEDDING_INPUT}")
    print(
        f"  umbrales BandThresholds(): high_cosine {thresholds.high_cosine} "
        f"· medium_cosine {thresholds.medium_cosine}"
    )
    print(f"  seed {seed} · per-stratum {per_stratum} · salida {out_path}")
    print("  lectura lexica: reading_for(description_overlap) del modelo compartido")
    print("  consultas: solo MATCH (garantia de no mutacion)")

    planned_calls = _print_plan(
        strata, sampled, seed=seed, per_stratum=per_stratum, dry_run=dry_run
    )

    plan_rows = [
        {
            "stratum": stratum,
            "population": len(population),
            "sampled": len(sampled.get(stratum, [])),
        }
        for stratum, population in strata.items()
    ]

    if dry_run:
        _print_dry_run_pairs(sampled)
        payload_out: dict[str, Any] = {
            "schema": CALIBRATION_SCHEMA,
            "generated_at": datetime.now(UTC).isoformat(),
            "read_only": True,
            "query_classes": ["MATCH"],
            "dry_run": True,
            "seed": seed,
            "per_stratum": per_stratum,
            "model_id": model_id,
            "embedding_input": EMBEDDING_INPUT,
            "provider_calls": 0,
            "planned_provider_calls": planned_calls,
            "input": {
                "path": str(input_path),
                "schema": input_schema,
                "pairs_usable": len(pairs),
                "pairs_skipped_missing_entities": skipped_missing,
            },
            "thresholds": {
                "high_cosine": thresholds.high_cosine,
                "medium_cosine": thresholds.medium_cosine,
            },
            "plan": plan_rows,
            "matrix": {},
            "cosine_bands": list(COSINE_BANDS),
            "verdict_counts": dict.fromkeys(VERDICT_ORDER, 0),
            "headline": {
                "no_lexical_evidence_sampled": 0,
                "cosine_rescued": 0,
            },
            "skipped": {"missing_entities": skipped_missing, "no_description": 0},
            "pairs": [_pair_payload(None, pair) for pairs in sampled.values() for pair in pairs],
        }
        _write_payload(out_path, payload_out)
        print("\n-- DRY RUN: sin llamadas --")
        print(f"  llamadas al proveedor: 0 (previstas {planned_calls})")
        print("  consultas al grafo: 0")
        print(f"  JSON (plan): {out_path}")
        return payload_out

    ids = sorted(
        {pair.canonical_id for pairs in sampled.values() for pair in pairs}
        | {pair.candidate_id for pairs in sampled.values() for pair in pairs}
    )
    descriptions: dict[str, str] = {}
    if ids:
        descriptions = await fetch_descriptions(ids)
    print("\n-- DESCRIPCIONES COMPLETAS (grafo, solo MATCH) --")
    print(
        f"  ids pedidos: {len(ids)} · descripciones no vacias: "
        f"{sum(1 for text in descriptions.values() if text.strip())}"
    )

    results, provider_calls, skipped_no_description = await _measure(
        sampled, descriptions, provider, model_id, thresholds
    )
    matrix = _build_matrix(results, list(strata))
    verdicts = _verdict_counts(results)
    headline = _headline(results)

    _print_matrix(matrix)
    print("\n-- ACUERDO / DESACUERDO --")
    for verdict, count in verdicts.items():
        print(f"  {verdict}: {count}")

    print("\n-- PARES MUESTREADOS --")
    for result in results:
        _print_result(result)

    print("\n-- TITULAR --")
    print(
        f"  pares muestreados SIN evidencia lexica "
        f"(no_shared_context): {headline['no_lexical_evidence_sampled']}"
    )
    print(f"  de esos, rescatados por el cosine (>= high_cosine): {headline['cosine_rescued']}")

    payload_out = {
        "schema": CALIBRATION_SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(),
        "read_only": True,
        "query_classes": ["MATCH"],
        "dry_run": False,
        "seed": seed,
        "per_stratum": per_stratum,
        "model_id": model_id,
        "embedding_input": EMBEDDING_INPUT,
        "provider_calls": provider_calls,
        "planned_provider_calls": planned_calls,
        "input": {
            "path": str(input_path),
            "schema": input_schema,
            "pairs_usable": len(pairs),
            "pairs_skipped_missing_entities": skipped_missing,
        },
        "thresholds": {
            "high_cosine": thresholds.high_cosine,
            "medium_cosine": thresholds.medium_cosine,
        },
        "plan": plan_rows,
        "matrix": matrix,
        "cosine_bands": list(COSINE_BANDS),
        "verdict_counts": verdicts,
        "headline": headline,
        "skipped": {
            "missing_entities": skipped_missing,
            "no_description": skipped_no_description,
        },
        "pairs": [_pair_payload(result, result.pair) for result in results],
    }
    _write_payload(out_path, payload_out)

    print("\n-- COSTO --")
    print(f"  llamadas al proveedor: {provider_calls} (previstas {planned_calls}; 2 por par)")
    if skipped_no_description:
        print(f"  pares descartados sin descripcion completa: {skipped_no_description}")
    print(f"  JSON: {out_path}")
    print("  grafo sin mutar: solo se ejecutaron consultas MATCH")
    return payload_out


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--in",
        dest="in_path",
        type=Path,
        default=DEFAULT_IN,
        help=f"payload JSON de la retro-auditoria (default: {DEFAULT_IN})",
    )
    parser.add_argument(
        "--per-stratum",
        type=int,
        default=DEFAULT_PER_STRATUM,
        help=f"pares por estrato lexico (default {DEFAULT_PER_STRATUM})",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SEED,
        help=f"seed del muestreo determinista (default {DEFAULT_SEED})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="imprime que se embeberia y cuantas llamadas haria; NO llama al "
        "proveedor ni al grafo (revisar costo antes de gastar)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"payload JSON del calibrado (default: {DEFAULT_OUT})",
    )
    return parser.parse_args(argv)


async def main() -> None:
    args = _parse_args()
    settings = Settings.model_validate({})
    thresholds = BandThresholds(
        high_cosine=settings.band_high_cosine,
        medium_cosine=settings.band_medium_cosine,
        high_context=settings.band_high_context,
        conflict_floor=settings.band_conflict_floor,
    )
    # El adaptador carga el modelo en el primer embed: en dry run no carga nada.
    provider = SentenceTransformerAdapter(settings)
    await calibrate(
        input_path=args.in_path,
        per_stratum=args.per_stratum,
        seed=args.seed,
        dry_run=args.dry_run,
        out_path=args.out,
        model_id=settings.embedding_model_id,
        thresholds=thresholds,
        provider=provider,
        fetch_descriptions=_graph_fetcher(settings),
    )


if __name__ == "__main__":
    asyncio.run(main())
