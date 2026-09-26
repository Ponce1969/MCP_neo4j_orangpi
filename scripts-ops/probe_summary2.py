import asyncio

from neo4j import AsyncGraphDatabase

from book_graph_rag.config import Settings

s = Settings.model_validate({})
P = "knowledge:graphrag-agentic"


async def main() -> None:
    drv = AsyncGraphDatabase.driver(
        s.neo4j_uri, auth=(s.neo4j_user, s.neo4j_password.get_secret_value())
    )
    async with drv.session() as ses:
        print("== sections del libro parcial según book_id ==")
        rec = await (await ses.run(
            "MATCH (n:Section) WHERE n.book_id = $p RETURN count(n) AS c", p=P
        )).single()
        assert rec is not None
        print("  sections con book_id:", rec[0])
        rec = await (await ses.run("MATCH (n:Section) RETURN count(n) AS c")).single()
        assert rec is not None
        print("  sections totales:", rec[0])
        print("== secciones con ps>305 ==")
        recs = await (await ses.run(
            "MATCH (n:Section) WHERE n.page_start > 305 "
            "RETURN n.id AS id, n.title AS t, n.page_start AS ps, n.level AS lv, "
            "n.book_id AS bid, n.chapter_number AS cn "
            "ORDER BY n.page_start LIMIT 25"
        )).values()
        for r in recs:
            print(f"  id={r[0]!r} t={r[1]!r} ps={r[2]} lv={r[3]} bid={r[4]!r} cn={r[5]}")
        print("== Summary 4/6/8: sus parents y Book ==")
        for cn in (4, 6, 8):
            q = (
                "MATCH (n:Section) WHERE n.title = $t AND n.chapter_number = $cn "
                "OPTIONAL MATCH (b:Book)-[:CONTAINS]->(:Chapter)"
                "-[:HAS_SECTION|HAS_SUBSECTION*1..]->(n) "
                "RETURN n.id AS id, n.page_start AS ps, b.id AS bid, b.page_count AS bpc"
            )
            recs = await (await ses.run(q, t="Summary", cn=cn)).values()
            for r in recs:
                print(f"  Summary cn={cn} id={r[0]!r} ps={r[1]} bid={r[2]!r} bpc={r[3]}")
    await drv.close()


asyncio.run(main())
