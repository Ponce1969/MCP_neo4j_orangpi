"""MCP transport matrix + self-description contract (in-process, no Docker).

Boots the transport dispatcher with uvicorn on an ephemeral loopback port and
drives it with the official MCP SDK clients:

* the legacy SSE transport (``/sse`` + ``/messages/``) must keep working;
* the streamable HTTP transport (``/mcp``) must work (pi-compatible clients);
* bearer auth is one rule shared by BOTH transports (401 without a token);
* the server must describe itself: ``initialize.instructions``, the
  ``bookgraph://catalog`` resource, honest required/enum scope schemas, and
  scope errors that list the valid values.

No Neo4j, no Docker, no LLM: graph, logging, and text2cypher ports are stubs.
Assertions stay on protocol surfaces (initialize result, resources, tool
schemas, tool call results) — never on private adapter internals.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import TextContent, TextResourceContents
from pydantic import SecretStr

from book_graph_rag.domain.mcp_security import ScopeContext
from book_graph_rag.domain.models import (
    EntityType,
    EntityWithContext,
    GraphPath,
    Relationship,
    RelationshipType,
)
from book_graph_rag.infrastructure.catalog_loader import CatalogLoader
from book_graph_rag.infrastructure.catalog_scope_resolver import CatalogScopeResolver
from book_graph_rag.infrastructure.mcp.bearer_auth import BearerTokenAuthMiddleware
from book_graph_rag.infrastructure.mcp.http_transport_dispatch import TransportDispatcher
from book_graph_rag.infrastructure.mcp.mcp_server_adapter import McpServerAdapter
from book_graph_rag.ports.graph_query_port import GraphQueryPort
from book_graph_rag.ports.query_logger_port import QueryLoggerPort
from book_graph_rag.ports.text2cypher_port import Text2CypherPort, Text2CypherResult

_TOKEN = "integration-test-token"
_CATALOG_PATH = Path(__file__).resolve().parents[2] / "catalog.yaml"

_CATALOG = CatalogLoader(_CATALOG_PATH).load()
_ACTIVE_SOURCE_IDS: list[str] = sorted(
    f"{corpus}:{slug}"
    for corpus, corpus_model in _CATALOG.corpora.items()
    for slug, source_model in corpus_model.sources.items()
    if source_model.status == "active"
)


def _base(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_TOKEN}"}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# ── Stubs (no Neo4j, no LLM, no log files) ───────────────────────────────────


class _StubGraphQueryPort(GraphQueryPort):
    """Returns deterministic data; records the scope bound to count_entities."""

    def __init__(self) -> None:
        self.count_scopes: list[ScopeContext | None] = []

    async def find_entity(
        self,
        name: str,
        entity_type: EntityType | None,
        *,
        scope: ScopeContext | None = None,
    ) -> list[EntityWithContext]:
        raise AssertionError("find_entity is not exercised by these tests")

    async def find_entities_batch(self, ids: list[str]) -> list[EntityWithContext]:
        raise AssertionError("find_entities_batch is not exercised by these tests")

    async def traverse_relationships(
        self,
        source_id: str,
        rel_type: RelationshipType | None,
        depth: int,
        *,
        scope: ScopeContext | None = None,
    ) -> tuple[list[EntityWithContext], list[Relationship]]:
        raise AssertionError("traverse_relationships is not exercised by these tests")

    async def find_path(self, start_id: str, end_id: str, max_depth: int) -> list[GraphPath]:
        raise AssertionError("find_path is not exercised by these tests")

    async def search_chunks(
        self, query: str, limit: int, *, scope: ScopeContext | None = None
    ) -> list[dict[str, Any]]:
        raise AssertionError("search_chunks is not exercised by these tests")

    async def count_entities(
        self, entity_type: str | None, *, scope: ScopeContext | None = None
    ) -> int:
        self.count_scopes.append(scope)
        return 42

    async def list_entities(
        self,
        cursor: int,
        page_size: int,
        *,
        scope: ScopeContext | None = None,
    ) -> tuple[list[EntityWithContext], int]:
        raise AssertionError("list_entities is not exercised by these tests")

    async def ensure_indexes(self) -> None:
        raise AssertionError("ensure_indexes is not exercised by these tests")


class _NoopQueryLogger(QueryLoggerPort):
    async def log_query(self, entry: Any) -> None:
        pass

    async def close(self) -> None:
        pass


class _ExplodingText2Cypher(Text2CypherPort):
    async def generate_and_run(
        self, question: str, *, scope: ScopeContext | None = None
    ) -> Text2CypherResult:
        raise AssertionError("text2cypher must never run in these tests")


def _build_adapter() -> McpServerAdapter:
    loader = CatalogLoader(_CATALOG_PATH)
    catalog = loader.load()

    async def _stats_reader() -> dict[str, dict[str, int]]:
        return {source_id: {"chunks": 10, "entities": 5} for source_id in _ACTIVE_SOURCE_IDS}

    return McpServerAdapter(
        graph_query_port=_StubGraphQueryPort(),
        query_logger=_NoopQueryLogger(),
        text2cypher_port=_ExplodingText2Cypher(),
        scope_resolver=CatalogScopeResolver(loader),
        catalog=catalog,
        catalog_stats_reader=_stats_reader,
        hmac_key=SecretStr("integration-test-hmac-key"),
        app_env="test",
    )


# ── Server boot (module-scoped: one uvicorn thread for the whole file) ───────


@pytest.fixture(scope="module")
def mcp_port() -> Iterator[int]:
    """Boot the dispatcher behind the bearer middleware on an ephemeral port."""
    adapter = _build_adapter()
    server = adapter.create_server(host="127.0.0.1", port=0)
    dispatcher = TransportDispatcher(
        sse_app=server.sse_app(),
        streamable_app=server.streamable_http_app(),
    )
    app: Any = BearerTokenAuthMiddleware(dispatcher, _TOKEN)

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    uv_server = uvicorn.Server(config)
    thread = threading.Thread(target=lambda: asyncio.run(uv_server.serve()), daemon=True)
    thread.start()

    deadline = time.monotonic() + 15.0
    while not uv_server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start within 15s")
        time.sleep(0.05)

    yield port

    uv_server.should_exit = True
    thread.join(timeout=15)


@asynccontextmanager
async def _streamable_session(port: int) -> AsyncIterator[ClientSession]:
    async with (
        streamablehttp_client(f"{_base(port)}/mcp", headers=_auth_headers()) as (
            read,
            write,
            _get_session_id,
        ),
        ClientSession(read, write) as session,
    ):
        yield session


# ── Transport matrix ─────────────────────────────────────────────────────────


async def test_sse_transport_handshake_still_works(mcp_port: int) -> None:
    """The legacy SSE client completes the handshake through the dispatcher."""
    async with (
        sse_client(f"{_base(mcp_port)}/sse", headers=_auth_headers()) as (read, write),
        ClientSession(read, write) as session,
    ):
        init = await session.initialize()

    assert init.serverInfo.name == "book-graph-rag"
    assert init.instructions, "the server describes itself on the SSE transport too"


async def test_streamable_transport_handshake_and_tool_call(mcp_port: int) -> None:
    """A pi-style streamable client handshakes and runs count_entities."""
    async with _streamable_session(mcp_port) as session:
        init = await session.initialize()
        result = await session.call_tool(
            "count_entities", {"source_id": "knowledge:essential-graphrag"}
        )

    assert init.serverInfo.name == "book-graph-rag"
    assert not result.isError
    first = result.content[0]
    assert isinstance(first, TextContent)
    payload = json.loads(first.text)
    assert payload["count"] == 42


async def test_missing_bearer_token_is_401_on_both_transports(mcp_port: int) -> None:
    """One auth rule wraps the dispatcher: 401 on /sse AND /mcp, 404 elsewhere."""
    async with httpx.AsyncClient() as client:
        sse_response = await client.get(f"{_base(mcp_port)}/sse", timeout=5.0)
        assert sse_response.status_code == 401

        streamable_response = await client.post(
            f"{_base(mcp_port)}/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
            },
            timeout=5.0,
        )
        assert streamable_response.status_code == 401

        unknown = await client.get(
            f"{_base(mcp_port)}/no-such-path", headers=_auth_headers(), timeout=5.0
        )
        assert unknown.status_code == 404


# ── Self-description ─────────────────────────────────────────────────────────


async def test_initialize_instructions_describe_scope_contract(mcp_port: int) -> None:
    """initialize.instructions is non-empty and teaches the scope contract."""
    async with _streamable_session(mcp_port) as session:
        init = await session.initialize()

    text = init.instructions
    assert text, "initialize.instructions must be non-empty"

    # Every active source_id from catalog.yaml is discoverable.
    for source_id in _ACTIVE_SOURCE_IDS:
        assert source_id in text

    # Grammar, filters, two-id trap, failure modes, bilingual hint.
    assert "corpus:source" in text
    assert "book_ids" in text
    assert "traverse_relationships" in text
    assert "scope_source_id" in text
    assert "missing_scope" in text
    assert "invalid_scope" in text
    assert "401" in text
    assert "partly English" in text
    assert "partly Spanish" in text


async def test_catalog_resource_exposes_active_sources_with_language(
    mcp_port: int,
) -> None:
    """resources/list exposes bookgraph://catalog; reading it yields every source."""
    async with _streamable_session(mcp_port) as session:
        await session.initialize()
        listed = await session.list_resources()
        uris = [str(resource.uri) for resource in listed.resources]
        assert any("bookgraph://catalog" in uri for uri in uris)
        catalog_uri = next(
            resource.uri
            for resource in listed.resources
            if "bookgraph://catalog" in str(resource.uri)
        )
        contents = await session.read_resource(catalog_uri)

    text_blobs = [item.text for item in contents.contents if isinstance(item, TextResourceContents)]
    assert text_blobs, "the catalog resource must be readable as text"
    payload = json.loads(text_blobs[0])

    sources = payload["sources"]
    by_id = {source["source_id"]: source for source in sources}
    assert set(by_id) == set(_ACTIVE_SOURCE_IDS)

    for source in sources:
        assert source["label"]
        assert source["language"] in {"en", "es", "unknown"}

    # The filenames in catalog.yaml mark (EN)/(ES).
    assert by_id["knowledge:graphrag-agentic"]["language"] == "en"
    assert by_id["knowledge:ai-engineering-huyen"]["language"] == "es"

    # Size counts come from the injected single read, stamped as of that read.
    assert all(source["chunks"] == 10 for source in sources)
    assert all(source["entities"] == 5 for source in sources)
    assert payload["counts_as_of"]


