"""Auditoria retroactiva READ-ONLY de los 302 merges cross-namespace (T8).

Historia: ``scripts-ops/resolve_cross_namespace.py`` forjo ``band=EXACT,
cross_namespace=True`` y aplico directo contra ``ApplyMergeUseCase``, saltandose
la cuarentena obligatoria (spec 03 2.4 / policy R6.2). Resultado: 302 de las
958 entradas del ledger cruzaron namespace sin revision y sin scoring S3 (la
evidencia guardada no lleva ``s3``). Antes de aprobar ningun merge nuevo (los
20 registros pendientes se quedan pendientes a proposito) se audita el pasado
con este guion SOLO LECTURA:

  - el ledger se lee con el lector existente ``JSONLMergeLedger`` y el modelo
    ``MergeLedgerEntry`` (sin parseo propio);
  - el grafo se consulta unicamente con sentencias MATCH (sin escritura de
    nodos, aristas ni estado);
  - el unico archivo que escribe es el ``--out`` JSON.

SELECCION: entrada del ledger cuyo ``canonical_id`` y al menos un
``candidate_ids`` difieren en namespace (namespace = ``namespace_from_id``:
los dos primeros componentes del id separados por ``:``). El total debe ser
exactamente 302 (``EXPECTED_CROSS_NAMESPACE_ENTRIES``); si el numero cambia,
el guion falla con el numero medido en el mensaje.

HECHOS HISTORICOS (solo ledger, sin grafo): por entrada se conservan ``seq``,
``applied_at``, ``approver``, la banda guardada y si la evidencia guardada no
lleva ``s3`` (``s3 is None`` en TODOS sus registros de evidencia); la consola
imprime el agregado y el JSON conserva la fila por entrada.

EVIDENCIA RECOMPUTADA HOY (grafo, solo MATCH), por cada par (canonical,
candidato que cruza) — el duplicado conserva su propia descripcion porque el
merge lo soft-deletea:

  - ``description_overlap`` es la senal PRIMARIA; ``mentions_jaccard`` y
    ``related_jaccard`` son estructurales (etiquetas ``PRIMARY_SIGNAL`` /
    ``STRUCTURAL_SIGNALS``; funciones del dominio en
    ``domain/s3_context_scoring.py``, re-auditoria post-merge — design 4.1).
  - riesgo de etiqueta del corpus via ``is_generic_label`` + ``label_risk`` /
    ``format_risk_marker``; el ``namespace_count`` se lee del grafo (como
    exige ``label_risk``), nunca del ledger. La clave de grupo es la misma
    expresion Cypher del R5a (``toLower(trim(name))`` + ``type`` sobre
    entidades vivas), con la coordenacion T9 documentada en el renderer.
  - ``lexically_silent``: overlap exactamente 0 y AMBAS descripciones
    sustanciales (>= ``LEXICAL_SILENCE_MIN_CHARS`` chars sin contar
    espacios, ~una oracion en cualquiera de los dos idiomas). Ahi el numero
    lexico no puede juzgar (libros en distinto idioma o encuadres sin
    solape de tokens) y el humano tiene que LEER: es el listado donde el
    cosine opcional aprobaria. Mas corto que el piso no es "silencioso",
    es una descripcion que no llega a informar.
  - la fuerza de la evidencia la da ``reading_for`` + ``BandThresholds()``;
    este guion no define umbrales ni etiquetas de lectura propias.

ESTRATIFICACION: matriz riesgo de etiqueta (high/medium/none) x fuerza de
evidencia (strong / ambiguous / none; dentro de ``none`` separa
``lexically_silent`` del resto). Listas resultantes:

  - ``clear_identity``: evidencia fuerte, overlap descendente;
  - ``suspicious``: riesgo high/medium con evidencia debil o ausente y NO
    silencioso — lo mas peligroso primero (riesgo alto, luego menor overlap):
    los candidatos a merge erroneo;
  - ``needs_reading``: ``lexically_silent`` — el corto donde ayudaria el
    cosine; ``--limit`` acota las listas de consola, el JSON siempre lleva
    todo.

Garantia de solo lectura: unicamente se ejecutan consultas MATCH; el unico
archivo que se escribe es ``--out``.

Uso (desde la raiz del repo, con el .env sourceado):
    uv run --no-sync python scripts-ops/audit_applied_cross_namespace.py --help
    uv run --no-sync python scripts-ops/audit_applied_cross_namespace.py
    uv run --no-sync python scripts-ops/audit_applied_cross_namespace.py \\
        --limit 3 --out /tmp/applied_cross_namespace_audit.json
    uv run --no-sync python scripts-ops/audit_applied_cross_namespace.py \\
        --namespace knowledge:ai-engineering-huyen
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from neo4j import AsyncGraphDatabase, AsyncSession
from pydantic import ValidationError

from book_graph_rag.config import Settings
from book_graph_rag.domain.merge_ledger_models import MergeBand, MergeLedgerEntry
from book_graph_rag.domain.models import Entity
from book_graph_rag.domain.quarantine_review_models import (
    PRIMARY_SIGNAL,
    STRUCTURAL_SIGNALS,
    EvidenceReading,
    LabelRisk,
    RiskLevel,
    format_risk_marker,
    is_generic_label,
    label_risk,
    mention_snippet,
    reading_for,
)
from book_graph_rag.domain.s0_normalization import namespace_from_id
from book_graph_rag.domain.s3_context_scoring import (
    description_overlap,
    mentions_jaccard,
    related_jaccard,
)
from book_graph_rag.domain.s4_band_assignment import BandThresholds
from book_graph_rag.infrastructure.jsonl_merge_ledger import JSONLMergeLedger

AUDIT_SCHEMA = "applied-cross-namespace-audit/1"
#: Ground truth medida el 2026-10-03 (design seccion 1): 302 de 958.
EXPECTED_CROSS_NAMESPACE_ENTRIES = 302
DEFAULT_OUT = Path("/tmp/applied_cross_namespace_audit.json")
DEFAULT_LIMIT = 15
#: Piso de "descripcion sustancial" para ``is_lexically_silent``: con menos
#: texto que esto el solape cero no prueba nada (no hay material para juzgar),
#: asi que solo encuesta el silencio con ambas descripciones >= este largo.
LEXICAL_SILENCE_MIN_CHARS = 60
BATCH_SIZE = 500

#: Etiquetas de lectura heredadas del modelo compartido (nada local).
READING_ORDER = tuple(reading.value for reading in EvidenceReading)
#: Columnas de la matriz: dentro de ``none`` se separa el silencio lexico.
STRENGTH_COLUMNS = ("strong", "ambiguous", "none_silent", "none_rest")
_LIST_KEYS = ("clear_identity", "suspicious", "needs_reading")

# ── Lectura (solo Cypher MATCH; ninguna decision de dominio vive aca) ─────────

# Incluye soft-deleted: el duplicado de cada par conserva su propia
# descripcion porque el merge lo soft-deletea (merged_into).
_QUERY_ENTITIES_BY_IDS = """
MATCH (e:Entity)
WHERE e.id IN $ids
RETURN e.id AS id, e.name AS name, e.type AS type,
       coalesce(e.description, '') AS description,
       e.source_page AS source_page,
       coalesce(e.merged_into, '') AS merged_into
