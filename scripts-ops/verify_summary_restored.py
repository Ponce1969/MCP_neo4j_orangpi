import asyncio

from neo4j import AsyncGraphDatabase

from book_graph_rag.config import Settings

s = Settings.model_validate({})


async def main() -> None:
    drv = AsyncGraphDatabase.driver(
        s.neo4j_uri, auth=(s.neo4j_user, s.neo4j_password.get_secret_value())
    )
    async with drv.session() as ses:
        print("== Summary del Essential (caps 4/6/8) ==")
        recs = await (await ses.run(
            "MATCH (b:Book {id: 'knowledge:essential-graphrag'})-[:CONTAINS]->(ch:Chapter) "
            "MATCH (ch)-[:HAS_SECTION|HAS_SUBSECTION*1..]->(s:Section) "
            "WHERE s.title = 'Summary' "
            "RETURN ch.number AS cn, ch.title AS ct, s.page_start AS ps "
            "ORDER BY cn"
        )).values()
        for r in recs:
            print(f"  cn={r[0]} {str(r[1])[:50]!r} ps={r[2]}")
        print("== Secciones pisadas con ps>178 bajo Essential ==")
        rec = await (await ses.run(
            "MATCH (b:Book {id: 'knowledge:essential-graphrag'})-[:CONTAINS]->(:Chapter)"
            "-[:HAS_SECTION|HAS_SUBSECTION*1..]->(s:Section) "
            "WHERE s.page_start > 178 RETURN count(s) AS c"
        )).single()
        assert rec is not None
        print("  count:", rec[0])
        print("== Secciones sin parent ==")
        rec = await (await ses.run(
            "MATCH (s:Section) WHERE NOT EXISTS { MATCH ()-[:HAS_SECTION|HAS_SUBSECTION]->(s) } "
            "RETURN count(s) AS c"
        )).single()
        assert rec is not None
        print("  count:", rec[0])
        print("== Secciones compartidas con >1 parent ==")
        rec = await (await ses.run(
            "MATCH (s:Section) WITH s, size([(p)-[:HAS_SECTION|HAS_SUBSECTION]->(s) | p]) AS np "
            "WHERE np > 1 RETURN count(s) AS c"
        )).single()
        assert rec is not None
        print("  count:", rec[0])
    await drv.close()


asyncio.run(main())
