"""Limpieza SCOPED de un namespace de conocimiento (rebuild de un libro).

Contexto: un libro indexado con un PDF sin TOC (o con un PDF parcial) deja el
namespace sin jerarquia editorial y con provenance incompleta. Para
reconstruirlo con el PDF correcto hay que borrar SOLO ese namespace y volver a
indexar; un ``--fresh`` global no es opcion (borra los otros libros).

Fases:
  Fase 1 (default, READ-ONLY): reporta exactamente que se borraria.
  Fase 2 (--apply): borra el namespace. Es MUTACION DESTRUCTIVA y exige:
    - ``--backup <json>``  backup fresco ya hecho (scripts-ops/backup_only.py).
      Se rechaza si tiene mas de 24 h salvo ``--allow-stale-backup``.
    - ``--approval <file>`` cuyo contenido incluya la palabra "approve"
      (mismo contrato que ``--backfill-checkpoints``, AGENTS.md 7.1).

Alcance exacto (nada fuera del namespace):
  Checkpoint  {source_id: <source_id>}
  Chunk       {book_id: <source_id>} OR {source_id: <source_id>}
  Entity      id STARTS WITH "<source_id>:"
  Section     {book_id: <source_id>}
  Chapter     {book_id: <source_id>}
  Book        {id: <source_id>}
Los MENTIONS/RELATED incidentes caen con el DETACH DELETE de sus nodos.
Las :CommunitySummary son GLOBALES (no tienen namespace): se reportan y no se
tocan; se reconstruyen en la cola post-index.

Uso (desde la raiz del repo en el OrangePi, con el .env sourceado):
    uv run --no-sync python scripts-ops/cleanup_namespace.py
    uv run --no-sync python scripts-ops/cleanup_namespace.py --apply \\
        --backup ~/backups_neo4j/bookgraph_backup_YYYYMMDDTHHMMSSZ.json \\
        --approval /tmp/approve_huyen.txt
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

DEFAULT_SOURCE_ID = "knowledge:ai-engineering-huyen"
STALE_BACKUP_HOURS = 24
APPROVAL_WORD = "approve"

_COUNTS: tuple[tuple[str, str], ...] = (
    ("checkpoints", "MATCH (c:Checkpoint {source_id: $sid}) RETURN count(c) AS c"),
    (
        "chunks",
        "MATCH (k:Chunk) WHERE k.book_id = $sid OR k.source_id = $sid RETURN count(k) AS c",
    ),
    ("entities", "MATCH (e:Entity) WHERE e.id STARTS WITH $prefix RETURN count(e) AS c"),
    (
        "related-edges (con >=1 punta en el namespace)",
        "MATCH (a:Entity)-[r:RELATED]->(b:Entity) "
        "WHERE a.id STARTS WITH $prefix OR b.id STARTS WITH $prefix RETURN count(r) AS c",
    ),
    ("chapters", "MATCH (ch:Chapter {book_id: $sid}) RETURN count(ch) AS c"),
    ("sections", "MATCH (s:Section {book_id: $sid}) RETURN count(s) AS c"),
    ("book", "MATCH (b:Book {id: $sid}) RETURN count(b) AS c"),
)

_DELETIONS: tuple[tuple[str, str], ...] = (
    (
        "chunks",
        "MATCH (k:Chunk) WHERE k.book_id = $sid OR k.source_id = $sid DETACH DELETE k",
    ),
    ("entities", "MATCH (e:Entity) WHERE e.id STARTS WITH $prefix DETACH DELETE e"),
    ("checkpoints", "MATCH (c:Checkpoint {source_id: $sid}) DETACH DELETE c"),
    ("sections", "MATCH (s:Section {book_id: $sid}) DETACH DELETE s"),
    ("chapters", "MATCH (ch:Chapter {book_id: $sid}) DETACH DELETE ch"),
    ("book", "MATCH (b:Book {id: $sid}) DETACH DELETE b"),
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=DEFAULT_SOURCE_ID, help="source_id del namespace")
    parser.add_argument("--apply", action="store_true", help="ejecuta el borrado (DESTRUCTIVO)")
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


async def _report(session: AsyncSession, source_id: str, prefix: str, title: str) -> None:
    print(f"\n== {title} ==")
    for label, query in _COUNTS:
        value = await _scalar(session, query, sid=source_id, prefix=prefix)
        print(f"  {label}: {value}")


def _validate_apply_gates(args: argparse.Namespace) -> None:
    if args.approval is None or not args.approval.exists():
        sys.exit("APPLY abortado: falta --approval <archivo existente>")
    content = args.approval.read_text(encoding="utf-8").strip().lower()
    if APPROVAL_WORD not in content:
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
    prefix = f"{source_id}:"
    settings = Settings.model_validate({})
    mode = "APPLY" if args.apply else "DRY-RUN"

    print(f"== MODO {mode} ==")
    print(f"  namespace: {source_id}")
    print(f"  prefijo de entidades: {prefix}")

    driver = AsyncGraphDatabase.driver(
        settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value())
    )
    try:
        async with driver.session() as session:
            await _report(session, source_id, prefix, "Estado ANTES")

            processing = await _scalar(
                session,
                "MATCH (c:Checkpoint {source_id: $sid}) WHERE c.status = 'PROCESSING' "
                "RETURN count(c) AS c",
                sid=source_id,
            )
            dangling_merges = await _scalar(
                session,
                "MATCH (e:Entity) WHERE e.merged_into STARTS WITH $prefix RETURN count(e) AS c",
                prefix=prefix,
            )
            summaries = await _scalar(session, "MATCH (c:CommunitySummary) RETURN count(c) AS c")
            books_query = "MATCH (b:Book) RETURN b.id AS id ORDER BY id"
            books = await (await session.run(books_query)).values()
            print(f"  checkpoints PROCESSING (indice corriendo?): {processing}")
            print(f"  entidades de OTROS namespaces mergeadas a este: {dangling_merges}")
            print(f"  :CommunitySummary globales (NO se tocan): {summaries}")
            print(f"  Books hoy: {[row[0] for row in books]}")

            if not args.apply:
                print("\n== DRY-RUN: nada mutado ==")
                print("  Para aplicar: --apply --backup <json> --approval <file con 'approve'>")
                return

            if processing:
                sys.exit(
                    f"APPLY abortado: hay {processing} checkpoints PROCESSING en {source_id} "
                    "(index en curso). Esperar a que termine."
                )
            _validate_apply_gates(args)

            print("\n== BORRANDO (scoped) ==")
            for label, query in _DELETIONS:
                await session.run(query, sid=source_id, prefix=prefix)
                print(f"  {label}: borrados")

            await _report(session, source_id, prefix, "Estado DESPUES (esperado: todo 0)")
            remaining = await (await session.run(books_query)).values()
            print(f"  Books restantes: {[row[0] for row in remaining]}")
            print("APPLY_OK")
    finally:
        await driver.close()


if __name__ == "__main__":
    asyncio.run(main())
