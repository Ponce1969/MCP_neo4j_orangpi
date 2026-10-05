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
   RELATED, por namespace y db-wide. La delta predicha es UNA entidad activa
   menos y UN ``merged_into`` mas por duplicado fusionado; los totales de aristas
   son INVARIANTES (el merge re-apunta, nunca borra). Tras ``--apply`` se mide y
   cualquier drift imprime lineas DRIFT y sale con codigo distinto de cero.

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

Efecto secundario conocido: el adapter re-apunta RELATED con
``MERGE (canon)-[r2:RELATED]->(other)`` sin excluir ``other == canonical``, asi
que una arista RELATED entre dos miembros del MISMO grupo queda como self-loop.
El script lo CUENTA (con la misma clave de agrupacion, insensible a
mayusculas) y lo reporta; borrar es un paso aparte con aprobacion humana.
Re-ejecutar es idempotente: la regla del audit ignora las entidades ya
fusionadas.

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

# Aristas RELATED entre dos miembros ACTIVOS del MISMO grupo: la misma
# construccion de grupos que _GROUPS_QUERY (clave desde la constante
# compartida, insensible a mayusculas desde T9b), de modo que el reporte mide
# exactamente la poblacion que el plan fusiona — con la clave exacta anterior
# los pares solo-de-mayusculas quedarian invisibles y el riesgo se reportaria
# en silencio. Son las aristas que el merge convertiria en self-loop o en
# arista hacia un soft-deleted; a.id < b.id cuenta cada par una sola vez.
_INTRA_GROUP_RELATED_QUERY = f"""
MATCH (n:Entity)
WHERE n.id STARTS WITH $prefix
  AND (n.merged_into IS NULL OR n.merged_into = '')
  AND n.id IS NOT NULL
WITH n, split(n.id, ':') AS parts WHERE size(parts) >= 2
WITH n, parts ORDER BY coalesce(n.id, '')
WITH n, parts[0] + ':' + parts[1] AS namespace,
     {DUPLICATE_GROUP_KEY_EXPRESSION} AS name, n.type AS kind
WITH namespace, name, kind, collect(n) AS members WHERE size(members) > 1
UNWIND members AS a
MATCH (a)-[r:RELATED]->(b)
WHERE b IN members AND a.id < b.id
RETURN count(r) AS c
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


def predicted_census_delta(plan: Sequence[PlannedMerge]) -> Census:
    """Delta predicha: UNA activa menos y UN ``merged_into`` mas por duplicado.

    Con los grupos de dos miembros (la poblacion case-only) esto se lee como
    "una entidad menos y un ``merged_into`` mas por grupo"; un grupo de N
    miembros aporta N-1 duplicados. Los totales de aristas son INVARIANTES
    porque el merge re-apunta MENTIONS/RELATED al canonical, nunca los borra.
    """
    duplicates = sum(len(merge.duplicate_ids) for merge in plan)
    return Census(
        active_entities=-duplicates,
        merged_into_count=duplicates,
        total_mentions=0,
        total_related=0,
    )


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
            f"!= esperado {expected_mentions} (el merge solo re-apunta aristas)"
        )
    expected_related = before.total_related + predicted.total_related
    if after.total_related != expected_related:
        drift.append(
            f"RELATED {before.total_related} -> {after.total_related} "
            f"!= esperado {expected_related} (el merge solo re-apunta aristas)"
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


async def _intra_group_related(session: AsyncSession, prefix: str) -> int:
    record = await (await session.run(_INTRA_GROUP_RELATED_QUERY, prefix=prefix)).single()
    return int(record[0]) if record is not None else 0


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
    print(f"  grupos por tamano: {dict(sorted(sizes.items()))}")
    print(f"  tipos: {dict(sorted(kinds.items()))}")
    print(f"  RELATED intra-grupo (quedarian como self-loop o soft-deleted): {intra_group}")
    print(f"  fingerprint: {fingerprint}")
    _print_census("antes", namespace, census_before_ns, census_before_db)
    print(
        "  delta predicha: "
        f"entidades activas {delta.active_entities:+d} · "
        f"merged_into {delta.merged_into_count:+d} · "
        f"MENTIONS {delta.total_mentions:+d} (invariante) · "
        f"RELATED {delta.total_related:+d} (invariante)"
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
            intra_group = await _intra_group_related(session, prefix)
            census_before_ns = await _read_census(session, prefix)
            census_before_db = await _read_census(session, "")
            delta = predicted_census_delta(plan)
            payload = _plan_payload(
                mode=mode,
                namespace=namespace,
                plan=plan,
                scores=scores,
                fingerprint=fingerprint,
                intra_group=intra_group,
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
