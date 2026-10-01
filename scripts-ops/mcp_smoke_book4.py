"""Smoke de salud del MCP contra produccion tras el cierre del libro 4 (read-only).

Verifica el handshake, que los CUATRO namespaces esten expuestos via
``count_entities``, que ``search_rag`` devuelva entidades del libro nuevo y que el
scope aisle correctamente entre libros.

Uso (en el OrangePi, con el .env sourceado para tomar MCP_ACCESS_TOKEN):
    uv run --no-sync python scripts-ops/mcp_smoke_book4.py
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.types import TextContent

MCP_URL = "http://100.106.85.109:8003/sse"
_ACCESS_TOKEN = os.environ.get("MCP_ACCESS_TOKEN", "")
BOOK1 = "knowledge:agentic-architectural-patterns"
BOOK2 = "knowledge:essential-graphrag"
BOOK3 = "knowledge:graphrag-agentic"
BOOK4 = "knowledge:ai-engineering-huyen"


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

        print("=== count_entities: los 4 namespaces ===")
        for scope in (BOOK1, BOOK2, BOOK3, BOOK4):
            out = await ask(session, "count_entities", {"source_id": scope})
            print(f"  {scope}: {out.strip()}")

        print("\n=== search_rag en el libro 4 ===")
        for query in (
            "evaluacion de modelos",
            "RAG y agentes",
            "optimizacion de la inferencia",
        ):
            out = await ask(
                session,
                "search_rag",
                {"query": query, "source_id": BOOK4, "limit": 2},
            )
            data = json.loads(out)
            print(f"\n  Q: {query!r} -> {len(data.get('entities', []))} entidades")
            for entity in data.get("entities", [])[:2]:
                ent = entity["entity"]
                print(
                    f"     - {ent['name']} ({ent['type']}) p{ent.get('source_page')} "
                    f"[{entity.get('source')}]"
                )

        print("\n=== aislamiento: query del libro 4 contra el libro 1 ===")
        out = await ask(
            session,
            "search_rag",
            {"query": "evaluacion de modelos", "source_id": BOOK1, "limit": 2},
        )
        data = json.loads(out)
        print(f"  'evaluacion de modelos' en libro 1 -> {len(data.get('entities', []))} entidades")

        print("\n=== tools expuestas por el servidor ===")
        tools = await session.list_tools()
        print("  " + ", ".join(t.name for t in tools.tools))


if __name__ == "__main__":
    asyncio.run(main())
