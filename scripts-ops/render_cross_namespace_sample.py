"""Hoja de decision READ-ONLY de grupos cross-namespace de entidades (T2).

Renderiza la muestra que el maintainer revisa ANTES de aprobar cualquier merge
cross-namespace. El guion es deterministico y estratificado; este script NO
tiene logica de scoring propia: agrupa con ``normalize_key`` (coordinacion T9,
ver mas abajo), elige el anchor con ``choose_canonical_id`` (id mas corto,
empates lexicograficos - la regla canonica del proyecto) y calcula cada
evidencia reutilizando el dominio:

  - S0:  ``s0_match`` (builder publico de ``domain/s0_normalization.py``);
         devuelve ``ResolutionEvidence`` con ``s0_matched_field``,
         ``anchor_namespace``, ``candidate_namespace``, ``cross_namespace`` y
         ``cross_type``. No se reimplementa la normalizacion.
  - S2:  ``s2_type_gate`` (``domain/s2_type_gate.py``).
  - S3:  ``mentions_jaccard``, ``related_jaccard`` y ``description_overlap``
         (``domain/s3_context_scoring.py``); los conjuntos se leen del grafo
         (solo MATCH). ``description_overlap`` es la senal PRIMARIA; los dos
         jaccards son estructurales (~0 pre-merge, solo para re-auditar un
         merge ya aplicado - design 4.1): etiquetas compartidas
         ``PRIMARY_SIGNAL``/``STRUCTURAL_SIGNALS``.
  - S4:  ``composite`` (media de los tres overlaps, ``composite_score``) es
         INFORMATIVA: no califica pares cross-namespace.

Lectura (fuerza de evidencia, JAMAS decision de enrutamiento): delegada al
modelo compartido ``reading_for(description_overlap, BandThresholds())`` de
``domain/quarantine_review_models.py`` (la misma que ``quarantine list|render``
y la CLI), etiquetas ``EvidenceReading``: ``identity``/``undecided``/
``no_shared_context``. El renderer no define umbrales ni etiquetas propias.
El enrutamiento de un par cross-namespace es SIEMPRE cuarentena (policy R6.2):
este sheet solo aporta evidencia para el juicio humano.

NO se computa cosine/embeddings (ninguna llamada de modelo en una lectura), asi
que NO se reclama banda S4: el campo ``s1``/cosine queda explicitamente en
``cosine_computed=false`` en el JSON.

Grupos candidatos: mismo ``normalize_key(name)`` + ``type``, entidades activas
(``merged_into`` NULL o vacio, la convencion de vivo del proyecto), abarcando
>= 2 namespaces (namespace = los dos primeros componentes del id separados por
``:``).

Coordinacion T9 (pendiente, una linea): este script agrupa con Python
``normalize_key`` (NFKC + colapso de espacios) y la auditoria R5a/``quarantine``
con Cypher ``toLower(trim(name))``; ambos producen hoy 456 grupos (2026-10-03)
- unificar con R5b. No cambiar el sample sin decidirlo.

Muestreo estratificado y determinista (``--seed``, default 20261003: la fecha
de la medicion ground-truth de la design; misma semilla => mismo sample):
  estrata 1  TODOS los grupos con etiqueta generica de una palabra (``llm``,
             ``agent``...: el riesgo de colision de etiqueta generica); no se
             limita con ``--limit`` porque es la poblacion prioritaria.
  estrata 2  los pares de mayor grado (grado combinado MENTIONS + RELATED),
             cuota = mitad restante del ``--limit`` (redondeo hacia arriba).
  estrata 3  mix sembrado round-robin sobre los types presentes, cuota =
             mitad restante del ``--limit`` (redondeo hacia abajo).
``--all`` salta el muestreo; ``--type`` / ``--generic-only`` / ``--namespace``
filtran antes de muestrear (``--namespace`` acota los pares a ese namespace
contra todos los demas).

Garantia de solo lectura: unicamente se ejecutan consultas ``MATCH``; nunca
escribe nodos, aristas ni archivos de estado del grafo. El unico archivo que
escribe es el ``--out`` con el payload JSON.

Uso (desde la raiz del repo, con el .env sourceado):
    uv run --no-sync python scripts-ops/render_cross_namespace_sample.py
    uv run --no-sync python scripts-ops/render_cross_namespace_sample.py --all
    uv run --no-sync python scripts-ops/render_cross_namespace_sample.py \\
        --type component --generic-only --limit 5 --seed 42 \\
        --out /tmp/render_components.json
    uv run --no-sync python scripts-ops/render_cross_namespace_sample.py \\
        --namespace knowledge:ai-engineering-huyen
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from neo4j import AsyncGraphDatabase, AsyncSession
from pydantic import ValidationError

from book_graph_rag.config import Settings
from book_graph_rag.domain.audit_models import normalize_key
from book_graph_rag.domain.duplicate_grouping import ENTITY_TYPES, choose_canonical_id
from book_graph_rag.domain.models import Entity, EntityType
from book_graph_rag.domain.quarantine_review_models import (
    PRIMARY_SIGNAL,
    STRUCTURAL_SIGNALS,
    EvidenceReading,
    format_risk_marker,
    label_risk,
    reading_for,
)
from book_graph_rag.domain.resolution_models import S3ContextSignals
from book_graph_rag.domain.s0_normalization import namespace_from_id, s0_match
from book_graph_rag.domain.s2_type_gate import s2_type_gate
from book_graph_rag.domain.s3_context_scoring import (
    description_overlap,
    mentions_jaccard,
    related_jaccard,
    s3_context_score,
)
from book_graph_rag.domain.s4_band_assignment import BandThresholds, composite_score

RENDER_SCHEMA = "cross-namespace-render/2"
DEFAULT_SEED = 20261003  # fecha de la medicion ground-truth (design seccion 1)
DEFAULT_LIMIT = 15
DEFAULT_OUT = Path("/tmp/cross_namespace_render.json")
BATCH_SIZE = 500
DESC_RENDER_LIMIT = 160
SHARED_NEIGHBOR_RENDER_LIMIT = 10
# Etiquetas de lectura heredadas del modelo compartido (nada local).
READING_ORDER = tuple(reading.value for reading in EvidenceReading)

# ── Lectura (solo Cypher MATCH; ninguna decision de dominio vive aca) ─────────

# Vivo = merged_into NULL o vacio: la misma convencion que usan el audit
# (ENDPOINT_*) y scripts-ops/repoint_merged_endpoint_edges.py.
_QUERY_ENTITIES = """
MATCH (e:Entity)
WHERE coalesce(e.merged_into, '') = ''
RETURN e.id AS id, e.name AS name, e.type AS type,
       coalesce(e.description, '') AS description,
       e.source_page AS source_page,
       coalesce(e.aliases, []) AS aliases,
       e.canonical_name AS canonical_name