async def test_scoped_tool_schemas_require_scope_and_enumerate_sources(
    mcp_port: int,
) -> None:
    """The JSON Schema is honest: scope params are required and enumerate sources."""
    async with _streamable_session(mcp_port) as session:
        await session.initialize()
        listed = await session.list_tools()

    tools = {tool.name: tool for tool in listed.tools}
    for tool_name, scope_param in (
        ("search_rag", "source_id"),
        ("traverse_relationships", "scope_source_id"),
    ):
        schema = tools[tool_name].inputSchema
        assert scope_param in schema.get("required", []), (
            f"{tool_name}.{scope_param} must be declared required"
        )
        prop = schema["properties"][scope_param]
        assert prop["type"] == "string"
        assert sorted(prop["enum"]) == sorted(_ACTIVE_SOURCE_IDS)


async def test_scope_errors_list_valid_values(mcp_port: int) -> None:
    """An invalid scope errors out naming every valid value; missing scope errors too."""
    async with _streamable_session(mcp_port) as session:
        await session.initialize()
        invalid = await session.call_tool(
            "count_entities", {"source_id": "knowledge:not-a-real-source"}
        )
        missing = await session.call_tool("count_entities", {})

    assert invalid.isError
    invalid_text = invalid.content[0]
    assert isinstance(invalid_text, TextContent)
    for source_id in _ACTIVE_SOURCE_IDS:
        assert source_id in invalid_text.text

    assert missing.isError
    missing_text = missing.content[0]
    assert isinstance(missing_text, TextContent)
    assert "source_id" in missing_text.text
