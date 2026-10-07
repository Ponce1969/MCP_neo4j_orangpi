import asyncio

from neo4j import AsyncGraphDatabase

from book_graph_rag.config import Settings

s = Settings.model_validate({})


async def main() -> None:
    drv = AsyncGraphDatabase.driver(
        s.neo4j_uri, auth=(s.neo4j_user, s.neo4j_password.get_secret_value())
    )
    async with drv.session() as ses:
        print("== Chapters por libro ==")
        recs = await (
            await ses.run(
                "MATCH (b:Book)-[:CONTAINS]->(c:Chapter) "
                "RETURN b.id AS bid, c.id AS cid, c.chapter_number AS cn, c.page_start AS ps "
                "ORDER BY bid, cn"
            )
        ).values()
        for r in recs:
            print(f"  bid={r[0]} cid={r[1]!r} cn={r[2]} ps={r[3]}")
        print("== Section ps=179 cn=4: padres reales ==")
        recs = await (
            await ses.run(
                "MATCH (n:Section) WHERE n.page_start = 179 AND n.chapter_number = 4 "
                "MATCH (p)-[r:HAS_SECTION|HAS_SUBSECTION]->(n) "
                "RETURN labels(p) AS pl, p.id AS pid, p.chapter_number AS pcn, "
                "p.title AS pt, type(r) AS rt "
            )
        ).values()
        for r in recs:
            print(f"  parent={r[0]} id={r[1]!r} pcn={r[2]} title={r[3]!r} rel={r[4]}")
        print("== Chapters del graphrag-agentic: con cuántas sections ==")
        recs = await (
            await ses.run(
                "MATCH (b:Book {id: 'knowledge:graphrag-agentic'})-[:CONTAINS]->(c:Chapter) "
                "OPTIONAL MATCH (c)-[:HAS_SECTION|HAS_SUBSECTION*1..]->(s:Section) "
                "RETURN c.id AS cid, c.chapter_number AS cn, count(s) AS ns "
                "ORDER BY cn"
            )
        ).values()
        for r in recs:
            print(f"  cid={r[0]!r} cn={r[1]} sections={r[2]}")
        print("== Sections sin parent ==")
        rec = await (
            await ses.run(
                "MATCH (n:Section) WHERE NOT (()-[r:HAS_SECTION|HAS_SUBSECTION]->(n)) "
                "RETURN count(n) AS c"
            )
        ).single()
        assert rec is not None
        print("  sections sin parent:", rec[0])
        print("== Sections del nuevo libro vs essential: page_start range ==")
        for _bid in ("knowledge:essential-graphrag", "knowledge:graphrag-agentic"):
            rec = await (
                await ses.run(
                    "MATCH (c:Chapter)-[:HAS_SECTION|HAS_SUBSECTION*1..]->(s:Section) "
                    "WHERE c.chapter_number = 4 RETURN min(s.page_start) AS mn, "
                    "max(s.page_start) AS mx, count(s) AS c"
                )
            ).single()
            assert rec is not None
            print(f"  cn=4 global: min={rec[0]} max={rec[1]} count={rec[2]}")
    await drv.close()


asyncio.run(main())
