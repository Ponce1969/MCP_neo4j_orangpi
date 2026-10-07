"""Deshacer el namespace knowledge:graphrag-agentic del piloto c3468.

Borrado en dos fases:
  Fase 1 (dry-run, read-only): lista lo que se borraría.
  Fase 2 (apply): borra chunks, entidades, book+chapters del namespace, y
  después las secciones que quedaron SIN parent (las del piloto no compartidas;
  las compartidas con Essential conservan su HAS_SECTION y su page_start será
  restaurado por el restore del backup pre-index).

ADVERTENCIA: la Fase 2 es mutación destructiva. Requiere backup previo ya
hecho (bookgraph_backup_20260925T020527Z.json) y aprobación humana.
"""

import asyncio
import sys
from typing import Any

from neo4j import AsyncGraphDatabase, AsyncSession

from book_graph_rag.config import Settings

s = Settings.model_validate({})
PREFIX = "knowledge:graphrag-agentic"
BOOK_ID = "knowledge:graphrag-agentic"
APPLY = "--apply" in sys.argv


async def count_and_report(
    ses: AsyncSession,
    label: str,
    query: str,
    params: dict[str, Any] | None = None,
) -> int:
    rec = await (await ses.run(query, params or {})).single()
    n = rec[0] if rec else 0
    print(f"  {label}: {n}")
    return n


async def main() -> None:
    drv = AsyncGraphDatabase.driver(
        s.neo4j_uri, auth=(s.neo4j_user, s.neo4j_password.get_secret_value())
    )
    mode = "APPLY" if APPLY else "DRY-RUN"
    print(f"== MODO {mode} ==")
    async with drv.session() as ses:
        # 1. Checkpoints
        await count_and_report(
            ses,
            "checkpoints",
            "MATCH (c:Checkpoint) WHERE c.source_id CONTAINS $p RETURN count(c) AS c",
            {"p": PREFIX},
        )
        # 2. Chunks
        await count_and_report(
            ses,
            "chunks",
            "MATCH (k:Chunk) WHERE k.book_id = $bid OR k.source_id CONTAINS $p "
            "RETURN count(k) AS c",
            {"bid": BOOK_ID, "p": PREFIX},
        )
        # 3. Entities
        await count_and_report(
            ses,
            "entities",
            "MATCH (e:Entity) WHERE e.id CONTAINS $p RETURN count(e) AS c",
            {"p": PREFIX},
        )
        # 4. Book + chapters del namespace
        await count_and_report(
            ses, "book", "MATCH (b:Book {id: $id}) RETURN count(b) AS c", {"id": BOOK_ID}
        )
        await count_and_report(
            ses,
            "chapters-del-book",
            "MATCH (b:Book {id: $id})-[:CONTAINS]->(ch:Chapter) RETURN count(ch) AS c",
            {"id": BOOK_ID},
        )

        print("== Antes de borrar chapters: secciones con parent en los chapters del Alcaraz ==")
        recs = await (
            await ses.run(
                "MATCH (b:Book {id: $id})-[:CONTAINS]->(ch:Chapter)"
                "-[:HAS_SECTION|HAS_SUBSECTION*1..]->(sc:Section) "
                "RETURN ch.number AS cn, ch.title AS ct, sc.title AS st, "
                "sc.page_start AS ps ORDER BY cn, ps",
                id=BOOK_ID,
            )
        ).values()
        print(f"  total secciones alcanzables desde chapters del Alcaraz: {len(recs)}")
        for r in recs[:60]:
            print(f"    cn={r[0]} ch={str(r[1])[:45]!r} sec={r[2]!r} ps={r[3]}")

        if not APPLY:
            print("== DRY-RUN: nada mutado. Ejecutar con --apply tras aprobacion. ==")
            await drv.close()
            return

        # --- MUTACIONES ---
        await ses.run(
            "MATCH (c:Checkpoint) WHERE c.source_id CONTAINS $p DETACH DELETE c", p=PREFIX
        )
        await ses.run(
            "MATCH (k:Chunk) WHERE k.book_id = $bid OR k.source_id CONTAINS $p DETACH DELETE k",
            bid=BOOK_ID,
            p=PREFIX,
        )
        await ses.run("MATCH (e:Entity) WHERE e.id CONTAINS $p DETACH DELETE e", p=PREFIX)
        await ses.run(
            "MATCH (b:Book {id: $id}) "
            "OPTIONAL MATCH (b)-[:CONTAINS]->(ch:Chapter) "
            "DETACH DELETE ch, b",
            id=BOOK_ID,
        )

        # 5. Secciones huérfanas post-borrado = las del piloto no compartidas.
        #    (id no existe en secciones; se detectan por ausencia de parent.)
        rec = await (
            await ses.run(
                "MATCH (sc:Section) WHERE NOT EXISTS { "
                "MATCH ()-[:HAS_SECTION|HAS_SUBSECTION]->(sc) } "
                "RETURN count(sc) AS c"
            )
        ).single()
        assert rec is not None
        total_orphan = rec[0]
        print(f"  secciones sin parent tras borrado: {total_orphan}")
        recs = await (
            await ses.run(
                "MATCH (sc:Section) WHERE NOT EXISTS { "
                "MATCH ()-[:HAS_SECTION|HAS_SUBSECTION]->(sc) } "
                "RETURN sc.title AS t, sc.chapter_number AS cn, sc.page_start AS ps ORDER BY cn, ps"
            )
        ).values()
        for r in recs[:60]:
            print(f"    ORPHAN sec={r[0]!r} cn={r[1]} ps={r[2]}")
        await ses.run(
            "MATCH (sc:Section) WHERE NOT EXISTS { MATCH ()-[:HAS_SECTION|HAS_SUBSECTION]->(sc) } "
            "DETACH DELETE sc"
        )

        # 6. Sanity final
        print("== Conteos finales ==")
        await count_and_report(
            ses,
            "chunks restantes",
            "MATCH (k:Chunk) WHERE k.book_id = $bid OR k.source_id CONTAINS $p "
            "RETURN count(k) AS c",
            {"bid": BOOK_ID, "p": PREFIX},
        )
        await count_and_report(
            ses,
            "entities restantes",
            "MATCH (e:Entity) WHERE e.id CONTAINS $p RETURN count(e) AS c",
            {"p": PREFIX},
        )
        await count_and_report(
            ses, "book restante", "MATCH (b:Book {id: $id}) RETURN count(b) AS c", {"id": BOOK_ID}
        )
        await count_and_report(
            ses,
            "sections sin parent (post-limpieza)",
            "MATCH (sc:Section) WHERE NOT EXISTS { MATCH ()-[:HAS_SECTION|HAS_SUBSECTION]->(sc) } "
            "RETURN count(sc) AS c",
        )
        print("APPLY_OK")
    await drv.close()


asyncio.run(main())