ORDER BY e.id
"""

# Grados por entidad viva (estrata 2): MENTIONS entrantes + RELATED incidentes.
_QUERY_DEGREE_MENTIONS = (
    "MATCH (c:Chunk)-[:MENTIONS]->(e:Entity) "
    "WHERE coalesce(e.merged_into, '') = '' "
    "RETURN e.id AS id, count(*) AS c"
)
_QUERY_DEGREE_RELATED = (
    "MATCH (a:Entity)-[:RELATED]-(b:Entity) "
    "WHERE coalesce(a.merged_into, '') = '' AND coalesce(b.merged_into, '') = '' "
    "RETURN a.id AS id, count(*) AS c"
)

# Fuentes (libros) que mencionan cada entidad seleccionada. coalesce replica el
# patron de Neo4jNeighborhoodQueryAdapter.mention_sources (chunks legacy sin
# source_id).
_QUERY_MENTION_SOURCES = """
MATCH (c:Chunk)-[:MENTIONS]->(e:Entity)
WHERE e.id IN $ids
RETURN DISTINCT e.id AS id, coalesce(c.source_id, c.book_id) AS source_id
"""

# Vecinos RELATED en AMBAS direcciones (patron no dirigido) con entidades
# vivas unicamente: los nodos con merged_into son carcasas soft-deleted cuyo
# conocimiento vive en el canonico, incluirlos contaminaria el overlap con
# estado legacy (deuda de RELATED colgante ya reportada por mantenimiento).
_QUERY_NEIGHBORS = """
MATCH (e:Entity)-[:RELATED]-(other:Entity)
WHERE e.id IN $ids AND coalesce(other.merged_into, '') = ''
RETURN DISTINCT e.id AS id, other.id AS neighbor_id
"""


@dataclass
class CandidateGroup:
    """Un grupo cross-namespace: mismo normalize_key(name) + type."""

    group_key: str
    entity_type: EntityType
    member_ids: tuple[str, ...]  # orden lexicografico para I/O determinista
    namespaces: frozenset[str]
    single_word: bool
    degree: int  # suma de grados (MENTIONS + RELATED) de los miembros
    stratum: str = ""  # generic | degree | mix | all


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SEED,
        help=f"semilla del muestreo estratificado (default {DEFAULT_SEED}: "
        "fecha de la medicion ground-truth; misma semilla = mismo sample)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"tamano maximo de las estratas 2 y 3 (default {DEFAULT_LIMIT}); "
        "la estrata de etiquetas genericas de una palabra se incluye siempre",
    )
    parser.add_argument(
        "--all",
        dest="render_all",
        action="store_true",
        help="renderiza todos los grupos candidatos (salta el muestreo)",
    )
    parser.add_argument(
        "--type",
        dest="type_filter",
        choices=sorted(ENTITY_TYPES),
        default=None,
        help="un solo EntityType (los ocho/nueve tipos del grafo)",
    )
    parser.add_argument(
        "--generic-only",
        action="store_true",
        help="solo grupos cuya etiqueta normalizada es una palabra",
    )
    parser.add_argument(
        "--namespace",
        default=None,
        help="acota los pares a <namespace> contra todos los demas "
        "(p. ej. knowledge:ai-engineering-huyen)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"payload JSON completo (default: {DEFAULT_OUT})",
    )
    return parser.parse_args()


async def _dicts(session: AsyncSession, query: str, **params: Any) -> list[dict[str, Any]]:
    result = await session.run(query, **params)
    return [record.data() async for record in result]


async def _load_entities(session: AsyncSession) -> tuple[dict[str, Entity], int]:
    entities: dict[str, Entity] = {}
    skipped = 0
    for row in await _dicts(session, _QUERY_ENTITIES):
        try:
            entity = Entity.model_validate(row)
        except ValidationError:
            skipped += 1  # tipos legacy fuera del contrato EntityType: se reporta
            continue
        entities[entity.id] = entity
    return entities, skipped


async def _load_degrees(session: AsyncSession) -> dict[str, int]:
    degrees: dict[str, int] = {}
    for row in await _dicts(session, _QUERY_DEGREE_MENTIONS):
        degrees[str(row["id"])] = int(row["c"])
    for row in await _dicts(session, _QUERY_DEGREE_RELATED):
        entity_id = str(row["id"])
        degrees[entity_id] = degrees.get(entity_id, 0) + int(row["c"])
    return degrees


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


def _build_groups(entities: dict[str, Entity], degrees: dict[str, int]) -> list[CandidateGroup]:
    buckets: dict[tuple[str, EntityType], list[str]] = {}
    for entity in entities.values():
        buckets.setdefault((normalize_key(entity.name), entity.type), []).append(entity.id)

    groups: list[CandidateGroup] = []
    for (key, entity_type), member_ids in buckets.items():
        namespaces = frozenset(namespace_from_id(entity_id) for entity_id in member_ids)
        if len(namespaces) < 2:
            continue
        ordered = tuple(sorted(member_ids))
        groups.append(
            CandidateGroup(
                group_key=key,
                entity_type=entity_type,
                member_ids=ordered,
                namespaces=namespaces,
                single_word=len(key.split()) == 1,
                degree=sum(degrees.get(entity_id, 0) for entity_id in ordered),
            )
        )
    groups.sort(key=lambda group: (group.entity_type, group.group_key))
    return groups


def _apply_filters(
    groups: list[CandidateGroup],
    *,
    type_filter: str | None,
    generic_only: bool,
    namespace_filter: str | None,
) -> list[CandidateGroup]:
    selected: list[CandidateGroup] = []
    for group in groups:
        if type_filter is not None and group.entity_type != type_filter:
            continue
        if generic_only and not group.single_word:
            continue
        if namespace_filter is not None and namespace_filter not in group.namespaces:
            continue
        selected.append(group)
    return selected


def _mix_round_robin(pool: list[CandidateGroup], *, seed: int, quota: int) -> list[CandidateGroup]:
    """Mix sembrado round-robin sobre los types presentes (estrata 3)."""
    if quota <= 0 or not pool:
        return []
    rng = random.Random(seed)
    by_type: dict[EntityType, list[CandidateGroup]] = {}
    for group in sorted(pool, key=lambda item: (item.group_key, item.entity_type)):
        by_type.setdefault(group.entity_type, []).append(group)
    for bucket in by_type.values():
        rng.shuffle(bucket)

    mixed: list[CandidateGroup] = []
    type_order = sorted(by_type)
    round_index = 0
    while len(mixed) < quota:
        progressed = False
        for entity_type in type_order:
            bucket = by_type[entity_type]
            if round_index < len(bucket) and len(mixed) < quota:
                mixed.append(bucket[round_index])
                progressed = True
        if not progressed:
            break
        round_index += 1
    return mixed


def _select_groups(
    filtered: list[CandidateGroup], *, seed: int, limit: int, render_all: bool
) -> list[CandidateGroup]:
    if render_all:
        for group in filtered:
            group.stratum = "all"
        return sorted(
            filtered, key=lambda group: (not group.single_word, -group.degree, group.group_key)
        )

    # Estrata 1: TODAS las etiquetas genericas de una palabra (no se limita).
    generic = sorted(
        (group for group in filtered if group.single_word),
        key=lambda group: (-group.degree, group.group_key, group.entity_type),
    )
    rest = [group for group in filtered if not group.single_word]

    # Estratas 2 y 3 comparten el ``--limit`` restante a partes iguales.
    budget = max(limit - len(generic), 0)
    quota_degree = (budget + 1) // 2
    quota_mix = budget - quota_degree

    by_degree = sorted(rest, key=lambda group: (-group.degree, group.group_key, group.entity_type))[
        :quota_degree
    ]
    selected_keys = {(group.group_key, group.entity_type) for group in by_degree}
    remaining = [
        group for group in rest if (group.group_key, group.entity_type) not in selected_keys
    ]
    mixed = _mix_round_robin(remaining, seed=seed, quota=quota_mix)

    for group in generic:
        group.stratum = "generic"
    for group in by_degree:
        group.stratum = "degree"
    for group in mixed:
        group.stratum = "mix"

    chosen = [*generic, *by_degree, *mixed]
    return sorted(chosen, key=lambda group: (not group.single_word, -group.degree, group.group_key))


def _pair_payload(
    anchor: Entity,
    candidate: Entity,
    mentions: dict[str, set[str]],
    neighbors: dict[str, set[str]],
    thresholds: BandThresholds,
) -> dict[str, Any]:
    evidence = s0_match(anchor, candidate)
    gate = s2_type_gate(anchor.type, candidate.type)

    mentions_j, mentions_shared, mentions_union = mentions_jaccard(
        mentions.get(anchor.id, set()), mentions.get(candidate.id, set())
    )
    related_j, related_shared, related_union = related_jaccard(
        neighbors.get(anchor.id, set()), neighbors.get(candidate.id, set())
    )
    desc_o = description_overlap(anchor.description, candidate.description)

    # El builder exige un cosine: 0.0 documenta que NO se computo (sin modelo);
    # por eso conflict_flag queda False y no es informative en este sheet.
    signals: S3ContextSignals = s3_context_score(
        mentions_j,
        related_j,
        desc_o,
        0.0,
        mentions_source_count=mentions_union,
        mentions_shared_count=mentions_shared,
        related_neighbor_count=related_union,
        related_shared_count=related_shared,
    )
    composite = composite_score(signals)
    shared = sorted(neighbors.get(anchor.id, set()) & neighbors.get(candidate.id, set()))

    return {
        "anchor_id": anchor.id,
        "candidate_id": candidate.id,
        "anchor_namespace": evidence.anchor_namespace,
        "candidate_namespace": evidence.candidate_namespace,
        "cross_namespace": evidence.cross_namespace,
        "cross_type": evidence.cross_type,
        "s0_matched_field": evidence.s0_matched_field,
        "s2_type_gate": {"passed": gate.passed, "reason": gate.reason},
        "mentions_jaccard": mentions_j,
        "mentions_shared": mentions_shared,
        "mentions_union": mentions_union,
        "related_jaccard": related_j,
        "related_shared": related_shared,
        "related_union": related_union,
        "description_overlap": desc_o,
        "composite": composite,
        "thresholds": {
            "high_context": thresholds.high_context,
            "conflict_floor": thresholds.conflict_floor,
        },
        "reading": reading_for(desc_o, thresholds).value,
        "primary_signal": PRIMARY_SIGNAL,
        "structural_signals": list(STRUCTURAL_SIGNALS),
        "cosine_computed": False,
        "shared_neighbors": [
            {"id": neighbor_id, "namespace": namespace_from_id(neighbor_id)}
            for neighbor_id in shared
        ],
    }


def _member_payload(
    entity: Entity, mentions: dict[str, set[str]], neighbors: dict[str, set[str]]
) -> dict[str, Any]:
    mention_sources = mentions.get(entity.id, set())
    return {
        "id": entity.id,
        "namespace": namespace_from_id(entity.id),
        "type": entity.type,
        "name": entity.name,
        "description": entity.description,
        "source_page": entity.source_page,
        "alias_count": len(entity.aliases),
        "mentions_count": len(mention_sources),
        "related_count": len(neighbors.get(entity.id, set())),
        "mention_source_ids": sorted(mention_sources),
    }


def _cross_namespace_pairs(
    group: CandidateGroup,
    entities: dict[str, Entity],
    *,
    namespace_filter: str | None,
) -> list[tuple[Entity, Entity]]:
    """Pares cross-namespace del grupo; anchor por ``choose_canonical_id``.

    Ancla por par = id mas corto (empates lexicograficos), que coincide con el
    ancla del grupo cuando el par lo incluye; ``--namespace`` deja solo los
    pares que involucran ese namespace.
    """
    members = [entities[entity_id] for entity_id in group.member_ids]
    pairs: list[tuple[Entity, Entity]] = []
    for index, first in enumerate(members):
        for second in members[index + 1 :]:
            first_ns = namespace_from_id(first.id)
            second_ns = namespace_from_id(second.id)
            if first_ns == second_ns:
                continue
            if namespace_filter is not None and namespace_filter not in (first_ns, second_ns):
                continue
            anchor_id = choose_canonical_id([first.id, second.id])
            anchor, candidate = (first, second) if anchor_id == first.id else (second, first)
            pairs.append((anchor, candidate))
    return pairs


def _group_payload(
    group: CandidateGroup,
    entities: dict[str, Entity],
    mentions: dict[str, set[str]],
    neighbors: dict[str, set[str]],
    thresholds: BandThresholds,
    *,
    namespace_filter: str | None,
) -> dict[str, Any]:
    member_ids = list(group.member_ids)
    members = [
        _member_payload(entities[entity_id], mentions, neighbors) for entity_id in member_ids
    ]
    pairs = [
        _pair_payload(anchor, candidate, mentions, neighbors, thresholds)
        for anchor, candidate in _cross_namespace_pairs(
            group, entities, namespace_filter=namespace_filter
        )
    ]
    # Riesgo combinado (T6c): regla compartida, nunca reimplementada aqui.
    risk = label_risk(
        single_word=group.single_word,
        namespace_count=len(group.namespaces),
    )
    return {
        "group_key": group.group_key,
        "group_label": f"{group.group_key}|{group.entity_type}",
        "entity_type": group.entity_type,
        "single_word": group.single_word,
        "stratum": group.stratum,
        "namespaces": sorted(group.namespaces),
        "anchor_id": choose_canonical_id(list(group.member_ids)),
        "degree": group.degree,
        "risk": risk.model_dump(mode="json"),
        "risk_marker": format_risk_marker(risk),
        "members": members,
        "pairs": pairs,
    }


def _print_header(
    args: argparse.Namespace, thresholds: BandThresholds, found: int, filtered: int
) -> None:
    mode = (
        "MUESTRA COMPLETA (--all)"
        if args.render_all
        else (f"MUESTRA sembrada (seed={args.seed}, limit={args.limit})")
    )
    print("== RENDER CROSS-NAMESPACE - HOJA DE DECISION (READ-ONLY) ==")
    print(f"  modo: {mode}")
    print(
        f"  filtros: type={args.type_filter or '(todos)'} "
        f"generic_only={args.generic_only} namespace={args.namespace or '(todos)'}"
    )
    print(f"  grupos candidatos: {found} | tras filtros: {filtered}")
    print(
        f"  lectura compartida (reading_for sobre {PRIMARY_SIGNAL}): "
        f"identity >= {thresholds.high_context:.2f} (high_context); "
        f"undecided >= {thresholds.conflict_floor:.2f} (conflict_floor); "
        "no_shared_context por debajo — la decide el modelo, no este guion"
    )
    print(
        f"  senales: {PRIMARY_SIGNAL} = primaria; "
        f"{', '.join(STRUCTURAL_SIGNALS)} = estructurales (~0 pre-merge)"
    )
    print("  cosine/embeddings: NO computado (sin llamadas de modelo) -> no se reclama banda S4")
    print("  enrutamiento cross-namespace: SIEMPRE cuarentena (R6.2);")
    print("  las lecturas son fuerza de evidencia para el juicio humano, no decisiones")
    print("  consultas: solo MATCH (garantia de no mutacion)")


def _print_group(payload: dict[str, Any], index: int, total: int) -> None:
    generic_tag = "generica=SI" if payload["single_word"] else "generica=no"
    print(
        f"\n  GRUPO {index}/{total}: label={payload['group_label']!r} "
        f"ns={len(payload['namespaces'])} miembros={len(payload['members'])} "
        f"grado={payload['degree']} {generic_tag} estrata={payload['stratum']}"
    )
    print(f"    anchor del grupo: {payload['anchor_id']}")
    print(f"    riesgo: {payload['risk_marker'] or '(sin riesgo)'}")
    pairs = payload["pairs"]
    for pair_index, pair in enumerate(pairs, start=1):
        print(
            f"    par {pair_index}/{len(pairs)}: {pair['anchor_id']}  <->  {pair['candidate_id']}"
        )
        print(
            f"      S0: matched_field={pair['s0_matched_field']} "
            f"cross_namespace={pair['cross_namespace']} cross_type={pair['cross_type']} "
            f"({pair['anchor_namespace']} -> {pair['candidate_namespace']})"
        )
        gate = pair["s2_type_gate"]
        print(f"      S2: passed={gate['passed']} - {gate['reason']}")
        print(
            f"      {PRIMARY_SIGNAL}={pair['description_overlap']:.3f} PRIMARIA | "
            f"mentions_jaccard={pair['mentions_jaccard']:.3f} "
            f"({pair['mentions_shared']}/{pair['mentions_union']}) estructural | "
            f"related_jaccard={pair['related_jaccard']:.3f} "
            f"({pair['related_shared']}/{pair['related_union']}) estructural"
        )
        print(
            f"      composite(informativo)={pair['composite']:.3f}  "
            f"[high_context={pair['thresholds']['high_context']:.2f} | "
            f"conflict_floor={pair['thresholds']['conflict_floor']:.2f}]"
        )
        print(
            f"      LECTURA (evidencia, NO enrutamiento): {pair['reading']}  | "
            "enrutamiento cross-ns: siempre cuarentena (R6.2) | cosine: NO computado"
        )
        shared = pair["shared_neighbors"]
        shown = ", ".join(
            f"{item['namespace']}/{item['id'].rsplit(':', 1)[-1]}"
            for item in shared[:SHARED_NEIGHBOR_RENDER_LIMIT]
        )
        suffix = " ..." if len(shared) > SHARED_NEIGHBOR_RENDER_LIMIT else ""
        print(f"      vecinos compartidos ({len(shared)}): {shown or '(ninguno)'}{suffix}")

    for member in payload["members"]:
        description = member["description"] or "(sin descripcion)"
        if len(description) > DESC_RENDER_LIMIT:
            description = description[: DESC_RENDER_LIMIT - 3] + "..."
        print(f"    miembro: {member['id']}")
        print(
            f"      ns={member['namespace']} type={member['type']} name={member['name']!r} "
            f"source_page={member['source_page']} aliases={member['alias_count']}"
        )
        print(f"      desc: {description}")
        print(
            f"      MENTIONS={member['mentions_count']} books={member['mention_source_ids']} "
            f"RELATED={member['related_count']}"
        )


def _write_payload(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False, default=str)
        handle.write("\n")


async def main() -> None:
    args = _parse_args()
    settings = Settings.model_validate({})
    thresholds = BandThresholds()

    driver = AsyncGraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value()),
    )
    try:
        async with driver.session() as session:
            entities, skipped = await _load_entities(session)
            degrees = await _load_degrees(session)
            groups = _build_groups(entities, degrees)
            filtered = _apply_filters(
                groups,
                type_filter=args.type_filter,
                generic_only=args.generic_only,
                namespace_filter=args.namespace,
            )
            selected = _select_groups(
                filtered, seed=args.seed, limit=args.limit, render_all=args.render_all
            )
            member_ids = sorted({entity_id for group in selected for entity_id in group.member_ids})
            mentions = await _load_mentions(session, member_ids)
            neighbors = await _load_neighbors(session, member_ids)
            payloads = [
                _group_payload(
                    group,
                    entities,
                    mentions,
                    neighbors,
                    thresholds,
                    namespace_filter=args.namespace,
                )
                for group in selected
            ]
    finally:
        await driver.close()

    single_word_found = sum(1 for group in groups if group.single_word)
    single_word_rendered = sum(1 for payload in payloads if payload["single_word"])
    readings: Counter[str] = Counter(
        pair["reading"] for payload in payloads for pair in payload["pairs"]
    )
    pair_count = sum(len(payload["pairs"]) for payload in payloads)

    _print_header(args, thresholds, found=len(groups), filtered=len(filtered))
    if skipped:
        print(f"  AVISO: {skipped} entidades ignoradas (type fuera del contrato EntityType)")
    for index, payload in enumerate(payloads, start=1):
        _print_group(payload, index, len(payloads))

    write_payload = {
        "schema": RENDER_SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(),
        "read_only": True,
        "query_classes": ["MATCH"],
        "cosine_computed": False,
        "s4_band_claimed": False,
        "routing_policy": "cross-namespace pairs are always quarantine (R6.2)",
        "seed": args.seed,
        "limit": args.limit,
        "render_all": args.render_all,
        "type_filter": args.type_filter,
        "generic_only": args.generic_only,
        "namespace_filter": args.namespace,
        "thresholds": {
            "high_context": thresholds.high_context,
            "conflict_floor": thresholds.conflict_floor,
        },
        "groups_found": len(groups),
        "groups_after_filters": len(filtered),
        "single_word_groups_found": single_word_found,
        "skipped_entities": skipped,
        "groups": payloads,
        "summary": {
            "groups_rendered": len(payloads),
            "pairs_evaluated": pair_count,
            "single_word_groups_rendered": single_word_rendered,
            "readings": {name: readings.get(name, 0) for name in READING_ORDER},
        },
    }
    _write_payload(args.out, write_payload)

    print("\n== RESUMEN ==")
    print(f"  grupos candidatos encontrados: {len(groups)}")
    print(f"  tras filtros: {len(filtered)}")
    print(f"  grupos renderizados: {len(payloads)}")
    print(
        f"  etiquetas de una palabra: {single_word_found} encontradas / "
        f"{single_word_rendered} renderizadas"
    )
    print(f"  pares evaluados: {pair_count}")
    for name in READING_ORDER:
        print(f"  lectura {name}: {readings.get(name, 0)}")
    print(f"  JSON: {args.out}")
    print("  grafo sin mutar: solo se ejecutaron consultas MATCH")


if __name__ == "__main__":
    asyncio.run(main())
