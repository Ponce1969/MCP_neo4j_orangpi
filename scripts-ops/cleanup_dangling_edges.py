"""Limpieza SCOPED de aristas basura en el grafo de conocimiento.

Dos clases de arista, ambas reportadas por el mantenimiento de resolucion y
ninguna detectada por el audit:

1. **RELATED a un duplicado soft-deleted**: el adapter de merge re-apunta con
   ``MERGE (canon)-[r2:RELATED]->(other)`` sin excluir a los otros miembros del
   mismo grupo, asi que una arista intra-grupo queda apuntando a un nodo con
   ``merged_into``. El alcance se limita con ``--namespace`` (por defecto el
   libro 4) para no tocar deuda vieja de otros libros.
2. **Self-loops** ``(:Entity)-[:RELATED]->(:Entity)``: basura de extraccion (el
   LLM se autorreferencia). Se borran en TODO el grafo.

Fases:
  Fase 1 (default, READ-ONLY): cuenta y muestra ambas clases.
  Fase 2 (--apply): borra. Es MUTACION DESTRUCTIVA y exige:
    - ``--backup <json>``  backup fresco ya hecho (scripts-ops/backup_only.py).
      Se rechaza si tiene mas de 24 h salvo ``--allow-stale-backup``.
    - ``--approval <file>`` cuyo contenido incluya la palabra "approve".

Solo se borran RELACIONES, nunca nodos: el ``merged_into`` de los duplicados y su
entrada en el ledger quedan intactos, asi que un rollback del merge sigue siendo
posible.

Uso (desde la raiz del repo en el OrangePi, con el .env sourceado):
    uv run --no-sync python scripts-ops/cleanup_dangling_edges.py
    uv run --no-sync python scripts-ops/cleanup_dangling_edges.py --apply \\
        --backup ~/backups_neo4j/bookgraph_backup_YYYYMMDDTHHMMSSZ.json \\
        --approval /tmp/approve_edges.txt
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from neo4j import AsyncGraphDatabase, AsyncSession

from book_graph_rag.config import Settings

DEFAULT_NAMESPACE = "knowledge:ai-engineering-huyen"
STALE_BACKUP_HOURS = 24
APPROVAL_WORD = "approve"
SAMPLE_LIMIT = 12

_DANGLING_SCOPE = """
(a.merged_into IS NOT NULL OR b.merged_into IS NOT NULL)
  AND (a.id STARTS WITH $prefix OR b.id STARTS WITH $prefix)
