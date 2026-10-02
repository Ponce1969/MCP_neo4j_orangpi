"""Re-apunta aristas que apuntan a entidades soft-deleted (``merged_into``).

Dos clases de arista, ambas heredadas de la resolucion de septiembre en
``knowledge:essential-graphrag`` y medidas en produccion (286 en total):

1. **MENTIONS**: ``(:Chunk)-[:MENTIONS]->(:Entity)`` donde la entidad tiene
   ``merged_into``. Se mueve al canonico terminal de la cadena (nunca se borra:
   el conocimiento del chunk sobrevive).
2. **RELATED**: ``(:Entity)-[r:RELATED]->(:Entity)`` con al menos un extremo
   fusionado. Se re-apunta al canonico de cada extremo; solo se BORRA si ambos
   extremos colapsan sobre el mismo nodo (se convertiria en self-loop).

La resolucion de canonicos es transitiva con guarda de ciclos (D2); los ciclos
y las cadenas que terminan en un destino inexistente se EXCLUYEN y se reportan,
nunca se adivinan (D3). Toda la logica vive en
``book_graph_rag.domain.merged_endpoint_resolution``; este script solo hace I/O.

Modos (``--mode``, default ``repoint``):
  repoint      re-apunta las aristas colgantes al canonico terminal (D1): el
               comportamiento historico del script, sin cambios.
  break-cycles detecta los ciclos mutuos de ``merged_into`` (pares donde cada
               miembro esta marcado como fusionado hacia el otro), elige al
               ganador por grado vivo — el miembro que porta el conocimiento —
               y con ``--apply`` limpia SOLO ``merged_into``/``merged_at`` del
               ganador; los perdedores quedan marcados hacia un nodo ahora
               vivo y las aristas dejan de colgar. Esos ciclos se excluian como
               D3 (en produccion frenaban los 286 edges); el maintainer aprobo
               romperlos. ``--mode repoint`` limpia lo que quede despues.

Fases:
  Fase 1 (default, READ-ONLY / dry-run): inventario + bundle de evidencia.
  Fase 2 (--apply): MUTACION DESTRUCTIVA y exige (AGENTS.md §7.2):
    - ``--backup <json>``  backup fresco ya hecho (scripts-ops/backup_only.py).
      Se rechaza si tiene mas de 24 h salvo ``--allow-stale-backup``.
    - ``--approval <file>`` cuyo contenido incluya la palabra "approve".
  En AMBAS fases se escribe el bundle de evidencia ANTES de cualquier
  mutacion, con las propiedades vivas de cada arista, de modo que la operacion
  sea reversible re-apuntando de vuelta a los endpoints originales. El apply NO
  es atomico (una query por lote): si falla a mitad, la verificacion posterior
  sale en rojo y el bundle es la ruta de recuperacion.

Uso (desde la raiz del repo, con el .env sourceado):
    uv run --no-sync python scripts-ops/repoint_merged_endpoint_edges.py
    uv run --no-sync python scripts-ops/repoint_merged_endpoint_edges.py \\
        --namespace knowledge:essential-graphrag
    uv run --no-sync python scripts-ops/repoint_merged_endpoint_edges.py --apply \\
        --backup ~/backups_neo4j/bookgraph_backup_YYYYMMDDTHHMMSSZ.json \\
        --approval /tmp/approve_repoint.txt
    uv run --no-sync python scripts-ops/repoint_merged_endpoint_edges.py \\
        --mode break-cycles --snapshot /tmp/bundle_cycles.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from neo4j import AsyncGraphDatabase, AsyncSession

from book_graph_rag.config import Settings
from book_graph_rag.domain.merged_endpoint_resolution import (
    CycleRepair,
    DanglingEdge,
    EdgeAction,
    RepointPlan,
    plan_cycle_repairs,
    plan_repoint,
)

BUNDLE_SCHEMA = "merged-endpoint-edges-bundle/1"
STALE_BACKUP_HOURS = 24
APPROVAL_WORD = "approve"
LEDGER_PATH = Path("data/resolution/merge_ledger.jsonl")
SNAPSHOT_DIR = Path("evidence-bundles")
SNAPSHOT_PREFIX = "merged-endpoint-edges"
BATCH_SIZE = 500

# ── Lectura (solo Cypher; ninguna decision de dominio vive aca) ────────────

_QUERY_MERGED = (
    "MATCH (e:Entity) WHERE e.merged_into IS NOT NULL RETURN e.id AS id, e.merged_into AS target"
)
_QUERY_KNOWN = "MATCH (e:Entity) WHERE e.id IN $ids RETURN e.id AS id"

_COUNT_MENTIONS_MERGED = (
    "MATCH (c:Chunk)-[:MENTIONS]->(e:Entity) WHERE e.merged_into IS NOT NULL RETURN count(*) AS c"
)
_COUNT_RELATED_MERGED = (
    "MATCH (a:Entity)-[r:RELATED]->(b:Entity) "
    "WHERE a.merged_into IS NOT NULL OR b.merged_into IS NOT NULL RETURN count(r) AS c"
)
_COUNT_MERGED = "MATCH (e:Entity) WHERE e.merged_into IS NOT NULL RETURN count(*) AS c"

# El lado fusionado se acota con $prefix ("" cuando no hay filtro: todo id
# empieza con la cadena vacia, asi que la consulta es identica sin filtro).
_READ_MENTIONS = """
MATCH (c:Chunk)-[m:MENTIONS]->(e:Entity)
WHERE e.merged_into IS NOT NULL AND e.id STARTS WITH $prefix
RETURN c.source_id AS source_id, c.chunk_index AS chunk_index, c.book_id AS book_id,
       e.id AS target, properties(m) AS properties
