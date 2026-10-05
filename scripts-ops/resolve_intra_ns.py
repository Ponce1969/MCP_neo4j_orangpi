"""Resolucion SCOPED de duplicados logicos intra-namespace (merge + ledger).

Contexto: la extraccion LLM de un libro produce el mismo concepto con varios ids
(sigla vs expansion, ingles vs espanol, y variantes SOLO de mayusculas desde
T9b). El audit los reporta como warning ``DUPLICATE_ENTITY_LOGICAL``; este script
los fusiona dentro de UN namespace con el use case real de produccion (soft-delete
``merged_into`` + re-point de MENTIONS/RELATED + fold de aliases + entrada
encadenada en el ledger), nunca con Cypher de escritura a mano.

Contrato T10 (cuatro piezas):

1. **Paridad de agrupacion (drift-proof)**: los grupos NO se hardcodean ni se
   re-definen: la query se construye desde ``DUPLICATE_GROUP_KEY_EXPRESSION``,
   la constante que exporta ``infrastructure/neo4j_audit_adapter.py`` y que la
   propia regla ``duplicates_entity`` usa (mismo patron anti-drift que
   ``CROSS_NAMESPACE_DECISION_EXCLUSION``). Sin eso, el script quedaria en la
   agrupacion exacta pos-T9b, veria CERO grupos y reportaria "nada que hacer"
   en silencio.
2. **Canonical por riqueza**: con las puntuaciones medidas (menciones,
   grado RELATED) el canonical es el miembro mas rico
   (``choose_canonical_id_by_richness``); el id que SOBREVIVE es el que
   referencian despues los consumidores (MCP, bundles de evidencia, registro de
   decisiones), asi que el slug mas corto es un proxy pobre. Sin scores sigue
   valiendo la regla historica (id mas corto).
3. **Fingerprint**: sha256 determinista sobre el plan canonico ordenado
   (namespace, name, kind, canonical_id, duplicate_ids), impreso en la salida
   humana y en ``--json``. ``--apply`` exige ``--expect-fingerprint`` y aborta
   (se RECHAZA, no solo advierte) si el plan recomputado difiere, ANTES de la
   primera escritura — mismo contrato que ``ledger rollback``.
4. **Censo antes/despues**: entidades activas, ``merged_into``, MENTIONS y
   RELATED, por namespace y db-wide. La delta de entidades es UNA entidad activa
   menos y UN ``merged_into`` mas por duplicado fusionado. Las aristas NO son
   invariantes: el apply colapsa los ``MENTIONS`` cuyo ``(chunk, entidad del
   grupo)`` ya menciona al canonical (``MERGE`` + ``DELETE`` en
   ``_REPOINT_MENTIONS_BATCH``), ``_DELETE_INTRA_GROUP_RELATED`` borra cada arista
   ``RELATED`` dirigida cuyos dos extremos caen en el grupo fusionado, y
   ``_REPOINT_RELATED_{OUT,IN}_BATCH`` colapsa aristas re-apuntadas cuando el
   canonical YA tenia una arista con la misma clave
   ``(direccion, otro extremo, type)`` (``MERGE`` sobre la existente + ``DELETE``
   de la del miembro). El plan MIDE las tres poblaciones (read-only, sobre los
   grupos del plan, contando en Python puro y testeable) y las resta de la delta:
   ``MENTIONS − colapsos`` y ``RELATED − (intra-grupo + re-point)``. Tras
   ``--apply`` se mide y cualquier drift imprime lineas DRIFT y sale con codigo
   distinto de cero.

Fases:
  Fase 1 (default, READ-ONLY): reporta el plan completo, fingerprint, censo y
  el impacto de aristas.
  Fase 2 (--apply): aplica los merges. MUTA EL GRAFO y exige:
    - ``--backup <json>``  backup fresco ya hecho (scripts-ops/backup_only.py).
      Se rechaza si tiene mas de 24 h salvo ``--allow-stale-backup``.
    - ``--approval <file>`` cuyo contenido incluya la palabra "approve"
      (AGENTS.md 7.2, mismo contrato que el cleanup scoped).
    - ``--expect-fingerprint <hex>`` con el hash del dry-run revisado
      (obligatorio; sin el o con uno distinto se aborta antes de escribir).

Efectos sobre las aristas que el script MIDE y reporta (no asume):

- ``_DELETE_INTRA_GROUP_RELATED`` (corrido por ``apply_merge`` en la misma
  transaccion) borra toda arista ``RELATED`` dirigida cuyos dos extremos
  pertenecen al mismo grupo fusionado — no queda self-loop ni arista al
  soft-deleted, y no hace falta un paso aparte de aprobacion.
- ``_REPOINT_MENTIONS_BATCH`` hace ``MERGE (chunk)-[:MENTIONS]->(canonical)``
  y luego ``DELETE`` la arista del duplicado: cuando el chunk YA mencionaba al
  canonical — o a otro miembro del grupo — el total colapsa en UNA arista por
  ``(chunk, grupo)``.
- ``_REPOINT_RELATED_{OUT,IN}_BATCH`` hacen
  ``MERGE (canon)-[r2:RELATED {type: r.type}]->(other)`` (y la simetrica
  ``(other)-[r2]->(canon)``) por cada arista capturada del miembro y despues
  ``DELETE`` la original: por cada clave ``(direccion, otro extremo, type)``
  sobrevive 1 arista cuando ``existing + incoming >= 1``, asi que la perdida es
  ``max(0, existing + incoming - 1)`` — ``existing`` es la arista pre-merge del
  canonical (el adapter solo captura ``duplicate_ids``) y ``incoming`` las aristas
  de miembros que mapean a esa misma clave. Las aristas cuyo otro extremo cae en
  el grupo las salta el ``WHERE`` del adapter y las borra
  ``_DELETE_INTRA_GROUP_RELATED``: se cuentan una sola vez, en el contador
  intra-grupo.

El script cuenta las tres poblaciones (read-only, sobre los grupos del plan que
el apply procesara, con el conteo en Python puro) y las imprime en el dry-run y
en ``--json`` como ``MENTIONS a colapsar: N``, ``RELATED intra-grupo a borrar:
M`` y ``RELATED re-point a colapsar: K``. Re-ejecutar es idempotente: la regla
del audit ignora las entidades ya fusionadas.

Uso (desde la raiz del repo en el OrangePi, con el .env sourceado):
    uv run --no-sync python scripts-ops/resolve_intra_ns.py
    uv run --no-sync python scripts-ops/resolve_intra_ns.py --json
    uv run --no-sync python scripts-ops/resolve_intra_ns.py --apply --limit 3 \\
        --backup ~/backups_neo4j/bookgraph_backup_YYYYMMDDTHHMMSSZ.json \\
        --approval /tmp/approve_huyen_resolve.txt \\
        --expect-fingerprint <hex del dry-run>
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from neo4j import AsyncGraphDatabase, AsyncSession

from book_graph_rag.application.resolve_entities_use_case import MergeGroup
from book_graph_rag.config import Settings
from book_graph_rag.domain.duplicate_grouping import (
    DuplicateMemberRow,
    PlannedMerge,
    plan_intra_resolution,
    to_entity_type,
)
from book_graph_rag.domain.resolution_models import (
    ConfidenceBand,
    ResolutionEvidence,
    S0NormalizedForm,
)
from book_graph_rag.infrastructure.neo4j_audit_adapter import DUPLICATE_GROUP_KEY_EXPRESSION
from book_graph_rag.infrastructure.resolution_wiring import build_apply_merge_use_case

DEFAULT_NAMESPACE = "knowledge:ai-engineering-huyen"
DEFAULT_APPROVER = "human:gonzalo"
STALE_BACKUP_HOURS = 24
APPROVAL_WORD = "approve"

# Espejo exacto de la regla DUPLICATE_ENTITY_LOGICAL del audit (query
# "duplicates_entity") con el scope inyectado tras el primer WHERE: la clave de
# agrupacion viene de la constante compartida (T10-A), NUNCA de una copia
# literal — tests/unit/test_resolve_intra_ns.py la falla si alguien la copia.
_GROUPS_QUERY = f"""
MATCH (n:Entity)
WHERE (n.id STARTS WITH $prefix)
  AND (n.merged_into IS NULL OR n.merged_into = '')
  AND n.id IS NOT NULL
