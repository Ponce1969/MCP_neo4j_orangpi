"""Completar ``:Chunk.source_id`` en libros indexados antes de que existiera.

Problema: los chunks del Libro 1 (``knowledge:agentic-architectural-patterns``)
se escribieron con ``MERGE (k:Chunk {chunk_index, book_id})``, sin
``source_id``.  El backfill de checkpoints matchea
``MATCH (k:Chunk {source_id: $source_id})`` -> 0 candidatos, y el path
resumable de ``index`` MERGEa por ``(source_id, chunk_index)``, asi que un
re-index crearia nodos duplicados.  Este script completa el dato faltante sin
tocar nada mas:

    SET k.source_id = k.book_id   WHERE k.source_id IS NULL AND k.book_id = <source>

Es idempotente (solo filas con ``source_id IS NULL``) y no borra nada.

Fases:
  Fase 1 (default, READ-ONLY): reporta el desglose por libro y lo que cambiaria.
  Fase 2 (--apply): escribe. Exige ``--backup <json>`` fresco (<= 24 h, igual
  que el cleanup de namespace) y ``--approval <file>`` con la palabra
  "approve" (AGENTS.md 7.1).

Uso (desde la raiz del repo en el OrangePi, con el .env sourceado):
    uv run --no-sync python scripts-ops/backfill_chunk_source_id.py
    uv run --no-sync python scripts-ops/backfill_chunk_source_id.py --apply \\
        --backup ~/backups_neo4j/bookgraph_backup_YYYYMMDDTHHMMSSZ.json \\
        --approval /tmp/approve_source_id.txt
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

DEFAULT_SOURCE_ID = "knowledge:agentic-architectural-patterns"
STALE_BACKUP_HOURS = 24
APPROVAL_WORD = "approve"

_BY_BOOK = (
    "MATCH (k:Chunk) WHERE k.source_id IS NULL "
    "RETURN coalesce(k.book_id, '<sin book_id>') AS book, count(k) AS c "
    "ORDER BY c DESC"
)

_TARGET = "MATCH (k:Chunk) WHERE k.source_id IS NULL AND k.book_id = $sid RETURN count(k) AS c"

_UPDATE = (
    "MATCH (k:Chunk) WHERE k.source_id IS NULL AND k.book_id = $sid SET k.source_id = k.book_id"
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", default=DEFAULT_SOURCE_ID, help="source_id / book_id a completar"
    )
    parser.add_argument("--apply", action="store_true", help="ejecuta la escritura")
    parser.add_argument(
        "--backup", type=Path, help="backup fresco ya hecho (obligatorio con --apply)"
    )
    parser.add_argument("--approval", type=Path, help="archivo con la palabra 'approve'")
    parser.add_argument(
        "--allow-stale-backup",
        action="store_true",
        help=f"permite un backup de mas de {STALE_BACKUP_HOURS} h",
    )
    return parser.parse_args()


async def _scalar(session: AsyncSession, query: str, **params: Any) -> int:
    record = await (await session.run(query, **params)).single()
    return int(record[0]) if record is not None else 0


def _validate_apply_gates(args: argparse.Namespace) -> None:
    if args.approval is None or not args.approval.exists():
        sys.exit("APPLY abortado: falta --approval <archivo existente>")
    if APPROVAL_WORD not in args.approval.read_text(encoding="utf-8").strip().lower():
        sys.exit(f"APPLY abortado: {args.approval} debe contener la palabra '{APPROVAL_WORD}'")

    if args.backup is None or not args.backup.exists():
        sys.exit("APPLY abortado: falta --backup <json de backup fresco ya hecho>")
    age_hours = (datetime.now(UTC).timestamp() - args.backup.stat().st_mtime) / 3600
    print(f"  backup: {args.backup} ({args.backup.stat().st_size / 1e6:.1f} MB, {age_hours:.1f} h)")
    if age_hours > STALE_BACKUP_HOURS and not args.allow_stale_backup:
        sys.exit(
            f"APPLY abortado: el backup tiene {age_hours:.1f} h (> {STALE_BACKUP_HOURS} h). "
            "Hacer uno fresco con scripts-ops/backup_only.py o pasar --allow-stale-backup."
        )


async def main() -> None:
    args = _parse_args()
    source_id: str = args.source
    settings = Settings.model_validate({})
    mode = "APPLY" if args.apply else "DRY-RUN"

    print(f"== MODO {mode} ==")
    print(f"  source_id a completar: {source_id}")

    driver = AsyncGraphDatabase.driver(
        settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value())
    )
    try:
        async with driver.session() as session:
            print("\n== Chunks con source_id NULL, por libro ==")
            for row in await (await session.run(_BY_BOOK)).values():
                print(f"  {row[0]}: {row[1]}")

            candidates = await _scalar(session, _TARGET, sid=source_id)
            total_chunks = await _scalar(session, "MATCH (k:Chunk) RETURN count(k) AS c")
            with_sid = await _scalar(
                session,
                "MATCH (k:Chunk {source_id: $sid}) RETURN count(k) AS c",
                sid=source_id,
            )
            print(f"\n  chunks totales en el grafo: {total_chunks}")
            print(f"  chunks de {source_id} con source_id: {with_sid}")
            print(f"  chunks de {source_id} a completar (source_id NULL): {candidates}")

            sample = await (
                await session.run(
                    "MATCH (k:Chunk) WHERE k.source_id IS NULL AND k.book_id = $sid "
                    "RETURN k.chunk_index AS ci, k.page_start AS ps ORDER BY k.chunk_index LIMIT 5",
                    sid=source_id,
                )
            ).values()
            for row in sample:
                print(f"    muestra: chunk_index={row[0]} page_start={row[1]}")

            if not args.apply:
                print("\n== DRY-RUN: nada mutado ==")
                print("  Para aplicar: --apply --backup <json> --approval <file con 'approve'>")
                return

            _validate_apply_gates(args)
            await session.run(_UPDATE, sid=source_id)
            after = await _scalar(session, _TARGET, sid=source_id)
            now_with_sid = await _scalar(
                session,
                "MATCH (k:Chunk {source_id: $sid}) RETURN count(k) AS c",
                sid=source_id,
            )
            print("\n== RESULTADO ==")
            print(f"  pendientes despues: {after} (esperado 0)")
            print(f"  chunks de {source_id} con source_id: {now_with_sid}")
            print("APPLY_OK")
    finally:
        await driver.close()


if __name__ == "__main__":
    asyncio.run(main())
