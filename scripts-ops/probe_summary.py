import asyncio

from neo4j import AsyncGraphDatabase

from book_graph_rag.config import Settings

s = Settings.model_validate({})


async def main() -> None:
    drv = AsyncGraphDatabase.driver(
        s.neo4j_uri, auth=(s.neo4j_user, s.neo4j_password.get_secret_value())
    )
    async with drv.session() as ses:
        b = await (
            await ses.run(
                "MATCH (b:Book) WHERE b.id = $id RETURN b.page_count AS pc, b.title AS t",
                id="knowledge:graphrag-agentic",
            )
        ).single()
        assert b is not None
        print("BOOK:", dict(b))
        print("== Sections title=Summary ==")
        recs = await (
            await ses.run(
                "MATCH (n:Section) WHERE n.title = $t "
                "RETURN n.chapter_number AS cn, n.title AS t, n.page_start AS ps, "
                "n.level AS lv ORDER BY cn LIMIT 20",
                t="Summary",
            )
        ).values()
        for r in recs:
            print(f"  cn={r[0]} t={r[1]!r} ps={r[2]} lv={r[3]}")
        print("== Sections maxima ps ==")
        rec = await (
            await ses.run("MATCH (n:Section) RETURN max(n.page_start) AS mx, count(n) AS c")
        ).single()
        assert rec is not None
        print(f"  max_ps={rec[0]} count={rec[1]}")
    await drv.close()


asyncio.run(main())