WITH n, split(n.id, ':') AS parts WHERE size(parts) >= 2
WITH n, parts ORDER BY coalesce(n.id, '')
WITH n, parts[0] + ':' + parts[1] AS namespace,
     {DUPLICATE_GROUP_KEY_EXPRESSION} AS name, n.type AS kind
WITH namespace, name, kind, collect(n) AS members WHERE size(members) > 1
WITH namespace, name, kind, members
ORDER BY namespace, coalesce(name, ''), coalesce(kind, '')
RETURN name, kind, [m IN members | coalesce(m.id, '')] AS ids
ORDER BY name, kind
"""

# Impacto de aristas por entidad: doble uso — (a) puntuaciones de riqueza
# (menciones, grado RELATED) para elegir el canonical y (b) el impacto de
# re-point que se imprime con el plan.
_EDGE_IMPACT_QUERY = """
UNWIND $ids AS did
MATCH (d:Entity {id: did})
RETURN did AS id,
       size([(c:Chunk)-[:MENTIONS]->(d) | c]) AS mentions_in,
       size([(d)-[r:RELATED]-() | r]) AS related_edges
"""

# Aristas RELATED con ambos extremos entre los miembros del plan: fila cruda
# (src, dst) SIN agrupacion y SIN orden de id. El filtro "mismo grupo" y el
# conteo por arista dirigida viven en Python (``count_intra_group_related``),
# donde son testeables: ``_DELETE_INTRA_GROUP_RELATED`` borra TODA arista con
# ambos extremos en el grupo, un par bidireccional son 2 aristas y una arista
# unica cuyo origen ordena mas alto que su destino tambien cuenta. La
# paridad de agrupacion la hereda del plan (``_GROUPS_QUERY`` + la constante
# compartida), no de una segunda copia de la clave aqui.
_INTRA_GROUP_RELATED_QUERY = """
MATCH (a:Entity)-[r:RELATED]->(b:Entity)
WHERE a.id IN $member_ids AND b.id IN $member_ids
RETURN a.id AS src, b.id AS dst
"""

# Mentions de los miembros del plan: (chunk, entidad), INCLUYENDO al canonical.
# El conteo de colapsos vive en Python (``count_mentions_collapses``):
# ``_REPOINT_MENTIONS_BATCH`` hace ``MERGE (chunk)-[:MENTIONS]->(canonical)``
# por cada arista capturada, asi que por cada ``(chunk, grupo)`` que menciona a
# cualquier miembro sobrevive UNA arista y la perdida es ``mencionados - 1``.
# Read-only; ``elementId`` identifica al chunk dentro de UNA lectura.
_MEMBER_MENTIONS_QUERY = """
MATCH (c:Chunk)-[:MENTIONS]->(e:Entity)
WHERE e.id IN $member_ids
RETURN elementId(c) AS chunk_ref, e.id AS entity_id
"""

# Aristas RELATED incidentes a los miembros del plan: (src, dst, type) SIN
# agrupacion y SIN orden de id, con OR en los dos extremos para ver TAMBIEN las
# aristas propias del canonical (la mitad ``existing`` de la clave de colapso del
# re-point: el adapter solo captura ``duplicate_ids``). El filtro "mismo grupo" y
# el conteo de colapsos viven en Python (``count_related_repoint_collapses``),
# donde son testeables: ``_REPOINT_RELATED_{OUT,IN}_BATCH`` MERGEa cada arista
# capturada del miembro sobre la clave ``(direccion, otro extremo, type)`` y
# despues borra la original, asi que la perdida por clave es
# ``max(0, existing + incoming - 1)`` y las aristas con ambos extremos en el
# grupo las posee ``_DELETE_INTRA_GROUP_RELATED`` (contador intra-grupo).
# Read-only.
_REPOINT_RELATED_ROWS_QUERY = """
MATCH (a:Entity)-[r:RELATED]->(b:Entity)
WHERE a.id IN $member_ids OR b.id IN $member_ids
RETURN a.id AS src, b.id AS dst, r.type AS type
"""

# Censo por namespace (o db-wide con $prefix = '') en UNA lectura: entidades
# activas, merged_into, aristas MENTIONS (targeto en el scope) y RELATED (al
# menos un extremo en el scope). Es read-only.
_CENSUS_QUERY = """
MATCH (e:Entity)
WHERE $prefix = '' OR e.id STARTS WITH $prefix
WITH count(CASE WHEN e.merged_into IS NULL OR e.merged_into = '' THEN 1 END) AS active_entities,
     count(CASE WHEN e.merged_into IS NOT NULL AND e.merged_into <> '' THEN 1 END)
         AS merged_into_count
