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
los dos primeros componentes del id separados por ``:``), EXCLUYENDO las
entradas compensatorias que un rollback appendicea (``rollback_of`` set — el
``compensates_seq`` del diseno): esas replican los ids de su original y
TAMBIEN parecen cross-namespace, asi que se excluyen de la seleccion y se
cuentan aparte. Las originales compensadas se conservan en la seleccion y su
flag ``rolled_back`` es **por par** (T8f.2): una compensatoria solo cubre los
candidatos que registro, asi que un rollback parcial deja los pares hermanos
en la poblacion auditada y solo marca los que si revirtio. El guard valida la
verdad invariable ``originales cross-namespace == 302``
(``EXPECTED_CROSS_NAMESPACE_ENTRIES``, ground truth 2026-10-03) y falla con el
numero en el mensaje si no cierra — no ``aplicadas + compensatorias``, que
dejo de ser invariante cuando el rollback parcial compensa un subconjunto.

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
    lexico no puede juzgar por si solo y el humano tiene que LEER.
    Mas corto que el piso no es "silencioso", es una descripcion que no
    llega a informar.
  - senal de idioma (T8b): ``language_note`` del modelo compartido separa
    dos silencios opuestos — ``different_languages`` (libros en distinto
    idioma: el numero lexico no decide, y ahi pagaria el cosine opcional) y
    ``same_language`` (mismo idioma con silencio total: evidencia CONTRA la
    identidad). Si el detector conservador no puede llamar algun lado
    (``unknown``) el par NINGUNA conclusion: sigue siendo de lectura.
  - la fuerza de la evidencia la da ``reading_for`` + ``BandThresholds()``;
    este guion no define umbrales ni etiquetas de lectura ni logica de
    idioma propias.

ESTRATIFICACION: matriz riesgo de etiqueta (high/medium/none) x fuerza de
evidencia (strong / ambiguous; dentro de ``none`` el silencio lexico se
separa por idioma en ``silent_cross_language`` / ``silent_same_language`` /
``silent_language_unknown``, y el resto va a ``none_rest``). Listas:

  - ``clear_identity``: evidencia fuerte, overlap descendente;
  - ``suspicious``: riesgo high/medium con evidencia debil o ausente, MAS
    todo ``silent_same_language`` (el silencio mismo-idioma con descripciones
    sustanciales es evidencia contra el merge, con o sin riesgo de etiqueta)
    — los candidatos a merge erroneo;
  - ``needs_reading``: ``silent_cross_language`` y ``silent_language_unknown``
    — el corto donde ayudaria el cosine y donde el numero no decide;
    ``--limit`` acota las listas de consola, el JSON siempre lleva todo.

La matriz y las tres listas describen la poblacion TODAVIA APLICADA: los
pares que un rollback revirtio quedan fuera de los estratos (la consola los
reporta revertidos via ``rolled_back`` y el contador de compensatorias junto
a la seleccion). Un rollback PARCIAL (T8f) saca solo los pares que si
revirtio: los candidatos hermanos de la misma entrada siguen auditados.

Decisiones humanas (T9a): un par cuyo ``keep`` el maintainer decidio
documentar (registro ``Settings.cross_namespace_decisions_path``) TAMBIEN
sale del estrato — mantenerlo fusionado es una decision humana, no un par
sospechoso. Las claves se leen con el modelo compartido ``keep_pair_keys``
(nunca reimplementado): archivo ausente = sin decisiones; archivo corrupto
= exit 2 ANTES de tocar el grafo. La consola y el payload JSON reportan
``decididos keep`` junto a los contadores de rollback.

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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

from neo4j import AsyncGraphDatabase, AsyncSession
from pydantic import ValidationError