"""
# Arista dirigida: cubre fusionado-como-origen y fusionado-como-destino en una
# sola pasada, exactamente una fila por arista (un patron no dirigido devolveria
# dos). El OR incluye tambien aristas con LOS DOS extremos fusionados.
_READ_RELATED = """
MATCH (a:Entity)-[r:RELATED]->(b:Entity)
WHERE (a.merged_into IS NOT NULL OR b.merged_into IS NOT NULL)
  AND ((a.merged_into IS NOT NULL AND a.id STARTS WITH $prefix)
    OR (b.merged_into IS NOT NULL AND b.id STARTS WITH $prefix))
RETURN a.id AS source_id, r.type AS relation_type, b.id AS target_id,
       properties(r) AS properties
"""

# ── Grados vivos por entidad fusionada (solo modo break-cycles) ───────────
# Una query por tipo de arista, combinadas en Python: el grado vivo de una
# entidad fusionada es su cantidad de MENTIONS entrantes mas sus RELATED
# incidentes. Es la medida con la que se elige el ganador de cada ciclo.
_QUERY_DEGREE_MENTIONS = (
    "MATCH (e:Entity)<-[:MENTIONS]-(:Chunk) WHERE e.merged_into IS NOT NULL "
    "RETURN e.id AS id, count(*) AS c"
)
_QUERY_DEGREE_RELATED = (
    "MATCH (e:Entity)-[:RELATED]-() WHERE e.merged_into IS NOT NULL "
    "RETURN e.id AS id, count(*) AS c"
)
# Marcas previas de cada ganador: quedan en el bundle para poder revertir a mano.
_QUERY_WINNER_MARKS = (
    "MATCH (e:Entity) WHERE e.id IN $ids "
    "RETURN e.id AS id, e.merged_into AS merged_into, e.merged_at AS merged_at"
)
_QUERY_STILL_MARKED = (
    "MATCH (e:Entity) WHERE e.id IN $ids AND e.merged_into IS NOT NULL RETURN e.id AS id"
)

# ── Escritura (solo ids exactos del plan; nunca se rederivan claves de chunk) ─

_WRITE_MENTIONS = """
UNWIND $entries AS e
MATCH (c:Chunk)-[m:MENTIONS]->(merged:Entity {id: e.merged_id})
MATCH (canon:Entity {id: e.canonical_id})
MERGE (c)-[m2:MENTIONS]->(canon)
ON CREATE SET m2 += properties(m)
ON MATCH SET m2 += properties(m)
DELETE m
"""

_WRITE_RELATED_REPOINT = """
UNWIND $entries AS e
MATCH (a:Entity {id: e.source_id})-[r:RELATED]->(b:Entity {id: e.target_id})
WHERE r.type = e.relation_type
MATCH (new_source:Entity {id: e.new_source_id})
MATCH (new_target:Entity {id: e.new_target_id})
MERGE (new_source)-[r2:RELATED {type: e.relation_type}]->(new_target)
SET r2 += properties(r)
DELETE r
"""

_WRITE_RELATED_DELETE = """
UNWIND $entries AS e
MATCH (a:Entity {id: e.source_id})-[r:RELATED]->(b:Entity {id: e.target_id})
WHERE r.type = e.relation_type
DELETE r
"""

# Rompe el ciclo: exactamente los ids de los ganadores del plan, sin tocar a
# nadie mas. Los perdedores conservan merged_into apuntando al ganador vivo.
_WRITE_CLEAR_WINNER = "MATCH (e:Entity) WHERE e.id IN $ids REMOVE e.merged_into, e.merged_at"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--namespace",
        default=None,
        help="acota el lado fusionado por prefijo de id (p. ej. knowledge:essential-graphrag)",
    )
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=None,
        help="ruta del bundle de evidencia "
        "(default: evidence-bundles/merged-endpoint-edges-<UTC>.json)",
    )
    parser.add_argument(
        "--mode",
        choices=("repoint", "break-cycles"),
        default="repoint",
        help="repoint (default): re-apunta las aristas al canonico; "
        "break-cycles: limpia merged_into/merged_at solo en el ganador de cada ciclo",
    )
    parser.add_argument("--apply", action="store_true", help="muta el grafo (DESTRUCTIVO)")
    parser.add_argument("--backup", type=Path, help="backup fresco (obligatorio con --apply)")
    parser.add_argument("--approval", type=Path, help="archivo con la palabra 'approve'")
    parser.add_argument(
        "--allow-stale-backup",
        action="store_true",
        help=f"permite un backup de mas de {STALE_BACKUP_HOURS} h",
    )
    return parser.parse_args()


def _validate_apply_gates(args: argparse.Namespace) -> dict[str, Any]:
    """Valida el gate §7.2 ANTES de escribir nada; sale con mensaje en español."""
    if args.approval is None or not args.approval.exists():
        sys.exit("APPLY abortado: falta --approval <archivo existente>")
    content = args.approval.read_text(encoding="utf-8").strip().lower()
    if APPROVAL_WORD not in content:
        sys.exit(f"APPLY abortado: {args.approval} debe contener la palabra '{APPROVAL_WORD}'")

    if args.backup is None or not args.backup.exists():
        sys.exit("APPLY abortado: falta --backup <json de backup fresco ya hecho>")
    age_hours = (datetime.now(UTC).timestamp() - args.backup.stat().st_mtime) / 3600
    size_mb = args.backup.stat().st_size / 1e6
    if age_hours > STALE_BACKUP_HOURS and not args.allow_stale_backup:
        sys.exit(
            f"APPLY abortado: el backup tiene {age_hours:.1f} h (> {STALE_BACKUP_HOURS} h). "
            "Hacer uno fresco con scripts-ops/backup_only.py o pasar --allow-stale-backup."
        )
    return {
        "path": str(args.backup),
        "age_hours": round(age_hours, 2),
        "size_mb": round(size_mb, 1),
        "approval_file": str(args.approval),
    }


async def _scalar(session: AsyncSession, query: str, **params: Any) -> int:
    record = await (await session.run(query, **params)).single()
    return int(record[0]) if record is not None else 0


async def _rows(session: AsyncSession, query: str, **params: Any) -> list[list[Any]]:
    return await (await session.run(query, **params)).values()


async def _run_write(session: AsyncSession, query: str, **params: Any) -> None:
    result = await session.run(query, **params)
    await result.consume()


async def _read_live_degrees(session: AsyncSession) -> tuple[dict[str, int], dict[str, int]]:
    """Grados vivos por entidad fusionada: (mentions, related), una query por tipo."""
    mentions = {str(row[0]): int(row[1]) for row in await _rows(session, _QUERY_DEGREE_MENTIONS)}
    related = {str(row[0]): int(row[1]) for row in await _rows(session, _QUERY_DEGREE_RELATED)}
    return mentions, related


def _namespace_of(entity_id: str) -> str:
    parts = entity_id.split(":")
    return ":".join(parts[:2]) if len(parts) >= 2 else entity_id


def _edge_namespace(edge: DanglingEdge, merged: dict[str, str]) -> str:
    for endpoint in (edge.source_id, edge.target_id):
        if endpoint in merged:
            return _namespace_of(endpoint)
    if edge.kind == "MENTIONS":
        return _namespace_of(edge.target_id)
    return _namespace_of(edge.source_id)


def _chunk_key(row: list[Any]) -> str:
    source_id, chunk_index, book_id = row[0], row[1], row[2]
    base = source_id if source_id is not None else book_id
    return f"{base}:chunk-{chunk_index}"


def _ledger_lines() -> int | None:
    if not LEDGER_PATH.exists():
        return None
    with LEDGER_PATH.open(encoding="utf-8") as handle:
        return sum(1 for line in handle if line.strip())


def _git_head() -> str:
    try:
        done = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return "unknown"
    return done.stdout.strip()


def _inventory(plan: RepointPlan, merged: dict[str, str]) -> None:
    """Imprime el inventario de la spec §2: namespace x tipo x tipo de RELATED."""
    buckets: dict[tuple[str, str, str], dict[str, Any]] = {}

    def _bucket(edge: DanglingEdge) -> dict[str, Any]:
        rel = edge.relation_type or "-"
        key = (_edge_namespace(edge, merged), edge.kind, rel)
        if key not in buckets:
            buckets[key] = {
                "edges": 0,
                "repoint": 0,
                "keys": set(),
                "delete": 0,
                "excluded": 0,
            }
        return buckets[key]

    for entry in plan.entries:
        bucket = _bucket(entry.edge)
        bucket["edges"] += 1
        if entry.action is EdgeAction.DELETE_COLLAPSE:
            bucket["delete"] += 1
        else:
            bucket["repoint"] += 1
            bucket["keys"].add(
                f"{entry.new_source_id}|{entry.edge.kind}|"
                f"{entry.edge.relation_type}|{entry.new_target_id}"
            )
    for excluded_entry in plan.excluded:
        bucket = _bucket(excluded_entry.edge)
        bucket["edges"] += 1
        bucket["excluded"] += 1

    print("\n== INVENTARIO (namespace x tipo x relacion) ==")
    header = (
        f"  {'namespace':<30} {'tipo':<9} {'rel':<15} {'aristas':>7} {'repoint':>7} "
        f"{'keys':>6} {'colapsan':>9} {'delete':>6} {'excl':>5}"
    )
    print(header)
    totals: dict[str, Any] = {"edges": 0, "repoint": 0, "keys": set(), "delete": 0, "excluded": 0}
    for key in sorted(buckets):
        bucket = buckets[key]
        collapsed = bucket["repoint"] - len(bucket["keys"])
        print(
            f"  {key[0]:<30} {key[1]:<9} {key[2]:<15} {bucket['edges']:>7} "
            f"{bucket['repoint']:>7} {len(bucket['keys']):>6} {collapsed:>9} "
            f"{bucket['delete']:>6} {bucket['excluded']:>5}"
        )
        for field in ("edges", "repoint", "delete", "excluded"):
            totals[field] += bucket[field]
        totals["keys"] |= bucket["keys"]
    collapsed_total = totals["repoint"] - len(totals["keys"])
    print(
        f"  {'TOTAL':<30} {'':<9} {'':<15} {totals['edges']:>7} {totals['repoint']:>7} "
        f"{len(totals['keys']):>6} {collapsed_total:>9} {totals['delete']:>6} "
        f"{totals['excluded']:>5}"
    )


def _print_anomalies(plan: RepointPlan) -> None:
    print("\n== ANOMALIAS (excluidas del re-point, D3) ==")
    if not plan.excluded:
        print("  (ninguna)")
        return
    for entry in plan.excluded:
        rel = entry.edge.relation_type or entry.edge.kind
        print(
            f"    {entry.edge.source_id} -[{rel}]-> {entry.edge.target_id}   "
            f"motivo: {entry.reason.value}"
        )


def _print_summary(edges: list[DanglingEdge], plan: RepointPlan) -> None:
    mentions = sum(1 for edge in edges if edge.kind == "MENTIONS")
    print("\n== RESUMEN DEL PLAN ==")
    print(
        f"  aristas leidas:      {len(edges):>6} (MENTIONS {mentions} / RELATED "
        f"{len(edges) - mentions})"
    )
    print(f"  re-point (D1):       {plan.repoint_count:>6}")
    print(f"  DELETE_COLLAPSE:     {plan.collapse_count:>6}")
    print(f"  merge keys:          {plan.merge_key_count:>6}")
    print(f"  colapsadas MERGE:    {plan.merge_collapsed:>6}")
    print(f"  excluidas (D3):      {len(plan.excluded):>6}")


def _batched(items: list[Any]) -> list[list[Any]]:
    return [items[i : i + BATCH_SIZE] for i in range(0, len(items), BATCH_SIZE)] or [[]]


def _print_cycles(
    repairs: tuple[CycleRepair, ...],
    *,
    mentions: dict[str, int],
    related: dict[str, int],
) -> None:
    """Imprime cada ciclo: miembros, grados vivos, ganador y la razon de la eleccion."""
    print("\n== CICLOS DE merged_into (break-cycles) ==")
    if not repairs:
        print("  sin ciclos: no hay pares/cadenas mutuos en merged_into; nada que romper.")
        return
    for index, repair in enumerate(repairs, start=1):
        print(f"  ciclo {index}: {repair.cycle}")
        for member in repair.cycle:
            m_degree = mentions.get(member, 0)
            r_degree = related.get(member, 0)
            print(
                f"    miembro:    {member}  grado vivo "
                f"MENTIONS={m_degree} RELATED={r_degree} total={m_degree + r_degree}"
            )
        print(f"    ganador:    {repair.winner}")
        losers = ", ".join(repair.losers) if repair.losers else "(ninguno: auto-referencia)"
        print(f"    perdedores: {losers}")
        print(
            "    razon: el ganador es el miembro que porta el conocimiento "
            "(mayor grado vivo; empates: id mas corto y despues lexicografico); "
            "a EL se le limpia merged_into/merged_at y los perdedores quedan "
            "marcados hacia un nodo ahora vivo."
        )


async def _pre_state(session: AsyncSession) -> dict[str, Any]:
    return {
        "mentions_to_merged": await _scalar(session, _COUNT_MENTIONS_MERGED),
        "related_to_merged": await _scalar(session, _COUNT_RELATED_MERGED),
        "merged_entities": await _scalar(session, _COUNT_MERGED),
        "ledger_lines": _ledger_lines(),
    }


def _write_bundle(path: Path, bundle: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(bundle, handle, indent=2, ensure_ascii=False, default=str)
        handle.write("\n")
    print(f"  bundle de evidencia: {path}")


async def _apply_plan(session: AsyncSession, plan: RepointPlan) -> None:
    mentions = [
        {"merged_id": entry.edge.target_id, "canonical_id": entry.new_target_id}
        for entry in plan.entries
        if entry.edge.kind == "MENTIONS"
    ]
    # El primer entry de un mismo par (merged, canonico) mueve TODAS las
    # MENTIONS de esa entidad con sus propias propiedades; el resto seria no-op.
    mention_pairs = sorted({(e["merged_id"], e["canonical_id"]) for e in mentions})
    mentions_params = [{"merged_id": m, "canonical_id": c} for m, c in mention_pairs]

    related_repoint = [
        {
            "source_id": entry.edge.source_id,
            "relation_type": entry.edge.relation_type,
            "target_id": entry.edge.target_id,
            "new_source_id": entry.new_source_id,
            "new_target_id": entry.new_target_id,
        }
        for entry in plan.entries
        if entry.edge.kind == "RELATED" and entry.action is EdgeAction.REPOINT
    ]
    related_delete = [
        {
            "source_id": entry.edge.source_id,
            "relation_type": entry.edge.relation_type,
            "target_id": entry.edge.target_id,
        }
        for entry in plan.entries
        if entry.edge.kind == "RELATED" and entry.action is EdgeAction.DELETE_COLLAPSE
    ]

    print("\n== APLICANDO (DESTRUCTIVO) ==")
    for batch in _batched(mentions_params):
        await _run_write(session, _WRITE_MENTIONS, entries=batch)
    if mentions_params:
        print(f"  MENTIONS re-point: {len(mentions_params)} entidades -> canonico")
    for batch in _batched(related_repoint):
        await _run_write(session, _WRITE_RELATED_REPOINT, entries=batch)
    if related_repoint:
        print(f"  RELATED re-point: {len(related_repoint)} aristas")
    for batch in _batched(related_delete):
        await _run_write(session, _WRITE_RELATED_DELETE, entries=batch)
    if related_delete:
        print(f"  RELATED colapsadas (borradas): {len(related_delete)} aristas")
    total = len(mentions_params) + len(related_repoint) + len(related_delete)
    print(f"  operaciones planificadas: {total}")


async def _apply_break_cycles(session: AsyncSession, repairs: tuple[CycleRepair, ...]) -> None:
    """Limpia merged_into/merged_at SOLO en el ganador de cada ciclo (ids exactos)."""
    winners = [repair.winner for repair in repairs]
    print("\n== APLICANDO break-cycles (DESTRUCTIVO) ==")
    if not winners:
        print("  sin ciclos: nada que limpiar.")
        return
    for winner in winners:
        print(f"  limpiando marca: {winner}")
    for batch in _batched(winners):
        await _run_write(session, _WRITE_CLEAR_WINNER, ids=batch)
    print(
        f"  operacion: REMOVE merged_into/merged_at en {len(winners)} ganadores "
        "(los perdedores quedan marcados hacia ellos)"
    )


def _verify(pre: dict[str, Any], post: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if post["mentions_to_merged"] != 0:
        failures.append(f"MENTIONS a fusionadas = {post['mentions_to_merged']} (esperado 0)")
    if post["related_to_merged"] != 0:
        failures.append(f"RELATED a fusionadas = {post['related_to_merged']} (esperado 0)")
    if post["merged_entities"] != pre["merged_entities"]:
        failures.append(
            f"entidades merged = {post['merged_entities']} (esperado {pre['merged_entities']})"
        )
    if post["ledger_lines"] != pre["ledger_lines"]:
        failures.append(f"ledger = {post['ledger_lines']} lineas (esperado {pre['ledger_lines']})")
    return failures


def _verify_break_cycles(
    pre: dict[str, Any],
    post: dict[str, Any],
    *,
    expected_merged: int,
    still_marked: list[str],
) -> list[str]:
    """Invariantes del break-cycles: ganadores limpios, merged baja exacto, ledger intacto.

    Los conteos PENDIENTES (aristas a fusionadas) NO son invariante aqui: este
    modo no re-apunta aristas, asi que permanecen hasta ``--mode repoint``.
    """
    failures: list[str] = []
    for entity_id in still_marked:
        failures.append(f"el ganador {entity_id} todavia tiene merged_into")
    if post["merged_entities"] != expected_merged:
        failures.append(
            f"entidades merged = {post['merged_entities']} (esperado {expected_merged}: "
            f"bajada exacta de {pre['merged_entities'] - expected_merged} ganadores)"
        )
    if post["ledger_lines"] != pre["ledger_lines"]:
        failures.append(f"ledger = {post['ledger_lines']} lineas (esperado {pre['ledger_lines']})")
    return failures


async def main() -> None:
    args = _parse_args()
    settings = Settings.model_validate({})
    mode = "apply" if args.apply else "dry-run"
    prefix = f"{args.namespace}:" if args.namespace else ""
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    snapshot: Path = args.snapshot or SNAPSHOT_DIR / f"{SNAPSHOT_PREFIX}-{timestamp}.json"

    print(f"== MODO {'APPLY' if args.apply else 'DRY-RUN'} ==")
    print(f"  mode: {args.mode}")
    print(f"  namespace (filtro): {args.namespace or '(global)'}")

    driver = AsyncGraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value()),
    )
    try:
        async with driver.session() as session:
            merged: dict[str, str] = {}
            for entity_id, target in await _rows(session, _QUERY_MERGED):
                if entity_id and target:  # merged_into en blanco = entidad viva
                    merged[str(entity_id)] = str(target)
            targets = sorted(set(merged.values()))
            known_ids = {str(row[0]) for row in await _rows(session, _QUERY_KNOWN, ids=targets)}

            pre = await _pre_state(session)
            print("\n== ESTADO INICIAL (global) ==")
            print(f"  MENTIONS a fusionadas: {pre['mentions_to_merged']}")
            print(f"  RELATED a fusionadas:  {pre['related_to_merged']}")
            print(f"  entidades merged_into: {pre['merged_entities']}")
            print(f"  lineas de ledger:      {pre['ledger_lines'] or '(sin ledger)'}")

            edges: list[DanglingEdge] = []
            for row in await _rows(session, _READ_MENTIONS, prefix=prefix):
                edges.append(
                    DanglingEdge(
                        kind="MENTIONS",
                        source_id=_chunk_key(row),
                        target_id=str(row[3]),
                        properties=dict(row[4] or {}),
                    )
                )
            for row in await _rows(session, _READ_RELATED, prefix=prefix):
                edges.append(
                    DanglingEdge(
                        kind="RELATED",
                        source_id=str(row[0]),
                        target_id=str(row[2]),
                        relation_type=None if row[1] is None else str(row[1]),
                        properties=dict(row[3] or {}),
                    )
                )

            plan = plan_repoint(edges, merged_into=merged, known_ids=known_ids)
            _inventory(plan, merged)
            _print_anomalies(plan)
            _print_summary(edges, plan)

            repairs: tuple[CycleRepair, ...] = ()
            winner_marks: dict[str, Any] = {}
            if args.mode == "break-cycles":
                degree_mentions, degree_related = await _read_live_degrees(session)
                live_degree = {
                    entity_id: degree_mentions.get(entity_id, 0) + degree_related.get(entity_id, 0)
                    for entity_id in set(degree_mentions) | set(degree_related)
                }
                repairs = plan_cycle_repairs(merged, live_degree=live_degree)
                _print_cycles(repairs, mentions=degree_mentions, related=degree_related)
                winner_marks = {
                    str(row[0]): {"merged_into": row[1], "merged_at": row[2]}
                    for row in await _rows(
                        session, _QUERY_WINNER_MARKS, ids=[repair.winner for repair in repairs]
                    )
                }

            backup_info: dict[str, Any] | None = None
            if args.apply:
                backup_info = _validate_apply_gates(args)
                print(
                    f"  backup: {backup_info['path']} "
                    f"({backup_info['size_mb']} MB, {backup_info['age_hours']} h)"
                )

            counts = {
                "edges_read": len(edges),
                "repoint": plan.repoint_count,
                "delete_collapse": plan.collapse_count,
                "excluded": len(plan.excluded),
                "merge_keys": plan.merge_key_count,
                "merge_collapsed": plan.merge_collapsed,
            }
            bundle: dict[str, Any] = {
                "schema": BUNDLE_SCHEMA,
                "generated_at": datetime.now(UTC).isoformat(),
                "git_head": _git_head(),
                "mode": mode,
                "namespace_filter": args.namespace,
                "backup": backup_info,
                "approval": str(args.approval) if args.approval else None,
                "pre_state": pre,
                "plan": plan.model_dump(mode="json"),
                "counts": counts,
                "post_state": None,
            }
            if args.mode == "break-cycles":
                # Reparaciones + marcas previas de cada ganador: con esto el
                # REMOVE es reversible a mano (restaurar merged_into/merged_at).
                bundle["cycles"] = [
                    {
                        **repair.model_dump(mode="json"),
                        "winner_previous_mark": winner_marks.get(repair.winner),
                    }
                    for repair in repairs
                ]
            print()
            # El bundle se escribe SIEMPRE antes de cualquier mutacion.
            _write_bundle(snapshot, bundle)

            if not args.apply:
                print("\n== DRY-RUN: grafo sin mutar ==")
                if args.mode == "break-cycles":
                    print(
                        "  break-cycles NO re-apunta aristas: "
                        "--mode repoint limpia lo que quede tras romper los ciclos."
                    )
                print("  Para aplicar: --apply --backup <json> --approval <file con 'approve'>")
                return

            if args.mode == "break-cycles":
                await _apply_break_cycles(session, repairs)
                post = await _pre_state(session)
                bundle["post_state"] = post
                _write_bundle(snapshot, bundle)

                still_marked = [
                    str(row[0])
                    for row in await _rows(
                        session,
                        _QUERY_STILL_MARKED,
                        ids=[repair.winner for repair in repairs],
                    )
                ]
                expected_merged = pre["merged_entities"] - len(repairs)
                print("\n== VERIFICACION POST-APPLY (break-cycles) ==")
                print(
                    f"  ganadores sin merged_into: "
                    f"{len(repairs) - len(still_marked)}/{len(repairs)}"
                )
                print(
                    f"  entidades merged_into: {post['merged_entities']} "
                    f"(esperado {expected_merged} = inicial - {len(repairs)} ganadores)"
                )
                print(
                    f"  lineas de ledger:      {post['ledger_lines'] or '(sin ledger)'} "
                    f"(esperado {pre['ledger_lines'] or '(sin ledger)'})"
                )
                print(
                    f"PENDIENTES: MENTIONS={post['mentions_to_merged']} "
                    f"RELATED={post['related_to_merged']}"
                )
                print(
                    "  --mode repoint limpia lo que quede: re-apunta esas aristas "
                    "al canonico ganador ahora vivo."
                )
                failures = _verify_break_cycles(
                    pre, post, expected_merged=expected_merged, still_marked=still_marked
                )
                if failures:
                    for failure in failures:
                        print(f"VERIFICACION FAILED: {failure}")
                    sys.exit(1)
                print("APPLY_OK")
                return

            await _apply_plan(session, plan)

            post = await _pre_state(session)
            bundle["post_state"] = post
            _write_bundle(snapshot, bundle)

            print("\n== VERIFICACION POST-APPLY ==")
            print(f"  MENTIONS a fusionadas: {post['mentions_to_merged']} (esperado 0)")
            print(f"  RELATED a fusionadas:  {post['related_to_merged']} (esperado 0)")
            print(
                f"  entidades merged_into: {post['merged_entities']} "
                f"(esperado {pre['merged_entities']})"
            )
            print(
                f"  lineas de ledger:      {post['ledger_lines'] or '(sin ledger)'} "
                f"(esperado {pre['ledger_lines'] or '(sin ledger)'})"
            )
            failures = _verify(pre, post)
            if failures:
                for failure in failures:
                    print(f"VERIFICACION FAILED: {failure}")
                sys.exit(1)
            print("APPLY_OK")
    finally:
        await driver.close()


if __name__ == "__main__":
    asyncio.run(main())