OPTIONAL MATCH (c:Chunk)-[:MENTIONS]->(t:Entity)
WHERE $prefix = '' OR t.id STARTS WITH $prefix
WITH active_entities, merged_into_count, count(c) AS total_mentions
OPTIONAL MATCH (a:Entity)-[:RELATED]->(b:Entity)
WHERE $prefix = '' OR a.id STARTS WITH $prefix OR b.id STARTS WITH $prefix
RETURN active_entities, merged_into_count, total_mentions, count(a) AS total_related
"""


@dataclass(frozen=True)
class Census:
    """Censo de entidades/aristas: activas, ``merged_into``, MENTIONS, RELATED."""

    active_entities: int
    merged_into_count: int
    total_mentions: int
    total_related: int


def predicted_census_delta(
    plan: Sequence[PlannedMerge],
    collapses: int = 0,
    intra_group_edges: int = 0,
    repoint_collapses: int = 0,
) -> Census:
    """Delta predicha = lo que el apply REALMENTE hara, no una asuncion.

    Entidades: UNA activa menos y UN ``merged_into`` mas por duplicado fusionado.
    Con los grupos de dos miembros (la poblacion case-only) esto se lee como
    "una entidad menos y un ``merged_into`` mas por grupo"; un grupo de N
    miembros aporta N-1 duplicados.

    Aristas: MENTIONS y RELATED NO son invariantes.

    - ``total_mentions = -collapses``: por cada ``(chunk, grupo)`` que menciona a
      mas de un miembro del grupo solo sobrevive una arista despues del
      ``MERGE`` + ``DELETE`` de ``_REPOINT_MENTIONS_BATCH``.
    - ``total_related = -(intra_group_edges + repoint_collapses)``:
      ``_DELETE_INTRA_GROUP_RELATED`` borra cada arista dirigida con sus dos
      extremos en el grupo fusionado, y el re-point MERGEa las aristas de los
      miembros sobre la clave ``(direccion, otro extremo, type)`` del canonical,
      colapsando ``max(0, existing + incoming - 1)`` aristas por clave.

    Los conteos los miden los fetchers read-only del script sobre los grupos del
    plan (0 por defecto = sin deletes medidos, p. ej. en pruebas).
    """
    duplicates = sum(len(merge.duplicate_ids) for merge in plan)
    return Census(
        active_entities=-duplicates,
        merged_into_count=duplicates,
        total_mentions=-collapses,
        total_related=-(intra_group_edges + repoint_collapses),
    )


def _group_index(plan: Sequence[PlannedMerge]) -> dict[str, int]:
    """Mapa ``id de entidad -> indice de su grupo`` en el plan (grupos disjuntos)."""
    index: dict[str, int] = {}
    for position, merge in enumerate(plan):
        for entity_id in (merge.canonical_id, *merge.duplicate_ids):
            index[entity_id] = position
    return index


def _members_by_group(plan: Sequence[PlannedMerge]) -> dict[int, frozenset[str]]:
    """Mapa ``indice de grupo -> ids de TODOS sus miembros`` (canonical incluido)."""
    return {
        position: frozenset((merge.canonical_id, *merge.duplicate_ids))
        for position, merge in enumerate(plan)
    }


def count_intra_group_related(
    plan: Sequence[PlannedMerge], edges: Sequence[tuple[str, str]]
) -> int:
    """Aristas RELATED dirigidas que ``_DELETE_INTRA_GROUP_RELATED`` borrara.

    Cada fila es una arista dirigida ``(src, dst)`` ya filtrada a miembros del
    plan; se cuenta TODA arista cuyos dos extremos caen en el MISMO grupo, sin
    filtro de orden de id: un par bidireccional intra-grupo cuenta 2 y una arista
    unica cuyo origen ordena mas alto que su destino cuenta 1.
    """
    group_of = _group_index(plan)
    counted = 0
    for src, dst in edges:
        group = group_of.get(src)
        if group is not None and group_of.get(dst) == group:
            counted += 1
    return counted


def count_mentions_collapses(
    plan: Sequence[PlannedMerge], mentions: Sequence[tuple[str, str]]
) -> int:
    """MENTIONS que ``_REPOINT_MENTIONS_BATCH`` colapsara (``MERGE`` + ``DELETE``).

    Cada fila es ``(chunk_ref, entity_id)`` — miembros o canonical. El batch hace
    ``MERGE (chunk)-[:MENTIONS]->(canonical)`` por cada arista capturada y luego
    ``DELETE`` la original, asi que por cada ``(chunk, grupo)`` que menciona a
    cualquier miembro del grupo sobrevive UNA sola arista y la perdida es
    ``mencionados - 1``:

    - chunk solo con el canonical (1) o solo con UN duplicado (1): 0 de perdida
      (en el segundo caso el MERGE crea la arista nueva: neto 0);
    - chunk con canonical + duplicado, o con dos duplicados sin canonical, o con
      los N miembros: ``N - 1``.
    """
    group_of = _group_index(plan)
    per_group: dict[int, dict[str, set[str]]] = {}
    for chunk_ref, entity_id in mentions:
        group = group_of.get(entity_id)
        if group is None:
            continue
        per_group.setdefault(group, {}).setdefault(chunk_ref, set()).add(entity_id)
    return sum(
        len(member_ids) - 1 for chunks in per_group.values() for member_ids in chunks.values()
    )


def count_related_repoint_collapses(
    plan: Sequence[PlannedMerge], edges: Sequence[tuple[str, str, str | None]]
) -> int:
    """Aristas RELATED que ``_REPOINT_RELATED_{OUT,IN}_BATCH`` colapsaran.

    Cada fila es ``(src, dst, type)`` de una arista incidente a un miembro del
    plan. El batch MERGEa cada arista capturada del miembro sobre
    ``(canon)-[RELATED {type}]->(other)`` (o la simetrica) y despues borra la
    original, asi que por cada clave ``(grupo, direccion, otro, type)``:

    - ``existing`` = 1 si el canonical ya tenia esa arista pre-merge (el adapter
      solo captura ``duplicate_ids``: las aristas propias del canonical son la
      mitad existente),
    - ``incoming`` = aristas de miembros del grupo que mapean a esa misma clave,
    - sobreviven ``min(1, existing + incoming)`` → perdida
      ``max(0, existing + incoming - 1)``.

    Exclusiones (paridad con el adapter, sin doble conteo):

    - aristas con ``type IS NULL``: ``WHERE r.type = inv.edge_properties.type``
      nunca matchea NULL, asi que el adapter NO las re-apunta y no colapsan;
    - aristas con ambos extremos en el mismo grupo: las salta el ``WHERE`` del
      batch (``NOT other.id IN $dup_ids AND other.id <> $canonical_id``) y las
      borra ``_DELETE_INTRA_GROUP_RELATED`` — las cuenta
      ``count_intra_group_related``.
    """
    group_of = _group_index(plan)
    canonical_by_group = {i: merge.canonical_id for i, merge in enumerate(plan)}
    members_of = _members_by_group(plan)
    existing: set[tuple[int, str, str, str]] = set()
    incoming: dict[tuple[int, str, str, str], int] = {}
    for src, dst, edge_type in edges:
        if edge_type is None:
            continue
        src_group = group_of.get(src)
        dst_group = group_of.get(dst)
        # direccion 'out': src es el miembro, dst es el otro extremo de la clave.
        if src_group is not None and dst not in members_of[src_group]:
            key = (src_group, "out", dst, edge_type)
            if src == canonical_by_group[src_group]:
                existing.add(key)
            else:
                incoming[key] = incoming.get(key, 0) + 1
        # direccion 'in': dst es el miembro, src es el otro extremo de la clave.
        if dst_group is not None and src not in members_of[dst_group]:
            key = (dst_group, "in", src, edge_type)
            if dst == canonical_by_group[dst_group]:
                existing.add(key)
            else:
                incoming[key] = incoming.get(key, 0) + 1
    return sum(max(0, (1 if key in existing else 0) + count - 1) for key, count in incoming.items())


def census_drift(before: Census, after: Census, predicted: Census) -> list[str]:
    """Lineas de drift; vacio = la medicion post-apply cumple la prediccion.

    Espejo del ``compare_census`` de ``ledger rollback``: cualquier campo que
    no iguale ``before + predicted`` produce una linea y hace fallar el comando.
    """
    drift: list[str] = []
    expected_active = before.active_entities + predicted.active_entities
    if after.active_entities != expected_active:
        drift.append(
            f"entidades activas {before.active_entities} -> {after.active_entities} "
            f"!= esperado {expected_active}"
        )
    expected_merged = before.merged_into_count + predicted.merged_into_count
    if after.merged_into_count != expected_merged:
        drift.append(
            f"merged_into {before.merged_into_count} -> {after.merged_into_count} "
            f"!= esperado {expected_merged}"
        )
    expected_mentions = before.total_mentions + predicted.total_mentions
    if after.total_mentions != expected_mentions:
        drift.append(
            f"MENTIONS {before.total_mentions} -> {after.total_mentions} "
            f"!= esperado {expected_mentions} (colapsos + re-point predichos)"
        )
    expected_related = before.total_related + predicted.total_related
    if after.total_related != expected_related:
        drift.append(
            f"RELATED {before.total_related} -> {after.total_related} "
            f"!= esperado {expected_related} (borrados intra-grupo + re-point predichos)"
        )
    return drift


def plan_fingerprint(plan: Sequence[PlannedMerge]) -> str:
    """sha256 determinista del plan canonico ordenado (T10-C).

    Cubre por grupo ``(namespace, name, kind, canonical_id, duplicate_ids)`` en
    el orden del plan: el mismo plan produce siempre el mismo hash y cualquier
    campo distinto lo cambia, de modo que ``--expect-fingerprint`` ata la
    revision del dry-run a la mutacion (mismo contrato que ``ledger rollback``).
    El namespace se deriva del id del canonical, la clave que el merge escribiria.
    """
    payload = [
        {
            "namespace": _namespace_of(merge.canonical_id),
            "name": merge.name,
            "kind": merge.kind,
            "canonical_id": merge.canonical_id,
            "duplicate_ids": list(merge.duplicate_ids),
        }
        for merge in plan
    ]
    canonical_json = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", default=DEFAULT_NAMESPACE, help="namespace (corpus:source)")
    parser.add_argument("--apply", action="store_true", help="aplica los merges (MUTA EL GRAFO)")
    parser.add_argument("--limit", type=int, default=None, help="maximo de grupos a aplicar")
    parser.add_argument("--approver", default=DEFAULT_APPROVER, help="identidad en el ledger")
    parser.add_argument("--backup", type=Path, help="backup fresco (obligatorio con --apply)")
    parser.add_argument("--approval", type=Path, help="archivo con la palabra 'approve'")
    parser.add_argument(
        "--expect-fingerprint",
        default=None,
        help="sha256 del dry-run revisado (obligatorio con --apply; aborta si difiere)",
    )
    parser.add_argument("--json", action="store_true", help="salida JSON (plan y resultado)")
    parser.add_argument(
        "--allow-stale-backup",
        action="store_true",
        help=f"permite un backup de mas de {STALE_BACKUP_HOURS} h",
    )
    return parser.parse_args(argv)


def _validate_apply_gates(args: argparse.Namespace) -> None:
    if args.approval is None or not args.approval.exists():
        sys.exit("APPLY abortado: falta --approval <archivo existente>")
    content = args.approval.read_text(encoding="utf-8").strip().lower()
    if APPROVAL_WORD not in content:
        sys.exit(f"APPLY abortado: {args.approval} debe contener la palabra '{APPROVAL_WORD}'")

    if args.backup is None or not args.backup.exists():
        sys.exit("APPLY abortado: falta --backup <json de backup fresco ya hecho>")
    age_hours = (datetime.now(UTC).timestamp() - args.backup.stat().st_mtime) / 3600
    size_mb = args.backup.stat().st_size / 1e6
    if not args.json:
        print(f"  backup: {args.backup} ({size_mb:.1f} MB, {age_hours:.1f} h)")
    if age_hours > STALE_BACKUP_HOURS and not args.allow_stale_backup:
        sys.exit(
            f"APPLY abortado: el backup tiene {age_hours:.1f} h (> {STALE_BACKUP_HOURS} h). "
            "Hacer uno fresco con scripts-ops/backup_only.py o pasar --allow-stale-backup."
        )


def _validate_apply_fingerprint(expect: str | None, computed: str) -> None:
    """Gate T10-C: ``--apply`` exige ``--expect-fingerprint``.

    Decision (spec abierta, elegida): se **RECHAZA** cuando falta, no se
    advierte — mas estricto que ``ledger rollback``, que solo nota la ausencia.
    Sin el hash revisado la mutacion no queda atada a la revision, que es todo
    el proposito del fingerprint. Tampoco se tolera un hash distinto: el plan
    cambio desde la revision y hay que volver a revisarlo.
    """
    if expect is None:
        sys.exit(
            "APPLY abortado: falta --expect-fingerprint. Corre primero el dry-run, "
            f"revisa el plan y repite con --expect-fingerprint {computed} "
            "(se rechaza en lugar de advertir: mismo contrato que ledger rollback)"
        )
    if expect != computed:
        sys.exit(
            f"FINGERPRINT abortado: reviewed {expect}, recomputed {computed} "
            "(el plan cambio desde la revision: grupos, orden o campos distintos)"
        )


async def _fetch_rows(session: AsyncSession, prefix: str) -> list[DuplicateMemberRow]:
    result = await session.run(_GROUPS_QUERY, prefix=prefix)
    records = await result.values()
    return [
        DuplicateMemberRow(
            name=str(record[0] or ""),
            kind=to_entity_type(str(record[1] or "")),
            entity_ids=tuple(str(entity_id) for entity_id in record[2]),
        )
        for record in records
    ]


async def _edge_impact(session: AsyncSession, ids: list[str]) -> dict[str, tuple[int, int]]:
    if not ids:
        return {}
    result = await session.run(_EDGE_IMPACT_QUERY, ids=ids)
    records = await result.values()
    return {str(record[0]): (int(record[1]), int(record[2])) for record in records}


async def _intra_group_related_rows(
    session: AsyncSession, member_ids: list[str]
) -> list[tuple[str, str]]:
    """Aristas RELATED con ambos extremos entre los miembros del plan (read-only)."""
    if not member_ids:
        return []
    result = await session.run(_INTRA_GROUP_RELATED_QUERY, member_ids=member_ids)
    records = await result.values()
    return [(str(record[0]), str(record[1])) for record in records]


async def _member_mention_rows(
    session: AsyncSession, member_ids: list[str]
) -> list[tuple[str, str]]:
    """Pares ``(chunk, entidad)`` de MENTIONS hacia miembros del plan (read-only)."""
    if not member_ids:
        return []
    result = await session.run(_MEMBER_MENTIONS_QUERY, member_ids=member_ids)
    records = await result.values()
    return [(str(record[0]), str(record[1])) for record in records]


async def _related_repoint_rows(
    session: AsyncSession, member_ids: list[str]
) -> list[tuple[str, str, str | None]]:
    """Aristas RELATED incidentes a los miembros: ``(src, dst, type)`` (read-only).

    El OR de los dos extremos es intencional: el conteo de colapsos del re-point
    necesita las aristas del canonical (``existing``) ademas de las de los
    miembros (``incoming``); el filtro "mismo grupo" vive en el contador.
    """
    if not member_ids:
        return []
    result = await session.run(_REPOINT_RELATED_ROWS_QUERY, member_ids=member_ids)
    records = await result.values()
    return [
        (str(record[0]), str(record[1]), None if record[2] is None else str(record[2]))
        for record in records
    ]


async def _read_census(session: AsyncSession, prefix: str) -> Census:
    record = await (await session.run(_CENSUS_QUERY, prefix=prefix)).single()
    if record is None:
        return Census(active_entities=0, merged_into_count=0, total_mentions=0, total_related=0)
    return Census(
        active_entities=int(record["active_entities"]),
        merged_into_count=int(record["merged_into_count"]),
        total_mentions=int(record["total_mentions"]),
        total_related=int(record["total_related"]),
    )


def _print_census(label: str, namespace: str, census_ns: Census, census_db: Census) -> None:
    print(
        f"  census {label} · namespace {namespace}: activas {census_ns.active_entities} · "
        f"merged_into {census_ns.merged_into_count} · MENTIONS {census_ns.total_mentions} · "
        f"RELATED {census_ns.total_related}"
    )
    print(
        f"  census {label} · db-wide: activas {census_db.active_entities} · "
        f"merged_into {census_db.merged_into_count} · MENTIONS {census_db.total_mentions} · "
        f"RELATED {census_db.total_related}"
    )


def _next_command(namespace: str, limit: int | None, fingerprint: str) -> str:
    parts = ["uv run --no-sync python scripts-ops/resolve_intra_ns.py", f"--namespace {namespace}"]
    if limit is not None:
        parts.append(f"--limit {limit}")
    parts.extend(
        [
            "--apply",
            "--backup",
            "<fresh.json>",
            "--approval",
            "<file con 'approve'>",
            "--expect-fingerprint",
            fingerprint,
        ]
    )
    return " ".join(parts)


def _plan_payload(
    *,
    mode: str,
    namespace: str,
    plan: list[PlannedMerge],
    scores: dict[str, tuple[int, int]],
    fingerprint: str,
    intra_group: int,
    mentions_collapses: int,
    repoint_collapses: int,
    census_before_ns: Census,
    census_before_db: Census,
    delta: Census,
) -> dict[str, Any]:
    duplicates = [dup for merge in plan for dup in merge.duplicate_ids]
    return {
        "mode": mode,
        "namespace": namespace,
        "fingerprint": fingerprint,
        "groups": [
            {
                "namespace": _namespace_of(merge.canonical_id),
                "name": merge.name,
                "kind": merge.kind,
                "canonical_id": merge.canonical_id,
                "duplicate_ids": list(merge.duplicate_ids),
            }
            for merge in plan
        ],
        "duplicates": len(duplicates),
        "edge_impact": {
            "mentions": sum(scores.get(dup, (0, 0))[0] for dup in duplicates),
            "related": sum(scores.get(dup, (0, 0))[1] for dup in duplicates),
        },
        "intra_group_related": intra_group,
        "mentions_collapses": mentions_collapses,
        "related_repoint_collapses": repoint_collapses,
        "census_before": {
            "namespace": asdict(census_before_ns),
            "db_wide": asdict(census_before_db),
        },
        "predicted_delta": asdict(delta),
    }


def _print_plan(
    plan: list[PlannedMerge],
    scores: dict[str, tuple[int, int]],
    fingerprint: str,
    census_before_ns: Census,
    census_before_db: Census,
    delta: Census,
    intra_group: int,
    mentions_collapses: int,
    repoint_collapses: int,
    namespace: str,
) -> None:
    duplicates = [dup for merge in plan for dup in merge.duplicate_ids]
    mentions = sum(scores.get(dup, (0, 0))[0] for dup in duplicates)
    related = sum(scores.get(dup, (0, 0))[1] for dup in duplicates)
    sizes: dict[int, int] = {}
    kinds: dict[str, int] = {}
    for merge in plan:
        sizes[len(merge.duplicate_ids) + 1] = sizes.get(len(merge.duplicate_ids) + 1, 0) + 1
        kinds[merge.kind] = kinds.get(merge.kind, 0) + 1
    print(f"  grupos de duplicados logicos: {len(plan)}")
    print(f"  duplicados a fusionar: {len(duplicates)}")
    print(f"  aristas a re-apuntar: MENTIONS {mentions} · RELATED {related}")
    print(f"  MENTIONS a colapsar: {mentions_collapses}")
    print(f"  RELATED intra-grupo a borrar: {intra_group}")
    print(f"  RELATED re-point a colapsar: {repoint_collapses}")
    print(f"  grupos por tamano: {dict(sorted(sizes.items()))}")
    print(f"  tipos: {dict(sorted(kinds.items()))}")
    print(f"  fingerprint: {fingerprint}")
    _print_census("antes", namespace, census_before_ns, census_before_db)
    print(
        "  delta predicha: "
        f"entidades activas {delta.active_entities:+d} · "
        f"merged_into {delta.merged_into_count:+d} · "
        f"MENTIONS {delta.total_mentions:+d} · "
        f"RELATED {delta.total_related:+d}"
    )


def _namespace_of(entity_id: str) -> str:
    parts = entity_id.split(":")
    return f"{parts[0]}:{parts[1]}" if len(parts) >= 2 else entity_id


def _normalized_form(entity_id: str) -> S0NormalizedForm:
    raw = entity_id.rsplit(":", 1)[-1].replace("-", " ")
    lowered = raw.lower()
    return S0NormalizedForm(
        original=raw,
        nfkc=raw,
        casefold=lowered,
        compact="".join(lowered.split()),
        tokens=tuple(lowered.split()),
    )


def _build_group(merge: PlannedMerge) -> MergeGroup:
    namespace = _namespace_of(merge.canonical_id)
    evidence = [
        ResolutionEvidence(
            anchor_id=merge.canonical_id,
            candidate_id=dup_id,
            anchor_type=merge.kind,
            candidate_type=merge.kind,
            anchor_namespace=namespace,
            candidate_namespace=_namespace_of(dup_id),
            anchor_normalized=_normalized_form(merge.canonical_id),
            candidate_normalized=_normalized_form(dup_id),
            s0_matched_field="id",
            band=ConfidenceBand.EXACT,
            cross_namespace=False,
            cross_type=False,
            composite_score=1.0,
        )
        for dup_id in merge.duplicate_ids
    ]
    return MergeGroup(
        canonical_id=merge.canonical_id,
        duplicate_ids=list(merge.duplicate_ids),
        band=ConfidenceBand.EXACT,
        evidence=evidence,
    )


async def _close_all(closables: list[Any]) -> None:
    for closable in closables:
        close = getattr(closable, "close", None)
        if close is not None:
            await close()


async def _apply(
    plan: list[PlannedMerge],
    settings: Settings,
    approver: str,
    *,
    quiet: bool = False,
) -> int:
    use_case, closables = await build_apply_merge_use_case(settings)
    applied = 0
    failures: list[str] = []
    try:
        for merge in plan:
            try:
                entry = await use_case.apply(_build_group(merge), approver=approver)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{merge.canonical_id}: {exc}")
                if not quiet:
                    print(f"  FAIL canon={merge.canonical_id}: {exc}")
                continue
            applied += 1
            short = merge.canonical_id.split(":")[-1]
            if not quiet:
                print(
                    f"  [{applied}/{len(plan)}] seq={entry.seq} canon={short} "
                    f"dups={len(merge.duplicate_ids)}"
                )
    finally:
        await _close_all(closables)
    if not quiet:
        print(f"\n  merges aplicados: {applied} · fallos: {len(failures)}")
        for failure in failures:
            print(f"    · {failure}")
    return 3 if failures else 0


async def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    namespace: str = args.namespace
    prefix = f"{namespace}:"
    settings = Settings.model_validate({})
    mode = "APPLY" if args.apply else "DRY-RUN"
    if not args.json:
        print(f"== MODO {mode} ==")
        print(f"  namespace: {namespace}")

    driver = AsyncGraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value()),
    )
    try:
        async with driver.session() as session:
            rows = await _fetch_rows(session, prefix)
            member_ids = sorted({member for row in rows for member in row.entity_ids})
            scores = await _edge_impact(session, member_ids)
            try:
                plan = plan_intra_resolution(rows, scores=scores)
            except ValueError as exc:
                sys.exit(f"PLAN abortado: fila invalida en el grafo ({exc})")
            if args.limit is not None:
                if args.limit < 1:
                    sys.exit("--limit debe ser un entero positivo")
                if args.limit < len(plan):
                    if not args.json:
                        print(f"  (limitado a {args.limit} grupos por --limit)")
                    plan = plan[: args.limit]
            fingerprint = plan_fingerprint(plan)
            plan_member_ids = sorted(
                {member for merge in plan for member in (merge.canonical_id, *merge.duplicate_ids)}
            )
            related_rows = await _intra_group_related_rows(session, plan_member_ids)
            mention_rows = await _member_mention_rows(session, plan_member_ids)
            repoint_rows = await _related_repoint_rows(session, plan_member_ids)
            intra_group = count_intra_group_related(plan, related_rows)
            collapses = count_mentions_collapses(plan, mention_rows)
            repoint = count_related_repoint_collapses(plan, repoint_rows)
            census_before_ns = await _read_census(session, prefix)
            census_before_db = await _read_census(session, "")
            delta = predicted_census_delta(plan, collapses, intra_group, repoint)
            payload = _plan_payload(
                mode=mode,
                namespace=namespace,
                plan=plan,
                scores=scores,
                fingerprint=fingerprint,
                intra_group=intra_group,
                mentions_collapses=collapses,
                repoint_collapses=repoint,
                census_before_ns=census_before_ns,
                census_before_db=census_before_db,
                delta=delta,
            )

            if not args.apply:
                if args.json:
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
                else:
                    _print_plan(
                        plan,
                        scores,
                        fingerprint,
                        census_before_ns,
                        census_before_db,
                        delta,
                        intra_group,
                        collapses,
                        repoint,
                        namespace,
                    )
                    print("\n== DRY-RUN: nada mutado ==")
                    print(f"  Para aplicar: {_next_command(namespace, args.limit, fingerprint)}")
                return

            if not args.json:
                _print_plan(
                    plan,
                    scores,
                    fingerprint,
                    census_before_ns,
                    census_before_db,
                    delta,
                    intra_group,
                    collapses,
                    repoint,
                    namespace,
                )
            _validate_apply_gates(args)
            _validate_apply_fingerprint(args.expect_fingerprint, fingerprint)
            if not args.json:
                print(f"  fingerprint ok: {fingerprint}")
                print(f"\n== APLICANDO {len(plan)} grupos (approver={args.approver}) ==")
            exit_code = await _apply(plan, settings, str(args.approver), quiet=bool(args.json))

        async with driver.session() as session:
            census_after_ns = await _read_census(session, prefix)
            census_after_db = await _read_census(session, "")
        drift = census_drift(census_before_ns, census_after_ns, delta) + census_drift(
            census_before_db, census_after_db, delta
        )

        if args.json:
            payload.update(
                {
                    "applied_exit_code": exit_code,
                    "census_after": {
                        "namespace": asdict(census_after_ns),
                        "db_wide": asdict(census_after_db),
                    },
                    "drift": drift,
                }
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            _print_census("despues", namespace, census_after_ns, census_after_db)
            if drift:
                for line in drift:
                    print(f"  DRIFT {line}")
            else:
                print("  census drift: none")

        if exit_code != 0:
            sys.exit(exit_code)
        if drift:
            print(
                "post-apply census MISMATCH: el grafo no coincide con el plan revisado "
                "(revisa las lineas DRIFT antes de cualquier otra accion)",
                file=sys.stderr,
            )
            sys.exit(1)
        if not args.json:
            print("APPLY_OK")
        sys.exit(0)
    finally:
        await driver.close()


if __name__ == "__main__":
    asyncio.run(main())
