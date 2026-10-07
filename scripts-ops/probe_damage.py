import asyncio
from typing import Any

from neo4j import AsyncGraphDatabase

from book_graph_rag.config import Settings

s = Settings.model_validate({})


async def main() -> None:
    drv = AsyncGraphDatabase.driver(
        s.neo4j_uri, auth=(s.neo4j_user, s.neo4j_password.get_secret_value())
    )
    async with drv.session() as ses:
        print("== Secciones con MAS DE UN padre Chapter/Section (contaminadas/shared) ==")
        rec = await (
            await ses.run(
                "MATCH (s:Section) WITH s, size(["
                "(p)-[:HAS_SECTION|HAS_SUBSECTION]->(s) | p]) AS np "
                "WHERE np > 1 RETURN count(s) AS c"
            )
        ).single()
        assert rec is not None
        print("  sections con >1 parent:", rec[0])
        print(
            "== Secciones bajo capítulos del Essential con page_start > 178 "
            "(pisadas por Alcaraz) =="
        )
        recs = await (
            await ses.run(
                "MATCH (b:Book {id: 'knowledge:essential-graphrag'})-[:CONTAINS]->(ch:Chapter) "
                "MATCH (ch)-[:HAS_SECTION|HAS_SUBSECTION*1..]->(s:Section) "
                "WHERE s.page_start > 178 OR s.page_start < 1 "
                "RETURN ch.number AS cn, ch.title AS ct, s.title AS st, "
                "s.page_start AS ps, s.level AS lv ORDER BY cn, ps LIMIT 40"
            )
        ).values()
        for r in recs:
            print(f"  cn={r[0]} ch={r[1]!r} sec={r[2]!r} ps={r[3]} lv={r[4]}")
        print("== Secciones pisadas también a MENOR rango (Summary cap3 etc.) ==")
        recs = await (
            await ses.run(
                "MATCH (b:Book {id: 'knowledge:essential-graphrag'})-[:CONTAINS]->(ch:Chapter) "
                "MATCH (ch)-[:HAS_SECTION|HAS_SUBSECTION*1..]->(s:Section) "
                "RETURN ch.number AS cn, ch.title AS ct, s.title AS st, s.page_start AS ps "
                "ORDER BY cn, ps"
            )
        ).values()
        by_chapter: dict[tuple[Any, Any], list[tuple[Any, Any]]] = {}
        for r in recs:
            key = (r[0], r[1])
            by_chapter.setdefault(key, []).append((r[2], r[3]))
        for (cn, ct), secs in by_chapter.items():
            print(f"  cn={cn} {ct[:40]!r} -> {[(st[:22], ps) for st, ps in secs][:8]}")
    await drv.close()


asyncio.run(main())