from book_graph_rag.config import Settings
from book_graph_rag.domain.cross_namespace_decision_models import (
    InvalidDecisionRecord,
    keep_pair_keys,
)
from book_graph_rag.domain.merge_ledger_models import MergeBand, MergeLedgerEntry
from book_graph_rag.domain.models import Entity
from book_graph_rag.domain.quarantine_review_models import (
    PRIMARY_SIGNAL,
    STRUCTURAL_SIGNALS,
    EvidenceReading,
    LabelRisk,
    LanguageNote,
    LanguageRelation,
    RiskLevel,
    format_risk_marker,
    is_generic_label,
    label_risk,
    language_note,
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
from book_graph_rag.infrastructure.jsonl_cross_namespace_decisions import (
    JSONLCrossNamespaceDecisions,
)
from book_graph_rag.infrastructure.jsonl_merge_ledger import JSONLMergeLedger

AUDIT_SCHEMA = "applied-cross-namespace-audit/2"
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
#: Columnas de la matriz: dentro de ``none`` el silencio lexico se separa
#: por senal de idioma (T8b: cross / same / sin determinar).
STRENGTH_COLUMNS = (
    "strong",
    "ambiguous",
    "silent_cross_language",
    "silent_same_language",
    "silent_language_unknown",
    "none_rest",
)
#: Ancho de cada columna de la matriz en consola (incluido ``total``).
_COLUMN_WIDTHS: dict[str, int] = {
    "strong": 8,
    "ambiguous": 11,
    "silent_cross_language": 23,
    "silent_same_language": 23,
    "silent_language_unknown": 25,
    "none_rest": 11,
    "total": 8,
}
#: Texto de la relacion de idioma por fila de consola (nada de logica).
_LANGUAGE_RELATION_ES: dict[LanguageRelation, str] = {
    LanguageRelation.SAME_LANGUAGE: "mismo idioma",
    LanguageRelation.DIFFERENT_LANGUAGES: "distintos",
    LanguageRelation.UNKNOWN: "sin determinar",
}
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
    #: True cuando el maintainer decidio KEEP para esta clave (seq, candidato)
    #: — T9a: una decision humana, fuera del estrato sospechoso.
    decided_keep: bool = False
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
    #: Senal de idioma de las dos descripciones (T8b, modelo compartido).
    language: LanguageNote = field(default_factory=LanguageNote)


@dataclass(frozen=True)
class Stratification:
    """Matriz riesgo x fuerza, las tres listas y los totales por estrato.

    Dentro de ``none``, el silencio lexico se estratifica por idioma:
    cross/indeterminado van a ``needs_reading``; mismo-idioma va a
    ``suspicious`` (silencio con descripciones sustanciales en el mismo
    idioma es evidencia contra la identidad — T8b).
    """

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


def _strength_column(pair: PairAudit, thresholds: BandThresholds) -> str:
    """Columna de la matriz con la lectura del modelo compartido (T8b).

    ``strong`` >= ``high_context``; ``ambiguous`` >= ``conflict_floor``;
    por debajo, el silencio lexico (overlap 0 + ambas descripciones
    sustanciales) se separa por la senal de idioma del modelo compartido:

      - ``silent_cross_language``: idiomas distintos — el numero lexico no
        decide, es la lista donde pagaria el cosine (lectura);
      - ``silent_same_language``: mismo idioma — evidencia CONTRA la
        identidad (sospechoso);
      - ``silent_language_unknown``: el detector conservador no pudo llamar
        ningun lado — ninguna conclusion, el humano decide (lectura).

    Ningun umbral ni logica de idioma vive en este guion: ``reading_for``
    y ``language_note`` son del modelo compartido.
    """
    reading = reading_for(pair.description_overlap, thresholds)
    if reading is EvidenceReading.IDENTITY:
        return "strong"
    if reading is EvidenceReading.UNDECIDED:
        return "ambiguous"
    if not pair.lexically_silent:
        return "none_rest"
    if pair.language.relation is LanguageRelation.DIFFERENT_LANGUAGES:
        return "silent_cross_language"
    if pair.language.relation is LanguageRelation.SAME_LANGUAGE:
        return "silent_same_language"
    return "silent_language_unknown"


def stratify(pairs: Sequence[PairAudit], thresholds: BandThresholds) -> Stratification:
    """Cruza riesgo de etiqueta x fuerza de evidencia y arma las tres listas.

    Pura sobre pares ya calculados (sin ledger ni grafo). Memoria: cada par cae
    en exacta una celda de la matriz y en a lo sumo una lista — los fuertes
    van a ``clear_identity``; el silencio cross-language o indeterminado a
    ``needs_reading``; el silencio mismo-idioma a ``suspicious`` (con o sin
    riesgo de etiqueta: el silencio mismo-idioma ES la evidencia); y el resto
    con riesgo high/medium tambien a ``suspicious``.
    """
    matrix: dict[RiskLevel, dict[str, int]] = {
        level: dict.fromkeys(STRENGTH_COLUMNS, 0) for level in RiskLevel
    }
    clear: list[PairAudit] = []
    suspicious: list[PairAudit] = []
    reading: list[PairAudit] = []
    for pair in pairs:
        column = _strength_column(pair, thresholds)
        matrix[pair.risk.level][column] += 1
        if column == "strong":
            clear.append(pair)
        elif column in ("silent_cross_language", "silent_language_unknown"):
            reading.append(pair)
        elif column == "silent_same_language":
            # T8b: silencio mismo-idioma con descripciones sustanciales es
            # evidencia contra la identidad — sospechoso con o sin riesgo.
            suspicious.append(pair)
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
    # Los estratos nuevos del silencio, visibles en los totales (T8b).
    for column in (
        "silent_cross_language",
        "silent_same_language",
        "silent_language_unknown",
    ):
        totals[column] = sum(matrix[level][column] for level in RiskLevel)
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
        "--decisions",
        type=Path,
        default=None,
        help="registro de decisiones cross-namespace (default: "
        "cross_namespace_decisions_path de Settings)",
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
    """Entradas cross-namespace originales: excluye las compensatorias.

    Las entradas que ``ledger rollback`` appendicea (``rollback_of`` set —
    el ``compensates_seq`` del diseno) replican los ids de su original y
    TAMBIEN parecen cross-namespace: no son poblacion aplicada ni parte del
    ground truth. Se excluyen de la seleccion y se cuentan aparte con
    :func:`_compensating_crossing_entries`; las originales compensadas se
    conservan aca para que su flag ``rolled_back`` siga visible en el
    informe (historial y payload por par).
    """
    selected: list[MergeLedgerEntry] = []
    for entry in entries:
        if entry.rollback_of is not None:
            continue
        canonical_namespace = namespace_from_id(entry.canonical_id)
        if _crossing_candidate_ids(entry, canonical_namespace):
            selected.append(entry)
    return selected


def _compensating_crossing_entries(
    entries: Sequence[MergeLedgerEntry],
) -> list[MergeLedgerEntry]:
    """Entradas compensatorias cross-namespace (``rollback_of`` set), aparte."""
    compensating: list[MergeLedgerEntry] = []
    for entry in entries:
        if entry.rollback_of is None:
            continue
        canonical_namespace = namespace_from_id(entry.canonical_id)
        if _crossing_candidate_ids(entry, canonical_namespace):
            compensating.append(entry)
    return compensating


def _compensated_candidates(
    compensating: Sequence[MergeLedgerEntry],
) -> dict[int, set[str]]:
    """seq original -> union de candidatos que un rollback ya revirtio (T8f.2).

    Una entrada compensatoria solo cubre los candidatos que registro: un
    rollback parcial (``ledger rollback --candidate``) revierte un
    subconjunto, asi que dos compensaciones del mismo seq se agregan y el
    resto de la entrada sigue aplicado.
    """
    compensated: dict[int, set[str]] = {}
    for entry in compensating:
        if entry.rollback_of is None:
            continue
        compensated.setdefault(entry.rollback_of, set()).update(entry.candidate_ids)
    return compensated


def _crossing_candidates(entry: MergeLedgerEntry) -> set[str]:
    """Candidatos cross-namespace de una entrada (normalizado a ``set``)."""
    return set(_crossing_candidate_ids(entry, namespace_from_id(entry.canonical_id)))


def _is_fully_compensated(entry: MergeLedgerEntry, compensated: Mapping[int, set[str]]) -> bool:
    """True cuando TODOS los candidatos cruzados de la entrada ya se revirtieron."""
    crossing = _crossing_candidates(entry)
    return bool(crossing) and crossing.issubset(compensated.get(entry.seq, set()))


def _is_partially_compensated(entry: MergeLedgerEntry, compensated: Mapping[int, set[str]]) -> bool:
    """True cuando ALGUN candidato cruzado se revirtio y otro sigue aplicado."""
    crossing = _crossing_candidates(entry)
    covered = crossing & compensated.get(entry.seq, set())
    return bool(covered) and not crossing.issubset(covered)


def _applied_entries(
    selected: Sequence[MergeLedgerEntry],
    compensated: Mapping[int, set[str]],
) -> list[MergeLedgerEntry]:
    """Originales que conservan AL MENOS UN candidato cruzado sin revertir.

    Un rollback parcial revierte solo su subconjunto: la entrada sigue en la
    poblacion aplicada (con sus pares restantes) hasta que TODOS sus
    candidatos cross-namespace estan compensados.
    """
    return [entry for entry in selected if not _is_fully_compensated(entry, compensated)]


def _assert_expected_count(originals: int, compensating: int) -> None:
    """Falla fuerte si la poblacion de ORIGINALES cross-namespace no es 302.

    Invariante append-only: las originales del 2026-10-03 nunca se borran y
    cada compensatoria (total o parcial) referencia a UNA de ellas. Se cuentan
    las ORIGINALES, no ``aplicadas + compensatorias``: esa suma dejo de ser
    invariante cuando el rollback parcial (T8f) compensa solo un subconjunto
    de una entrada. Si no cierra, el ledger cambio de una forma que este guion
    no entiende: aborta con el numero en el mensaje antes de consultar el
    grafo ni escribir salida.
    """
    if originals == EXPECTED_CROSS_NAMESPACE_ENTRIES:
        return
    print(
        f"ERROR: seleccion cross-namespace = {originals} originales "
        f"(+ {compensating} entradas compensatorias), esperado "
        f"{EXPECTED_CROSS_NAMESPACE_ENTRIES} (ground truth 2026-10-03). "
        "El ledger cambio: revisar antes de confiar en este informe. "
        "No se consulto el grafo ni se escribio ninguna salida.",
        file=sys.stderr,
    )
    raise SystemExit(2)


def _abort_ledger_drift(reason: str) -> NoReturn:
    """Aborta (exit 2) antes del grafo y antes de escribir ``--out``."""
    print(
        f"ERROR: {reason}. El ledger cambio: revisar antes de confiar en este "
        "informe. No se consulto el grafo ni se escribio salida.",
        file=sys.stderr,
    )
    raise SystemExit(2)


def _load_keep_pair_keys(path: Path) -> set[tuple[int, str]]:
    """Claves decididas ``keep`` via el modelo compartido T9a (nada propio).

    Lee el registro con el adaptador JSONL compartido y deriva las claves con
    ``keep_pair_keys`` (``domain/cross_namespace_decision_models``): la
    ULTIMA decision por ``(seq, candidate_id)`` gana. Archivo ausente = sin
    decisiones (conjunto vacio); archivo corrupto = fallo fuerte (exit 2)
    ANTES de tocar el grafo — una decision ilegible no es "sin decision".
    """
    try:
        records = JSONLCrossNamespaceDecisions(path).read_all()
    except InvalidDecisionRecord as exc:
        print(
            f"ERROR: registro de decisiones corrupto ({exc}). Revisar antes "
            "de confiar en este informe: no se consulto el grafo ni se "
            "escribio salida.",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc
    return keep_pair_keys(records)


def _assert_compensations_reference_originals(
    selected: Sequence[MergeLedgerEntry],
    compensating: Sequence[MergeLedgerEntry],
) -> None:
    """Cuadra el libro de compensaciones contra el ground truth.

    Falla fuerte (sin tocar el grafo) si un ``rollback_of`` no es el seq de una
    original cross-namespace seleccionada; si su ``canonical_id`` no coincide
    con el de esa original; si declara un candidato que la original no tenia; o
    si dos compensatorias del mismo seq vuelven a compensar el mismo candidato
    (doble contabilidad: el par se revirtio una sola vez). El guard de
    poblacion es solo de conteo, asi que estas comprobaciones son la unica red
    que detecta una compensacion duplicada o mal apuntada.
    """
    originals = {entry.seq: entry for entry in selected}
    counted: dict[int, set[str]] = {}
    for entry in compensating:
        target = entry.rollback_of
        if target is None or target not in originals:
            _abort_ledger_drift(
                f"la entrada compensatoria seq={entry.seq} referencia "
                f"rollback_of={target}, que no es una original cross-namespace "
                "seleccionada"
            )
        original = originals[target]
        if entry.canonical_id != original.canonical_id:
            _abort_ledger_drift(
                f"la entrada compensatoria seq={entry.seq} apunta a "
                f"rollback_of={target} pero su canonical_id={entry.canonical_id} "
                f"no coincide con {original.canonical_id}"
            )
        unknown = sorted(set(entry.candidate_ids) - set(original.candidate_ids))
        if unknown:
            _abort_ledger_drift(
                f"la entrada compensatoria seq={entry.seq} declara candidatos "
                f"que la original seq={target} no tenia: {unknown}"
            )
        already = counted.setdefault(target, set())
        repeated = sorted(already & set(entry.candidate_ids))
        if repeated:
            _abort_ledger_drift(
                f"la entrada compensatoria seq={entry.seq} vuelve a compensar "
                f"candidatos ya compensados en la original seq={target}: {repeated}"
            )
        already.update(entry.candidate_ids)


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
    compensated_candidates: Mapping[int, set[str]],
    keep_pair_keys: set[tuple[int, str]],
    namespace_filter: str | None,
) -> tuple[list[PairAudit], int, int]:
    """Cruza ledger + grafo en pares auditados.

    Devuelve ``(pares, pares_con_entidad_ausente, pares_con_fallback)``: el
    fallback cuenta los pares cuya etiqueta no esta entre las vivas (el
    namespace_count cae al piso honesto de 2 = el propio par).
    ``keep_pair_keys`` (T9a, leido con el modelo compartido) marca los pares
    que el maintainer decidio mantener fusionados (``decided_keep``).
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
                    language=language_note(canonical_description, candidate_description),
                    applied_at=entry.applied_at,
                    approver=entry.approver,
                    stored_band=entry.band.value,
                    stored_evidence_s3_none=all(ev.s3 is None for ev in entry.evidence),
                    rolled_back=(candidate_id in compensated_candidates.get(entry.seq, set())),
                    decided_keep=(entry.seq, candidate_id) in keep_pair_keys,
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


def _history_row(
    entry: MergeLedgerEntry,
    compensated_candidate_ids: set[str],
) -> dict[str, Any]:
    """Fila historica por entrada (ledger puro): lo que el bypass dejo guardado.

    ``rolled_back`` es total (TODOS los candidatos cruzados revertidos);
    ``rolled_back_candidates`` lista el subconjunto que un rollback parcial
    (T8f) si revirtio, para que el informe no oculte una reversion a medias.
    """
    crossing = _crossing_candidate_ids(entry, namespace_from_id(entry.canonical_id))
    compensated = [candidate for candidate in crossing if candidate in compensated_candidate_ids]
    return {
        "seq": entry.seq,
        "applied_at": entry.applied_at.isoformat(),
        "approver": entry.approver,
        "stored_band": entry.band.value,
        "stored_evidence_s3_none": all(ev.s3 is None for ev in entry.evidence),
        "rolled_back": bool(crossing) and len(compensated) == len(crossing),
        "rolled_back_candidates": compensated,
        "canonical_id": entry.canonical_id,
        "candidate_ids": list(entry.candidate_ids),
        "crossing_candidate_ids": crossing,
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
        "decided_keep": pair.decided_keep,
        "canonical_id": pair.canonical_id,
        "candidate_id": pair.candidate_id,
        "canonical_namespace": pair.canonical_namespace,
        "candidate_namespace": pair.candidate_namespace,
        "label": pair.label,
        "entity_type": pair.entity_type,
        "evidence": {
            PRIMARY_SIGNAL: pair.description_overlap,
            "reading": reading.value,
            "strength": _strength_column(pair, thresholds),
            "mentions_jaccard": pair.mentions_jaccard,
            "related_jaccard": pair.related_jaccard,
            "primary_signal": PRIMARY_SIGNAL,
            "structural_signals": list(STRUCTURAL_SIGNALS),
            "lexically_silent": pair.lexically_silent,
            # T8b: la senal de idioma que separa los dos silencios.
            "language": pair.language.model_dump(mode="json"),
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


def _stratifiable_pairs(pairs: Sequence[PairAudit]) -> list[PairAudit]:
    """Pares de la poblacion todavia aplicada (T8f.2: ``rolled_back`` por par).

    Un rollback parcial solo saca los pares que si revirtio; los candidatos
    hermanos de la misma entrada siguen en el estrato. Los pares con entidades
    ausentes del grafo tampoco se estratifican (el scoring no es comparable).
    Y un par decidido ``keep`` (T9a) sale tambien: mantenerlo fusionado es una
    decision humana documentada, no un par sospechoso.
    """
    return [
        pair
        for pair in pairs
        if not pair.missing_entities and not pair.rolled_back and not pair.decided_keep
    ]


def _print_history(
    entries_total: int,
    selected: Sequence[MergeLedgerEntry],
    compensating_count: int,
    applied_count: int,
    fully_compensated: int,
    partially_compensated: int,
    compensated_pair_count: int,
    decided_keep_pairs: int,
    pair_count: int,
    skipped_entities: int,
    missing_pairs: int,
    fallback_pairs: int,
) -> None:
    band_counts = Counter(entry.band.value for entry in selected)
    s3_none = sum(1 for entry in selected if all(ev.s3 is None for ev in entry.evidence))
    print("\n-- SELECCION (ledger, sin grafo) --")
    print(f"  entradas leidas: {entries_total}")
    print(
        f"  cross-namespace: {len(selected)} originales · "
        f"compensatorias: {compensating_count} · aplicadas: {applied_count}"
    )
    print(f"  guard: originales {len(selected)} (esperado {EXPECTED_CROSS_NAMESPACE_ENTRIES}) — OK")
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
    print(
        f"  pares revertidos por rollback: {compensated_pair_count} · "
        f"entradas con rollback total: {fully_compensated} · "
        f"con rollback parcial: {partially_compensated} · "
        f"decididos keep: {decided_keep_pairs}"
    )
    print("\n-- EVIDENCIA RECOMPUTADA HOY (grafo, solo MATCH) --")
    if skipped_entities:
        print(f"  AVISO: {skipped_entities} entidades ignoradas (type fuera del contrato)")
    print(f"  pares con alguna entidad ausente del grafo: {missing_pairs}")
    print(f"  pares con namespace_count en fallback (etiqueta no viva): {fallback_pairs}")


def _print_matrix(strat: Stratification) -> None:
    print("\n-- MATRIZ: riesgo de etiqueta x fuerza de evidencia --")
    header = f"  {'riesgo':<8}" + "".join(
        f"{column:>{_COLUMN_WIDTHS[column]}}" for column in (*STRENGTH_COLUMNS, "total")
    )
    print(header)
    for level in RiskLevel:
        counts = strat.matrix[level]
        cells = "".join(
            f"{counts[column]:>{_COLUMN_WIDTHS[column]}}" for column in STRENGTH_COLUMNS
        )
        print(f"  {level.value:<8}{cells}{sum(counts.values()):>8}")
    column_totals = {
        column: sum(strat.matrix[level][column] for level in RiskLevel)
        for column in STRENGTH_COLUMNS
    }
    cells = "".join(
        f"{column_totals[column]:>{_COLUMN_WIDTHS[column]}}" for column in STRENGTH_COLUMNS
    )
    print(f"  {'total':<8}{cells}{sum(column_totals.values()):>8}")


def _print_pair(pair: PairAudit, thresholds: BandThresholds) -> None:
    reading = reading_for(pair.description_overlap, thresholds)
    marker = format_risk_marker(pair.risk) or "—"
    print(
        f"    seq {pair.seq} · overlap {pair.description_overlap:.3f} "
        f"· lectura {reading.value} · band {pair.stored_band} "
        f"· s3 {'is None' if pair.stored_evidence_s3_none else 'presente'}"
    )
    print(f"    riesgo {marker} · estado candidato: {pair.candidate_state}")
    print(
        f"    idioma: {pair.language.anchor}/{pair.language.candidate} · "
        f"{_LANGUAGE_RELATION_ES[pair.language.relation]}"
    )
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

    decisions_path = (
        args.decisions if args.decisions is not None else settings.cross_namespace_decisions_path
    )
    # T9a: leer el registro de decisiones ANTES del ledger y del grafo — un
    # archivo corrupto falla fuerte (exit 2) sin haber tocado nada.
    keep_keys = _load_keep_pair_keys(decisions_path)

    ledger_path = args.ledger if args.ledger is not None else settings.merge_ledger_path
    entries = JSONLMergeLedger(ledger_path).read_all()
    selected = _select_crossing_entries(entries)
    compensating = _compensating_crossing_entries(entries)
    compensated = _compensated_candidates(compensating)
    applied = _applied_entries(selected, compensated)
    _assert_expected_count(len(selected), len(compensating))
    _assert_compensations_reference_originals(selected, compensating)
    fully_compensated_entries = sum(
        1 for entry in selected if _is_fully_compensated(entry, compensated)
    )
    partially_compensated_entries = sum(
        1 for entry in selected if _is_partially_compensated(entry, compensated)
    )

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
        compensated_candidates=compensated,
        keep_pair_keys=keep_keys,
        namespace_filter=args.namespace,
    )
    # La matriz y las tres listas describen la todavia aplicada: un par
    # revertido sale del estrato (su flag ``rolled_back`` sigue vivo en el
    # payload por par y en las filas historicas). Un rollback PARCIAL solo
    # saca los pares que si revirtio; los hermanos siguen auditados. Un par
    # decidido ``keep`` (T9a) sale tambien: es una decision humana.
    stratifiable = _stratifiable_pairs(pairs)
    strat = stratify(stratifiable, thresholds)
    decided_keep_count = sum(1 for pair in pairs if pair.decided_keep)

    history_rows = [_history_row(entry, compensated.get(entry.seq, set())) for entry in selected]
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
    print(f"  decisiones humanas: {decisions_path} · claves keep: {len(keep_keys)}")
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
    print(
        "  senal de idioma: language_note() del modelo compartido (detector "
        "conservador de solo-stopwords del corpus bilingue; unknown = no decide)"
    )
    print("  cosine/embeddings: NO computado (sin llamadas de modelo)")
    print("  consultas: solo MATCH (garantia de no mutacion)")

    _print_history(
        entries_total=len(entries),
        selected=selected,
        compensating_count=len(compensating),
        applied_count=len(applied),
        fully_compensated=fully_compensated_entries,
        partially_compensated=partially_compensated_entries,
        compensated_pair_count=sum(1 for pair in pairs if pair.rolled_back),
        decided_keep_pairs=decided_keep_count,
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
        "2) SOSPECHOSOS: posible merge erroneo (riesgo sin evidencia · silencio mismo-idioma)",
        strat.suspicious,
        args.limit,
        thresholds,
    )
    _print_list(
        "3) NECESITA LECTURA (silencio cross-language/indeterminado — aqui ayudaria el cosine)",
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
        "decisions_path": str(decisions_path),
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
            "compensating_entries": len(compensating),
            "applied_cross_namespace_entries": len(applied),
            "fully_compensated_entries": fully_compensated_entries,
            "partially_compensated_entries": partially_compensated_entries,
            "expected_cross_namespace_entries": EXPECTED_CROSS_NAMESPACE_ENTRIES,
            "compensated_pairs": sum(1 for pair in pairs if pair.rolled_back),
            "decided_keep_pairs": decided_keep_count,
            "pairs_total": len(pairs),
            "pairs_stratified": len(stratifiable),
            "skipped_entities": skipped_entities,
            "pairs_missing_entities": missing_pairs,
            "pairs_label_namespace_fallback": fallback_pairs,
        },
        "history": {
            "band_counts": dict(band_counts),
            "evidence_s3_none_entries": s3_none,
            "rolled_back_entries": fully_compensated_entries,
            "rolled_back_pairs": sum(1 for pair in pairs if pair.rolled_back),
            "partially_compensated_entries": partially_compensated_entries,
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
