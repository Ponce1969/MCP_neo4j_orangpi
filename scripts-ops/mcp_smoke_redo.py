"""Smoke MCP post-resolución del libro graphrag-agentic (read-only).

Verifica que el MCP responde con el libro nuevo (GraphRAG agéntico) y que el
scope aísla correctamente entre libros.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.types import TextContent

MCP_URL = "http://100.106.85.109:8003/sse"
# Optional bearer token (MCP_ACCESS_TOKEN) forwarded as an Authorization header.
_ACCESS_TOKEN = __import__("os").environ.get("MCP_ACCESS_TOKEN", "")
BOOK1 = "knowledge:agentic-architectural-patterns"
BOOK2 = "knowledge:essential-graphrag"
BOOK3 = "knowledge:graphrag-agentic"


async def ask(session: ClientSession, tool: str, args: dict[str, Any]) -> str:
    res = await session.call_tool(tool, args)
    return "\n".join(c.text for c in res.content if isinstance(c, TextContent))


async def main() -> None:
    headers = {"Authorization": f"Bearer {_ACCESS_TOKEN}"} if _ACCESS_TOKEN else {}
    async with (
        sse_client(MCP_URL, headers=headers) as (read, write),
        ClientSession(read, write) as session,
    ):
        init = await session.initialize()
        print(
            f"Handshake: {init.serverInfo.name} {init.serverInfo.version} "
            f"protocol={init.protocolVersion}\n"
        )

        print("=== count_entities por libro ===")
        for scope in (BOOK1, BOOK2, BOOK3):
            out = await ask(session, "count_entities", {"source_id": scope})
            print(f"  {scope}: {out.strip()}")

        print("\n=== search_rag en libro GA ===")
        for query in ("MCP server", "graph memory", "tool orchestration"):
            out = await ask(
                session,
                "search_rag",
                {"query": query, "source_id": BOOK3, "limit": 2},
            )
            data = json.loads(out)
            print(f"\n  Q: {query!r} -> {len(data.get('entities', []))} entidades")
            for e in data.get("entities", [])[:2]:
                ent = e["entity"]
                print(
                    f"     - {ent['name']} ({ent['type']}) p{ent.get('source_page')} "
                    f"[{e.get('source')}]"
                )

        print("\n=== search_rag: aislamiento (query GA en libro 1) ===")
        out = await ask(
            session,
            "search_rag",
            {"query": "MCP server", "source_id": BOOK1, "limit": 2},
        )
        data = json.loads(out)
        print(f"  'MCP server' en libro 1 -> {len(data.get('entities', []))} entidades")
        for e in data.get("entities", [])[:2]:
            print(f"     - {e['entity']['name']} [{e.get('source')}]")


if __name__ == "__main__":
    asyncio.run(main())