"""

# Fuentes (libros) que mencionan cada entidad seleccionada. coalesce replica el
# patron de Neo4jNeighborhoodQueryAdapter.mention_sources (chunks legacy sin
# source_id). Tras un merge las aristas MENTIONS del duplicado se reapuntaron
# al canonico (mapa inverso), de ahi que el lado duplicado pueda venir vacio.
_QUERY_MENTION_SOURCES = """
MATCH (c:Chunk)-[:MENTIONS]->(e:Entity)
WHERE e.id IN $ids
RETURN DISTINCT e.id AS id, coalesce(c.source_id, c.book_id) AS source_id
"""

# Vecinos RELATED en AMBAS direcciones (patron no dirigido) con entidades
# vivas unicamente: los nodos con merged_into son carcasas soft-deleted cuyo
# conocimiento vive en el canonico (mismo criterio que el renderer T2).
_QUERY_NEIGHBORS = """
MATCH (e:Entity)-[:RELATED]-(other:Entity)
WHERE e.id IN $ids AND coalesce(other.merged_into, '') = ''
RETURN DISTINCT e.id AS id, other.id AS neighbor_id
"""

# namespace_count del corpus para label_risk: entidades VIVAS que comparten
# la clave R5a (toLower(trim(name)) + type). La clave se devuelve ya bajada
# por Cypher y se agrupa aca con namespace_from_id (coordinacion T9).
_QUERY_LABEL_NAMESPACES = """
MATCH (e:Entity)
WHERE coalesce(e.merged_into, '') = '' AND toLower(trim(e.name)) IN $labels
RETURN toLower(trim(e.name)) AS group_key, e.type AS type, e.id AS id
"""


@dataclass(frozen=True)
class PairAudit:
    """Un par (canonical, candidato que cruza) con hechos + evidencia.

    Los primeros campos son los que la estratificacion lee; los del resto
    documentan el hecho historico y el estado actual del grafo (payload JSON).
    """

    seq: int
    canonical_id: str
    candidate_id: str
    canonical_namespace: str
    candidate_namespace: str
    label: str
    description_overlap: float
    risk: LabelRisk
    lexically_silent: bool
    applied_at: datetime | None = None
    approver: str = ""
    stored_band: str = ""
    stored_evidence_s3_none: bool = False
    rolled_back: bool = False
    entity_type: str = ""
    mentions_jaccard: float = 0.0
    related_jaccard: float = 0.0
    canonical_description: str = ""
    candidate_description: str = ""
    canonical_page: int | None = None
    candidate_page: int | None = None
    canonical_books: tuple[str, ...] = ()
    candidate_books: tuple[str, ...] = ()
    candidate_state: str = ""
    missing_entities: bool = False


@dataclass(frozen=True)
class Stratification:
    """Matriz riesgo x fuerza, las tres listas y los totales por estrato."""

    matrix: dict[RiskLevel, dict[str, int]]
    clear_identity: tuple[PairAudit, ...]
    suspicious: tuple[PairAudit, ...]
    needs_reading: tuple[PairAudit, ...]
    totals: dict[str, int]


def is_lexically_silent(
    overlap: float, canonical_description: str, candidate_description: str
) -> bool:
    """True cuando el solape es 0 y AMBAS descripciones son sustanciales.

    Sustancial = ``>= LEXICAL_SILENCE_MIN_CHARS`` chars no-vacios por lado
    (documentado arriba). Con overlap 0 exacto (Jaccard de tokens sin ningun
    token compartido) y material de lectura a los dos lados, el numero lexico
    no puede juzgar: o los libros estan en distinto idioma o los encuadres no
    comparten terminos — ahi decide el humano (y el cosine opcional pagaria).
    """
    if overlap != 0.0:
        return False
    return (
        len(canonical_description.strip()) >= LEXICAL_SILENCE_MIN_CHARS
        and len(candidate_description.strip()) >= LEXICAL_SILENCE_MIN_CHARS
    )


def _strength_column(overlap: float, silent: bool, thresholds: BandThresholds) -> str:
    """Columna de la matriz con la lectura del modelo compartido.

    ``strong`` >= ``high_context``; ``ambiguous`` >= ``conflict_floor``;
    por debajo, ``none_silent`` si el par es lexically silencioso y
    ``none_rest`` en caso contrario. Ningun umbral vive en este guion.
    """
    reading = reading_for(overlap, thresholds)
    if reading is EvidenceReading.IDENTITY:
        return "strong"
    if reading is EvidenceReading.UNDECIDED:
        return "ambiguous"
    return "none_silent" if silent else "none_rest"


def stratify(pairs: Sequence[PairAudit], thresholds: BandThresholds) -> Stratification:
    """Cruza riesgo de etiqueta x fuerza de evidencia y arma las tres listas.

    Pura sobre pares ya calculados (sin ledger ni grafo). Memoria: cada par cae
    en exacta una celda de la matriz y en a lo sumo una lista — los fuertes
    van a ``clear_identity``, los lexicamente silenciosos a ``needs_reading``
    y el resto con riesgo high/medium (y NO silencioso) a ``suspicious``.
    """
    matrix: dict[RiskLevel, dict[str, int]] = {
        level: dict.fromkeys(STRENGTH_COLUMNS, 0) for level in RiskLevel
    }
    clear: list[PairAudit] = []
    suspicious: list[PairAudit] = []
    reading: list[PairAudit] = []
    for pair in pairs:
        column = _strength_column(pair.description_overlap, pair.lexically_silent, thresholds)
        matrix[pair.risk.level][column] += 1
        if column == "strong":
            clear.append(pair)
        elif column == "none_silent":
            reading.append(pair)
        elif pair.risk.level in (RiskLevel.HIGH, RiskLevel.MEDIUM):
            # Riesgo sin evidencia fuerte y sin silencio lexico: el candidato
            # a merge erroneo. Lo mas peligroso primero: alto riesgo y luego
            # el overlap mas bajo.
            suspicious.append(pair)

    clear.sort(key=lambda pair: (-pair.description_overlap, pair.seq))
    suspicious.sort(
        key=lambda pair: (
            0 if pair.risk.level is RiskLevel.HIGH else 1,
            pair.description_overlap,
            pair.seq,
        )
    )
    reading.sort(key=lambda pair: (pair.label.casefold(), pair.seq))

    totals: dict[str, int] = {
        f"risk_{level.value}": sum(matrix[level].values()) for level in RiskLevel
    }
    totals["clear_identity"] = len(clear)
    totals["suspicious"] = len(suspicious)
    totals["needs_reading"] = len(reading)
    return Stratification(
        matrix=matrix,
        clear_identity=tuple(clear),
        suspicious=tuple(suspicious),
        needs_reading=tuple(reading),
        totals=totals,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ledger",
        type=Path,
        default=None,
        help="ruta del ledger merge (default: merge_ledger_path de Settings)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"maximo de pares por lista en consola (default {DEFAULT_LIMIT}); "
        "el JSON siempre conserva todos",
    )
    parser.add_argument(
        "--namespace",
        default=None,
        help="acota los pares a ese namespace en cualquiera de los dos lados "
        "(p. ej. knowledge:ai-engineering-huyen)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"payload JSON completo (default: {DEFAULT_OUT})",
    )
    return parser.parse_args()


def _crossing_candidate_ids(entry: MergeLedgerEntry, canonical_namespace: str) -> list[str]:
    """Candidatos cuyo namespace difiere del canonico (la seleccion T8)."""
    return [
        candidate_id
        for candidate_id in entry.candidate_ids
        if namespace_from_id(candidate_id) != canonical_namespace
    ]


def _select_crossing_entries(entries: Sequence[MergeLedgerEntry]) -> list[MergeLedgerEntry]:
    """Entradas donde al menos un candidato cruza de namespace respecto al canonico."""
    selected: list[MergeLedgerEntry] = []
    for entry in entries:
        canonical_namespace = namespace_from_id(entry.canonical_id)
        if _crossing_candidate_ids(entry, canonical_namespace):
            selected.append(entry)
    return selected


def _assert_expected_count(measured: int) -> None:
    """Falla fuerte si la seleccion no mide exactamente 302 (ground truth)."""
    if measured == EXPECTED_CROSS_NAMESPACE_ENTRIES:
        return
    print(
        f"ERROR: seleccion cross-namespace = {measured} entradas, "
        f"esperado {EXPECTED_CROSS_NAMESPACE_ENTRIES} (ground truth 2026-10-03). "
        "El ledger cambio: revisar antes de confiar en este informe. "
        "No se consulto el grafo ni se escribio ninguna salida.",
        file=sys.stderr,
    )
    raise SystemExit(2)


async def _dicts(session: AsyncSession, query: str, **params: Any) -> list[dict[str, Any]]:
    result = await session.run(query, **params)
    return [record.data() async for record in result]


async def _load_entities(
    session: AsyncSession, ids: list[str]
) -> tuple[dict[str, Entity], dict[str, str], int]:
    """Entidades por id (vivas y soft-deleted) + su merged_into."""
    entities: dict[str, Entity] = {}
    merged_into: dict[str, str] = {}
    skipped = 0
    for batch in _batched(ids):
        for row in await _dicts(session, _QUERY_ENTITIES_BY_IDS, ids=batch):
            merged_into[str(row["id"])] = str(row.get("merged_into") or "")
            try:
                entity = Entity.model_validate(row)
            except ValidationError:
                skipped += 1  # tipos legacy fuera del contrato EntityType
                continue
            entities[entity.id] = entity
    return entities, merged_into, skipped


def _batched(items: list[str]) -> list[list[str]]:
    return [items[i : i + BATCH_SIZE] for i in range(0, len(items), BATCH_SIZE)]


async def _load_mentions(session: AsyncSession, ids: list[str]) -> dict[str, set[str]]:
    mentions: dict[str, set[str]] = {entity_id: set() for entity_id in ids}
    for batch in _batched(ids):
        for row in await _dicts(session, _QUERY_MENTION_SOURCES, ids=batch):
            source_id = row.get("source_id")
            if source_id:
                mentions.setdefault(str(row["id"]), set()).add(str(source_id))
    return mentions


async def _load_neighbors(session: AsyncSession, ids: list[str]) -> dict[str, set[str]]:
    neighbors: dict[str, set[str]] = {entity_id: set() for entity_id in ids}
    for batch in _batched(ids):
        for row in await _dicts(session, _QUERY_NEIGHBORS, ids=batch):
            neighbors.setdefault(str(row["id"]), set()).add(str(row["neighbor_id"]))
    return neighbors


async def _load_label_namespaces(
    session: AsyncSession, labels: list[str]
) -> dict[tuple[str, str], set[str]]:
    """ns por etiqueta viva: ``(toLower(trim(name)), type) -> namespaces``.

    Los parametros se bajan en Python antes de la comparacion Cypher (la
    clave devuelta ya viene bajada por ``toLower``), misma coercion T9.
    """
    groups: dict[tuple[str, str], set[str]] = {}
    lowered = sorted({label.strip().lower() for label in labels})
    for batch in _batched(lowered):
        for row in await _dicts(session, _QUERY_LABEL_NAMESPACES, labels=batch):
            key = (str(row["group_key"]), str(row["type"]))
            groups.setdefault(key, set()).add(namespace_from_id(str(row["id"])))
    return groups


def _fallback_label(entry: MergeLedgerEntry) -> str:
    """Etiqueta cuando el canonico ya no existe en el grafo: la del ledger."""
    for evidence in entry.evidence:
        if evidence.anchor_id == entry.canonical_id:
            return evidence.anchor_normalized.original
    if entry.evidence:
        return entry.evidence[0].anchor_normalized.original
    return entry.canonical_id.rsplit(":", 1)[-1]


def _fallback_type(entry: MergeLedgerEntry) -> str:
    for evidence in entry.evidence:
        if evidence.anchor_id == entry.canonical_id:
            return str(evidence.anchor_type)
    if entry.evidence:
        return str(entry.evidence[0].anchor_type)
    return ""


def _candidate_state(merged_into_value: str, canonical_id: str) -> str:
    """Estado actual del duplicado tras el merge (soft-deleted hacia quien)."""
    if not merged_into_value:
        return "activo (NO soft-deleted)"
    if merged_into_value == canonical_id:
        return f"soft-deleted -> {canonical_id}"
    return f"soft-deleted -> {merged_into_value}"


def _namespace_count(
    label: str,
    entity_type: str,
    label_namespaces: dict[tuple[str, str], set[str]],
    canonical_namespace: str,
    candidate_namespace: str,
) -> tuple[int, bool]:
    """``(namespace_count, fallback)`` para ``label_risk`` — leido del grafo.

    Fallback = la etiqueta no aparece entre las vivas (p. ej. canonico fuera
    del grupo activo): se usa el tamanio del propio par (2 en la seleccion
    cross-namespace), el piso honesto que observa la evidencia.
    """
    group = label_namespaces.get((label.strip().lower(), entity_type))
    if group:
        return len(group), False
    return len({canonical_namespace, candidate_namespace}), True


def _build_pairs(
    entries: Sequence[MergeLedgerEntry],
    *,
    entities: dict[str, Entity],
    merged_into: dict[str, str],
    mentions: dict[str, set[str]],
    neighbors: dict[str, set[str]],
    label_namespaces: dict[tuple[str, str], set[str]],
    rolled_back_seqs: set[int],
    namespace_filter: str | None,
) -> tuple[list[PairAudit], int, int]:
    """Cruza ledger + grafo en pares auditados.

    Devuelve ``(pares, pares_con_entidad_ausente, pares_con_fallback)``: el
    fallback cuenta los pares cuya etiqueta no esta entre las vivas (el
    namespace_count cae al piso honesto de 2 = el propio par).
    """
    pairs: list[PairAudit] = []
    missing = 0
    fallbacks = 0
    for entry in entries:
        canonical_namespace = namespace_from_id(entry.canonical_id)
        crossing = _crossing_candidate_ids(entry, canonical_namespace)
        canonical_entity = entities.get(entry.canonical_id)
        label = canonical_entity.name if canonical_entity else _fallback_label(entry)
        entity_type = str(canonical_entity.type) if canonical_entity else _fallback_type(entry)
        for candidate_id in crossing:
            candidate_namespace = namespace_from_id(candidate_id)
            if namespace_filter is not None and namespace_filter not in (
                canonical_namespace,
                candidate_namespace,
            ):
                continue
            candidate_entity = entities.get(candidate_id)
            entity_missing = canonical_entity is None or candidate_entity is None
            if entity_missing:
                missing += 1
            canonical_description = canonical_entity.description if canonical_entity else ""
            candidate_description = candidate_entity.description if candidate_entity else ""
            overlap = description_overlap(canonical_description, candidate_description)
            mentions_j, _, _ = mentions_jaccard(
                mentions.get(entry.canonical_id, set()),
                mentions.get(candidate_id, set()),
            )
            related_j, _, _ = related_jaccard(
                neighbors.get(entry.canonical_id, set()),
                neighbors.get(candidate_id, set()),
            )
            ns_count, used_fallback = _namespace_count(
                label, entity_type, label_namespaces, canonical_namespace, candidate_namespace
            )
            if used_fallback:
                fallbacks += 1
            pairs.append(
                PairAudit(
                    seq=entry.seq,
                    canonical_id=entry.canonical_id,
                    candidate_id=candidate_id,
                    canonical_namespace=canonical_namespace,
                    candidate_namespace=candidate_namespace,
                    label=label,
                    description_overlap=overlap,
                    risk=label_risk(single_word=is_generic_label(label), namespace_count=ns_count),
                    lexically_silent=is_lexically_silent(
                        overlap, canonical_description, candidate_description
                    ),
                    applied_at=entry.applied_at,
                    approver=entry.approver,
                    stored_band=entry.band.value,
                    stored_evidence_s3_none=all(ev.s3 is None for ev in entry.evidence),
                    rolled_back=entry.seq in rolled_back_seqs,
                    entity_type=entity_type,
                    mentions_jaccard=mentions_j,
                    related_jaccard=related_j,
                    canonical_description=canonical_description,
                    candidate_description=candidate_description,
                    canonical_page=canonical_entity.source_page if canonical_entity else None,
                    candidate_page=candidate_entity.source_page if candidate_entity else None,
                    canonical_books=tuple(sorted(mentions.get(entry.canonical_id, set()))),
                    candidate_books=tuple(sorted(mentions.get(candidate_id, set()))),
                    candidate_state=_candidate_state(
                        merged_into.get(candidate_id, ""), entry.canonical_id
                    ),
                    missing_entities=entity_missing,
                )
            )
    return pairs, missing, fallbacks


def _history_row(entry: MergeLedgerEntry, rolled_back: bool) -> dict[str, Any]:
    """Fila historica por entrada (ledger puro): lo que el bypass dejo guardado."""
    return {
        "seq": entry.seq,
        "applied_at": entry.applied_at.isoformat(),
        "approver": entry.approver,
        "stored_band": entry.band.value,
        "stored_evidence_s3_none": all(ev.s3 is None for ev in entry.evidence),
        "rolled_back": rolled_back,
        "canonical_id": entry.canonical_id,
        "candidate_ids": list(entry.candidate_ids),
        "crossing_candidate_ids": _crossing_candidate_ids(
            entry, namespace_from_id(entry.canonical_id)
        ),
    }


def _pair_payload(pair: PairAudit, thresholds: BandThresholds) -> dict[str, Any]:
    """Payload JSON de un par: hechos historicos + evidencia recomputada."""
    reading = reading_for(pair.description_overlap, thresholds)
    return {
        "seq": pair.seq,
        "applied_at": pair.applied_at.isoformat() if pair.applied_at else None,
        "approver": pair.approver,
        "stored_band": pair.stored_band,
        "stored_evidence_s3_none": pair.stored_evidence_s3_none,
        "rolled_back": pair.rolled_back,
        "canonical_id": pair.canonical_id,
        "candidate_id": pair.candidate_id,
        "canonical_namespace": pair.canonical_namespace,
        "candidate_namespace": pair.candidate_namespace,
        "label": pair.label,
        "entity_type": pair.entity_type,
        "evidence": {
            PRIMARY_SIGNAL: pair.description_overlap,
            "reading": reading.value,
            "strength": _strength_column(
                pair.description_overlap, pair.lexically_silent, thresholds
            ),
            "mentions_jaccard": pair.mentions_jaccard,
            "related_jaccard": pair.related_jaccard,
            "primary_signal": PRIMARY_SIGNAL,
            "structural_signals": list(STRUCTURAL_SIGNALS),
            "lexically_silent": pair.lexically_silent,
            "cosine_computed": False,
            "thresholds": {
                "high_context": thresholds.high_context,
                "conflict_floor": thresholds.conflict_floor,
            },
            "risk": pair.risk.model_dump(mode="json"),
            "risk_marker": format_risk_marker(pair.risk),
        },
        "canonical": {
            "description": mention_snippet(pair.canonical_description),
            "source_page": pair.canonical_page,
            "mention_books": list(pair.canonical_books),
        },
        "candidate": {
            "description": mention_snippet(pair.candidate_description),
            "source_page": pair.candidate_page,
            "mention_books": list(pair.candidate_books),
        },
        "candidate_state": pair.candidate_state,
        "missing_entities": pair.missing_entities,
    }


def _print_history(
    entries_total: int,
    selected: Sequence[MergeLedgerEntry],
    rolled_back_seqs: set[int],
    pair_count: int,
    skipped_entities: int,
    missing_pairs: int,
    fallback_pairs: int,
) -> None:
    band_counts = Counter(entry.band.value for entry in selected)
    s3_none = sum(1 for entry in selected if all(ev.s3 is None for ev in entry.evidence))
    rolled_back = sum(1 for entry in selected if entry.seq in rolled_back_seqs)
    print("\n-- SELECCION (ledger, sin grafo) --")
    print(f"  entradas leidas: {entries_total}")
    print(f"  cross-namespace: {len(selected)} (esperado {EXPECTED_CROSS_NAMESPACE_ENTRIES}) — OK")
    print(f"  pares (canonical -> candidato que cruza): {pair_count}")
    print("\n-- HISTORICO GUARDADO (lo que dejo el bypass) --")
    print(
        "  banda guardada: "
        + " | ".join(
            f"{band} {band_counts.get(band, 0)}"
            for band in (MergeBand.EXACT.value, MergeBand.HIGH.value, MergeBand.MEDIUM.value)
        )
    )
    print(
        f"  evidencia con s3 is None: {s3_none}/{len(selected)} "
        "(el bypass no computo S3: sin scoring almacenado)"
    )
    print(f"  entradas seleccionadas ya revertidas por rollback: {rolled_back}")
    print("\n-- EVIDENCIA RECOMPUTADA HOY (grafo, solo MATCH) --")
    if skipped_entities:
        print(f"  AVISO: {skipped_entities} entidades ignoradas (type fuera del contrato)")
    print(f"  pares con alguna entidad ausente del grafo: {missing_pairs}")
    print(f"  pares con namespace_count en fallback (etiqueta no viva): {fallback_pairs}")


def _print_matrix(strat: Stratification) -> None:
    print("\n-- MATRIZ: riesgo de etiqueta x fuerza de evidencia --")
    header = (
        f"  {'riesgo':<8}"
        f"{'strong':>8}{'ambiguous':>11}{'none_silent':>13}{'none_rest':>11}{'total':>8}"
    )
    print(header)
    for level in RiskLevel:
        counts = strat.matrix[level]
        print(
            f"  {level.value:<8}"
            f"{counts['strong']:>8}{counts['ambiguous']:>11}"
            f"{counts['none_silent']:>13}{counts['none_rest']:>11}"
            f"{sum(counts.values()):>8}"
        )
    column_totals = {
        column: sum(strat.matrix[level][column] for level in RiskLevel)
        for column in STRENGTH_COLUMNS
    }
    print(
        f"  {'total':<8}"
        f"{column_totals['strong']:>8}{column_totals['ambiguous']:>11}"
        f"{column_totals['none_silent']:>13}{column_totals['none_rest']:>11}"
        f"{sum(column_totals.values()):>8}"
    )


def _print_pair(pair: PairAudit, thresholds: BandThresholds) -> None:
    reading = reading_for(pair.description_overlap, thresholds)
    marker = format_risk_marker(pair.risk) or "—"
    print(
        f"    seq {pair.seq} · overlap {pair.description_overlap:.3f} "
        f"· lectura {reading.value} · band {pair.stored_band} "
        f"· s3 {'is None' if pair.stored_evidence_s3_none else 'presente'}"
    )
    print(f"    riesgo {marker} · estado candidato: {pair.candidate_state}")
    print(f"    {pair.canonical_id}  ->  {pair.candidate_id}")
    print(
        f"      desc canon [{pair.canonical_namespace}]: "
        f"{mention_snippet(pair.canonical_description) or '(sin descripcion)'}"
    )
    print(
        f"      desc cand. [{pair.candidate_namespace}]: "
        f"{mention_snippet(pair.candidate_description) or '(sin descripcion)'}"
    )


def _print_list(
    title: str,
    pairs: Sequence[PairAudit],
    limit: int,
    thresholds: BandThresholds,
) -> None:
    shown = pairs[:limit]
    suffix = "" if len(pairs) <= limit else f" — mostrando {limit} de {len(pairs)}"
    print(f"\n  [{title}] — {len(pairs)} pares{suffix}")
    for pair in shown:
        _print_pair(pair, thresholds)
    if not pairs:
        print("    (ninguno)")


def _write_payload(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False, default=str)
        handle.write("\n")


async def main() -> None:
    args = _parse_args()
    settings = Settings.model_validate({})
    thresholds = BandThresholds()

    ledger_path = args.ledger if args.ledger is not None else settings.merge_ledger_path
    entries = JSONLMergeLedger(ledger_path).read_all()
    selected = _select_crossing_entries(entries)
    _assert_expected_count(len(selected))
    # Los seq con un entrada compensatoria: el par ya fue revertido.
    rolled_back_targets: set[int] = set()
    for entry in entries:
        if entry.rollback_of is not None:
            rolled_back_targets.add(entry.rollback_of)

    # Pares ya filtrados por --namespace, antes de tocar el grafo.
    planned: list[tuple[MergeLedgerEntry, str]] = []
    for entry in selected:
        canonical_namespace = namespace_from_id(entry.canonical_id)
        for candidate_id in _crossing_candidate_ids(entry, canonical_namespace):
            candidate_namespace = namespace_from_id(candidate_id)
            if args.namespace is not None and args.namespace not in (
                canonical_namespace,
                candidate_namespace,
            ):
                continue
            planned.append((entry, candidate_id))

    ids = sorted(
        {entity_id for entry, _ in planned for entity_id in (entry.canonical_id,)}
        | {candidate for _, candidate in planned}
    )

    entities: dict[str, Entity] = {}
    merged_into: dict[str, str] = {}
    mentions: dict[str, set[str]] = {}
    neighbors: dict[str, set[str]] = {}
    label_namespaces: dict[tuple[str, str], set[str]] = {}
    skipped_entities = 0
    if ids:
        driver = AsyncGraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value()),
        )
        try:
            async with driver.session() as session:
                entities, merged_into, skipped_entities = await _load_entities(session, ids)
                mentions = await _load_mentions(session, ids)
                neighbors = await _load_neighbors(session, ids)
                real_labels = sorted(
                    {entities[entity_id].name for entity_id in ids if entity_id in entities}
                )
                label_namespaces = await _load_label_namespaces(session, real_labels)
        finally:
            await driver.close()

    pairs, missing_pairs, fallback_pairs = _build_pairs(
        selected,
        entities=entities,
        merged_into=merged_into,
        mentions=mentions,
        neighbors=neighbors,
        label_namespaces=label_namespaces,
        rolled_back_seqs=rolled_back_targets,
        namespace_filter=args.namespace,
    )
    stratifiable = [pair for pair in pairs if not pair.missing_entities]
    strat = stratify(stratifiable, thresholds)

    history_rows = [_history_row(entry, entry.seq in rolled_back_targets) for entry in selected]
    band_counts = Counter(entry.band.value for entry in selected)
    s3_none = sum(1 for entry in selected if all(ev.s3 is None for ev in entry.evidence))

    membership: dict[tuple[int, str], list[str]] = {
        (pair.seq, pair.candidate_id): [] for pair in pairs
    }
    for key, group in (
        ("clear_identity", strat.clear_identity),
        ("suspicious", strat.suspicious),
        ("needs_reading", strat.needs_reading),
    ):
        for pair in group:
            membership[(pair.seq, pair.candidate_id)].append(key)

    print("== AUDITORIA RETRO-ACTIVA DE MERGES CROSS-NAMESPACE (SOLO LECTURA) ==")
    print(f"  ledger: {ledger_path} · esquema {AUDIT_SCHEMA}")
    print(
        f"  filtros: namespace={args.namespace or '(todos)'} limit={args.limit} "
        f"· salida: {args.out}"
    )
    print(
        f"  lectura compartida (reading_for sobre {PRIMARY_SIGNAL}): "
        f"strong >= high_context · ambiguous >= conflict_floor · "
        f"none por debajo (umbrales de BandThresholds(), no de este guion)"
    )
    print(
        f"  senales: {PRIMARY_SIGNAL} = primaria; "
        f"{', '.join(STRUCTURAL_SIGNALS)} = estructurales (re-auditoria post-merge)"
    )
    print("  cosine/embeddings: NO computado (sin llamadas de modelo)")
    print("  consultas: solo MATCH (garantia de no mutacion)")

    _print_history(
        entries_total=len(entries),
        selected=selected,
        rolled_back_seqs=rolled_back_targets,
        pair_count=len(pairs),
        skipped_entities=skipped_entities,
        missing_pairs=missing_pairs,
        fallback_pairs=fallback_pairs,
    )
    _print_matrix(strat)

    print(f"\n-- LISTAS (limit={args.limit}; el JSON conserva todos) --")
    _print_list(
        "1) EVIDENCIA CLARA DE IDENTIDAD (strong)", strat.clear_identity, args.limit, thresholds
    )
    _print_list(
        "2) SOSPECHOSOS: posible merge erroneo (riesgo sin evidencia)",
        strat.suspicious,
        args.limit,
        thresholds,
    )
    _print_list(
        "3) NECESITA LECTURA (lexically_silent — aqui ayudaria el cosine)",
        strat.needs_reading,
        args.limit,
        thresholds,
    )

    payload = {
        "schema": AUDIT_SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(),
        "read_only": True,
        "query_classes": ["MATCH"],
        "cosine_computed": False,
        "routing_policy": "cross-namespace pairs are always quarantine (R6.2)",
        "ledger_path": str(ledger_path),
        "namespace_filter": args.namespace,
        "limit_console": args.limit,
        "thresholds": {
            "high_context": thresholds.high_context,
            "conflict_floor": thresholds.conflict_floor,
        },
        "lexical_silence_min_chars": LEXICAL_SILENCE_MIN_CHARS,
        "selection": {
            "entries_total": len(entries),
            "cross_namespace_entries": len(selected),
            "expected_cross_namespace_entries": EXPECTED_CROSS_NAMESPACE_ENTRIES,
            "pairs_total": len(pairs),
            "skipped_entities": skipped_entities,
            "pairs_missing_entities": missing_pairs,
            "pairs_label_namespace_fallback": fallback_pairs,
        },
        "history": {
            "band_counts": dict(band_counts),
            "evidence_s3_none_entries": s3_none,
            "rolled_back_entries": sum(1 for entry in selected if entry.seq in rolled_back_targets),
            "entries": history_rows,
        },
        "stratification": {
            "matrix": {level.value: dict(counts) for level, counts in strat.matrix.items()},
            "totals": strat.totals,
            "strength_columns": list(STRENGTH_COLUMNS),
            "lists": {
                key: [pair.seq for pair in group]
                for key, group in (
                    ("clear_identity", strat.clear_identity),
                    ("suspicious", strat.suspicious),
                    ("needs_reading", strat.needs_reading),
                )
            },
        },
        "pairs": [
            {
                **_pair_payload(pair, thresholds),
                "lists": membership[(pair.seq, pair.candidate_id)],
            }
            for pair in pairs
        ],
    }
    _write_payload(args.out, payload)

    print("\n-- TOTALES POR ESTRATO --")
    for key in ("risk_high", "risk_medium", "risk_none"):
        print(f"  {key}: {strat.totals[key]}")
    for key in _LIST_KEYS:
        print(f"  {key}: {strat.totals[key]}")
    print(f"  pares totales: {len(pairs)}")
    print(f"  JSON: {args.out}")
    print("  grafo sin mutar: solo se ejecutaron consultas MATCH")


if __name__ == "__main__":
    asyncio.run(main())
