"""Smoke de salud del MCP contra el grafo de producción (read-only).

Conecta al servidor MCP del OrangePi vía Tailscale (100.106.85.109:8003),
inicializa la sesión y consulta con scope knowledge:essential-graphrag.
No muta nada: solo count_entities + search_rag.
"""

from __future__ import annotations

import asyncio
import sys

from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.types import TextContent

MCP_URL = "http://100.106.85.109:8003/sse"
SCOPE = "knowledge:essential-graphrag"


async def main() -> None:
    print(f"Conectando a {MCP_URL} ...")
    async with sse_client(MCP_URL) as (read, write), ClientSession(read, write) as session:
        init = await session.initialize()
        print(f"Handshake OK: {init.serverInfo.name} {init.serverInfo.version} "
              f"protocol={init.protocolVersion}")

        tools = await session.list_tools()
        print(f"Tools ({len(tools.tools)}):")
        for t in tools.tools:
            print(f"  - {t.name}")

        # 1. count_entities scoped al libro nuevo
        res = await session.call_tool(
            "count_entities", {"source_id": SCOPE}
        )
        print(f"\n[count_entities] source_id={SCOPE}")
        for c in res.content:
            if isinstance(c, TextContent):
                print(f"  {c.text}")

        # 2. count_entities total (ambos libros)
        res = await session.call_tool("count_entities", {"source_id": "knowledge"})
        print("\n[count_entities] source_id=knowledge (ambos)")
        for c in res.content:
            if isinstance(c, TextContent):
                print(f"  {c.text}")

        # 3. search honesta del libro nuevo — Text2Cypher (cap 4)
        res = await session.call_tool(
            "search_rag",
            {"query": "Text2Cypher implementation", "source_id": SCOPE, "limit": 3},
        )
        print(f"\n[search_rag] 'Text2Cypher implementation' source_id={SCOPE}")
        for c in res.content:
            if isinstance(c, TextContent):
                print(f"  {c.text[:600]}")

        # 4. fail-closed: sin scope debe rechazar
        try:
            res = await session.call_tool("count_entities", {})
            first = res.content[0]
            first_text = first.text if isinstance(first, TextContent) else repr(first)
            print(f"\n[count_entities] sin scope → RESPUESTA: {first_text[:200]}")
        except Exception as exc:  # noqa: BLE001
            print(f"\n[count_entities] sin scope → ERROR (fail-closed): {str(exc)[:200]}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:  # noqa: BLE001
        print(f"FALLO: {exc}", file=sys.stderr)
        sys.exit(1)
