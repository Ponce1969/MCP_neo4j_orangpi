import asyncio

from neo4j import AsyncGraphDatabase

from book_graph_rag.config import Settings

s = Settings.model_validate({})


async def main() -> None:
    drv = AsyncGraphDatabase.driver(
        s.neo4j_uri, auth=(s.neo4j_user, s.neo4j_password.get_secret_value())
    )
    async with drv.session() as ses:
        print("== Secciones compartidas: detalle por libro ==")
        recs = await (await ses.run(
            "MATCH (s:Section) "
            "MATCH (p)-[:HAS_SECTION|HAS_SUBSECTION]->(s) "
            "WITH s, collect(DISTINCT p) AS parents "
            "WHERE size(parents) > 1 "
            "UNWIND parents AS p "
            "RETURN s.title AS st, s.chapter_number AS cn, s.page_start AS ps, "
            "labels(p) AS pl, p.title AS pt "
            "ORDER BY cn, st"
        )).values()
        for r in recs:
            print(f"  sec={r[0]!r} cn={r[1]} ps={r[2]} <-- {r[3][0]} {str(r[4])[:50]!r}")
        print("== Determinamos de qué libro es cada chapter por su Book ==")
        recs = await (await ses.run(
            "MATCH (b:Book)-[:CONTAINS]->(c:Chapter) "
            "RETURN b.id AS bid, c.number AS cn, c.title AS ct, c.page_start AS ps "
            "ORDER BY bid, cn"
        )).values()
        for r in recs:
            print(f"  {r[0].split(':')[-1]} cn={r[1]} {str(r[2])[:45]!r} ps={r[3]}")
    await drv.close()


asyncio.run(main())
