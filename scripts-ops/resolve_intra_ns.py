"""Resolucion SCOPED de duplicados logicos intra-namespace (merge + ledger).

Contexto: la extraccion LLM de un libro produce el mismo concepto con varios ids
(sigla vs expansion, ingles vs espanol). El audit los reporta como warning
``DUPLICATE_ENTITY_LOGICAL``; este script los fusiona dentro de UN namespace con
el use case real de produccion (soft-delete ``merged_into`` + re-point de
MENTIONS/RELATED + fold de aliases + entrada encadenada en el ledger), nunca con
Cypher de escritura a mano.

Los grupos NO se hardcodean: se derivan de la regla del audit, espejo exacto de
``duplicates_entity`` en ``infrastructure/neo4j_audit_adapter.py`` (entidades no
fusionadas, ``id`` no nulo, agrupadas por ``name`` + ``type`` exactos, con el
scope ``n.id STARTS WITH $prefix`` que inyecta ``_scope_predicate``).
Canonical = id mas corto (ties lexicografico), la receta de las resoluciones
previas; el resto de los miembros se pliegan como duplicados.

Fases:
  Fase 1 (default, READ-ONLY): reporta el plan completo y el impacto de aristas.
  Fase 2 (--apply): aplica los merges. MUTA EL GRAFO y exige:
    - ``--backup <json>``  backup fresco ya hecho (scripts-ops/backup_only.py).
      Se rechaza si tiene mas de 24 h salvo ``--allow-stale-backup``.
    - ``--approval <file>`` cuyo contenido incluya la palabra "approve"
      (AGENTS.md 7.2, mismo contrato que el cleanup scoped).

Efecto secundario conocido: el adapter re-apunta RELATED con
``MERGE (canon)-[r2:RELATED]->(other)`` sin excluir ``other == canonical``, asi
que una arista RELATED entre dos miembros del MISMO grupo queda como self-loop.
El script lo CUENTA y lo reporta; borrar es un paso aparte con aprobacion humana.
Re-ejecutar es idempotente: la regla del audit ignora las entidades ya fusionadas.

Uso (desde la raiz del repo en el OrangePi, con el .env sourceado):
    uv run --no-sync python scripts-ops/resolve_intra_ns.py
    uv run --no-sync python scripts-ops/resolve_intra_ns.py --apply --limit 3 \\
        --backup ~/backups_neo4j/bookgraph_backup_YYYYMMDDTHHMMSSZ.json \\
        --approval /tmp/approve_huyen_resolve.txt
"""

from __future__ import annotations

import argparse
import asyncio
import sys
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
from book_graph_rag.infrastructure.resolution_wiring import build_apply_merge_use_case

DEFAULT_NAMESPACE = "knowledge:ai-engineering-huyen"
DEFAULT_APPROVER = "human:gonzalo"
STALE_BACKUP_HOURS = 24
APPROVAL_WORD = "approve"

# Espejo exacto de la regla DUPLICATE_ENTITY_LOGICAL del audit
# (query "duplicates_entity") con el scope inyectado tras el primer WHERE.
_GROUPS_QUERY = """
MATCH (n:Entity)
WHERE (n.id STARTS WITH $prefix)
  AND (n.merged_into IS NULL OR n.merged_into = '')
  AND n.id IS NOT NULL
WITH n, split(n.id, ':') AS parts WHERE size(parts) >= 2
WITH n, parts ORDER BY coalesce(n.id, '')
WITH n, parts[0] + ':' + parts[1] AS namespace, n.name AS name, n.type AS kind
WITH namespace, name, kind, collect(n) AS members WHERE size(members) > 1
WITH namespace, name, kind, members
ORDER BY namespace, coalesce(name, ''), coalesce(kind, '')
RETURN name, kind, [m IN members | coalesce(m.id, '')] AS ids
ORDER BY name, kind
"""

_EDGE_IMPACT_QUERY = """
UNWIND $ids AS did
MATCH (d:Entity {id: did})
RETURN did AS id,
       size([(c:Chunk)-[:MENTIONS]->(d) | c]) AS mentions_in,
       size([(d)-[r:RELATED]-() | r]) AS related_edges
"""

