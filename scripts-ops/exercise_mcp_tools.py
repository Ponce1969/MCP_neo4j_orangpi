"""Exercise the enabled MCP tools once, read-only, to build executability evidence.

Unit 4.2/4.3 of ``openspec/changes/skill-quality-gating``. The skill gate scores
``executability`` from ``logs/mcp_queries.jsonl``, where **no records scores 0.0**: the gate
cannot mean anything until every enabled tool has at least one clean call. This script makes
exactly those calls — read-only, no graph mutation, no writes — and reports per tool, so the
next seeding run has real evidence to score.

``query_cypher`` is deliberately not exercised: it is disabled by config
(``MCP_ENABLE_QUERY_CYPHER=false``), so calling it would only record an error and drag its
skill below the threshold. That is the intended outcome — a disabled capability should not be
advertised in the tool surface.

Usage (on the host, with ``.env`` sourced):

    set -a; . ./.env; set +a
    uv run --no-sync python scripts-ops/exercise_mcp_tools.py
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from typing import Any

from mcp import ClientSession
from mcp.client.sse import sse_client

MCP_URL = "http://100.106.85.109:8003/sse"
BOOK = "knowledge:ai-engineering-huyen"
_ENTITY_ID = re.compile(r"knowledge:[a-z0-9-]+:[A-Za-z0-9._-]+")


async def ask(session: ClientSession, tool: str, args: dict[str, Any]) -> str:
    """Call one tool and return its text payload (the same shape the smoke uses)."""
    result = await session.call_tool(tool, args)
    parts = [getattr(block, "text", "") for block in result.content]
    return "\n".join(part for part in parts if part)


def _first_entity_id(payload: str) -> str | None:
    """Pull an entity id out of a tool response, so traversal has something to walk from."""
    match = _ENTITY_ID.search(payload)
    return match.group(0) if match else None


async def main() -> None:
    token = os.environ.get("MCP_ACCESS_TOKEN")
    if not token:
        print("MCP_ACCESS_TOKEN is not set: source .env first", file=sys.stderr)
        sys.exit(2)
    headers = {"Authorization": f"Bearer {token}"}

    results: list[tuple[str, bool, str]] = []
    async with (
        sse_client(MCP_URL, headers=headers) as (read, write),
        ClientSession(read, write) as session,
    ):
        init = await session.initialize()
        print(f"handshake: {init.serverInfo.name} {init.serverInfo.version}\n")

        async def call(tool: str, args: dict[str, Any]) -> str:
            try:
                payload = await ask(session, tool, args)
            except Exception as exc:  # noqa: BLE001
                results.append((tool, False, f"{type(exc).__name__}: {exc}"))
                return ""
            results.append((tool, True, payload[:120].replace("\n", " ")))
            return payload

        await call("count_entities", {"source_id": BOOK})
        await call("find_entity", {"name": "RAG", "source_id": BOOK})
        listed = await call("list_entities", {"cursor": 0, "page_size": 3, "source_id": BOOK})
        entity_id = _first_entity_id(listed)
        if entity_id is None:
            results.append(
                ("traverse_relationships", False, "no entity id in the list_entities payload")
            )
        else:
            await call(
                "traverse_relationships",
                {"source_id": entity_id, "depth": 1, "scope_source_id": BOOK},
            )
        await call("search_chunks", {"query": "modelos", "limit": 3, "source_id": BOOK})
        await call("search_rag", {"query": "modelos", "limit": 3, "source_id": BOOK})
        await call(
            "ask_global",
            {"question": "de que trata el libro?", "detail_level": 1, "source_id": BOOK},
        )

    print("tool, ok, detail")
    for tool, ok, detail in results:
        print(f"{tool}, {'ok' if ok else 'FAILED'}, {detail}")
    failed = [tool for tool, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} tools answered cleanly")
    if failed:
        print(f"failed: {', '.join(failed)}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