"""

_COUNT_DANGLING = (
    f"MATCH (a:Entity)-[r:RELATED]->(b:Entity) WHERE {_DANGLING_SCOPE} RETURN count(r) AS c"
)
_SAMPLE_DANGLING = (
    "MATCH (a:Entity)-[r:RELATED]->(b:Entity) "
    f"WHERE {_DANGLING_SCOPE} "
    "RETURN a.id AS source, r.type AS kind, b.id AS target, "
    "coalesce(a.merged_into, b.merged_into) AS merged_into "
    f"LIMIT {SAMPLE_LIMIT}"
)
_DELETE_DANGLING = f"MATCH (a:Entity)-[r:RELATED]->(b:Entity) WHERE {_DANGLING_SCOPE} DELETE r"
_COUNT_DANGLING_GLOBAL = (
    "MATCH (a:Entity)-[r:RELATED]->(b:Entity) "
    "WHERE a.merged_into IS NOT NULL OR b.merged_into IS NOT NULL RETURN count(r) AS c"
)
_COUNT_SELF_LOOPS = "MATCH (a:Entity)-[r:RELATED]->(b) WHERE a = b RETURN count(r) AS c"
_SAMPLE_SELF_LOOPS = (
    "MATCH (a:Entity)-[r:RELATED]->(b) WHERE a = b "
    "RETURN a.id AS id, r.type AS kind, coalesce(a.merged_into, '') AS merged_into "
    f"LIMIT {SAMPLE_LIMIT}"
)
_DELETE_SELF_LOOPS = "MATCH (a:Entity)-[r:RELATED]->(b) WHERE a = b DELETE r"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", default=DEFAULT_NAMESPACE, help="namespace del alcance")
    parser.add_argument(
        "--only",
        choices=("both", "dangling", "self-loops"),
        default="both",
        help="que clase de arista limpiar (default: both)",
    )
    parser.add_argument("--apply", action="store_true", help="borra las aristas (DESTRUCTIVO)")
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


async def _scalar(session: AsyncSession, query: str, **params: Any) -> int:
    record = await (await session.run(query, **params)).single()
    return int(record[0]) if record is not None else 0


async def _rows(session: AsyncSession, query: str, **params: Any) -> list[list[Any]]:
    return await (await session.run(query, **params)).values()


async def _run_write(session: AsyncSession, query: str, **params: Any) -> None:
    result = await session.run(query, **params)
    await result.consume()


def _print_dangling_samples(rows: list[list[Any]]) -> None:
    for source, kind, target, merged_into in rows:
        print(f"    {source} -[{kind}]-> {target}   (soft-deleted: {merged_into})")


def _print_self_loop_samples(rows: list[list[Any]]) -> None:
    for entity_id, kind, merged_into in rows:
        marker = " [entidad fusionada]" if merged_into else ""
        print(f"    {entity_id} -[{kind}]-> si mismo{marker}")


async def main() -> None:
    args = _parse_args()
    namespace: str = args.namespace
    prefix = f"{namespace}:"
    settings = Settings.model_validate({})
    mode = "APPLY" if args.apply else "DRY-RUN"
    do_dangling = args.only in ("both", "dangling")
    do_self_loops = args.only in ("both", "self-loops")

    print(f"== MODO {mode} ==")
    print(f"  namespace (aristas colgantes): {namespace}")
    print(f"  alcance: {'dangling + self-loops' if args.only == 'both' else args.only}")

    driver = AsyncGraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value()),
    )
    try:
        async with driver.session() as session:
            self_loops = await _scalar(session, _COUNT_SELF_LOOPS)
            dangling = await _scalar(session, _COUNT_DANGLING, prefix=prefix)
            dangling_global = await _scalar(session, _COUNT_DANGLING_GLOBAL)

            print("\n== ANTES ==")
            print(f"  self-loops (todo el grafo): {self_loops}")
            print(f"  RELATED a soft-deleted en {namespace}: {dangling}")
            print(f"  RELATED a soft-deleted en TODO el grafo: {dangling_global}")
            if do_self_loops and self_loops:
                print("  muestras self-loops:")
                _print_self_loop_samples(await _rows(session, _SAMPLE_SELF_LOOPS))
            if do_dangling and dangling:
                print("  muestras colgantes:")
                _print_dangling_samples(await _rows(session, _SAMPLE_DANGLING, prefix=prefix))

            if not args.apply:
                print("\n== DRY-RUN: nada mutado ==")
                print("  Para aplicar: --apply --backup <json> --approval <file con 'approve'>")
                return
            _validate_apply_gates(args)

            print("\n== BORRANDO ==")
            if do_self_loops:
                await _run_write(session, _DELETE_SELF_LOOPS)
                print("  self-loops: borrados")
            if do_dangling:
                await _run_write(session, _DELETE_DANGLING, prefix=prefix)
                print(f"  RELATED a soft-deleted en {namespace}: borradas")

            print("\n== DESPUES ==")
            print(f"  self-loops (todo el grafo): {await _scalar(session, _COUNT_SELF_LOOPS)}")
            print(
                f"  RELATED a soft-deleted en {namespace}: "
                f"{await _scalar(session, _COUNT_DANGLING, prefix=prefix)}"
            )
            print(
                "  RELATED a soft-deleted en TODO el grafo (deuda ajena restante): "
                f"{await _scalar(session, _COUNT_DANGLING_GLOBAL)}"
            )
            print("APPLY_OK")
    finally:
        await driver.close()


if __name__ == "__main__":
    asyncio.run(main())