# Aristas RELATED entre dos miembros ACTIVOS del MISMO grupo (name+type iguales):
# son las que el merge convertiria en self-loop o en arista hacia un soft-deleted.
_INTRA_GROUP_RELATED_QUERY = """
MATCH (a:Entity)-[r:RELATED]->(b:Entity)
WHERE a.id STARTS WITH $prefix AND b.id STARTS WITH $prefix AND a.id < b.id
  AND (a.merged_into IS NULL OR a.merged_into = '')
  AND (b.merged_into IS NULL OR b.merged_into = '')
  AND a.name = b.name AND a.type = b.type
RETURN count(r) AS c
"""


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", default=DEFAULT_NAMESPACE, help="namespace (corpus:source)")
    parser.add_argument("--apply", action="store_true", help="aplica los merges (MUTA EL GRAFO)")
    parser.add_argument("--limit", type=int, default=None, help="maximo de grupos a aplicar")
    parser.add_argument("--approver", default=DEFAULT_APPROVER, help="identidad en el ledger")
    parser.add_argument("--backup", type=Path, help="backup fresco (obligatorio con --apply)")
    parser.add_argument("--approval", type=Path, help="archivo con la palabra 'approve'")
    parser.add_argument(
        "--allow-stale-backup",
        action="store_true",
        help=f"permite un backup de mas de {STALE_BACKUP_HOURS} h",
    )
    return parser.parse_args()


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
    print(f"  backup: {args.backup} ({size_mb:.1f} MB, {age_hours:.1f} h)")
    if age_hours > STALE_BACKUP_HOURS and not args.allow_stale_backup:
        sys.exit(
            f"APPLY abortado: el backup tiene {age_hours:.1f} h (> {STALE_BACKUP_HOURS} h). "
            "Hacer uno fresco con scripts-ops/backup_only.py o pasar --allow-stale-backup."
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


def _print_plan(plan: list[PlannedMerge], impact: dict[str, tuple[int, int]]) -> None:
    duplicates = [dup for merge in plan for dup in merge.duplicate_ids]
    mentions = sum(impact.get(dup, (0, 0))[0] for dup in duplicates)
    related = sum(impact.get(dup, (0, 0))[1] for dup in duplicates)
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


async def _apply(plan: list[PlannedMerge], settings: Settings, approver: str) -> int:
    use_case, closables = await build_apply_merge_use_case(settings)
    applied = 0
    failures: list[str] = []
    try:
        for merge in plan:
            try:
                entry = await use_case.apply(_build_group(merge), approver=approver)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{merge.canonical_id}: {exc}")
                print(f"  FAIL canon={merge.canonical_id}: {exc}")
                continue
            applied += 1
            short = merge.canonical_id.split(":")[-1]
            print(
                f"  [{applied}/{len(plan)}] seq={entry.seq} canon={short} "
                f"dups={len(merge.duplicate_ids)}"
            )
    finally:
        await _close_all(closables)
    print(f"\n  merges aplicados: {applied} · fallos: {len(failures)}")
    if failures:
        for failure in failures:
            print(f"    · {failure}")
        return 3
    return 0


async def main() -> None:
    args = _parse_args()
    namespace: str = args.namespace
    prefix = f"{namespace}:"
    settings = Settings.model_validate({})
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"== MODO {mode} ==")
    print(f"  namespace: {namespace}")

    driver = AsyncGraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value()),
    )
    try:
        async with driver.session() as session:
            rows = await _fetch_rows(session, prefix)
            try:
                plan = plan_intra_resolution(rows)
            except ValueError as exc:
                sys.exit(f"PLAN abortado: fila invalida en el grafo ({exc})")
            duplicates = sorted({dup for merge in plan for dup in merge.duplicate_ids})
            impact = await _edge_impact(session, duplicates)
            intra_group = await _intra_group_related(session, prefix)
            _print_plan(plan, impact)
            print(f"  RELATED intra-grupo (quedarian como self-loop o soft-deleted): {intra_group}")

            if not args.apply:
                print("\n== DRY-RUN: nada mutado ==")
                print("  Para aplicar: --apply --backup <json> --approval <file con 'approve'>")
                return
            _validate_apply_gates(args)
    finally:
        await driver.close()

    if args.limit is not None:
        if args.limit < 1:
            sys.exit("--limit debe ser un entero positivo")
        plan = plan[: args.limit]
        print(f"  (limitado a {len(plan)} grupos por --limit)")

    print(f"\n== APLICANDO {len(plan)} grupos (approver={args.approver}) ==")
    exit_code = await _apply(plan, settings, str(args.approver))
    if exit_code == 0:
        print("APPLY_OK")
    sys.exit(exit_code)


if __name__ == "__main__":
    asyncio.run(main())
