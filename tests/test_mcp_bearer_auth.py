"""Tests for BearerTokenAuthMiddleware (MCP SSE access token)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, MutableMapping
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from book_graph_rag.config import Settings
from book_graph_rag.infrastructure.mcp.bearer_auth import BearerTokenAuthMiddleware
from tests.host_env import clear_ambient_settings_env

ASGIScope = MutableMapping[str, Any]
ASGIReceive = Callable[[], Awaitable[MutableMapping[str, Any]]]
ASGISend = Callable[[MutableMapping[str, Any]], Awaitable[None]]
ASGIApp = Callable[[ASGIScope, ASGIReceive, ASGISend], Awaitable[None]]


async def _ok_app(scope: ASGIScope, receive: ASGIReceive, send: ASGISend) -> None:
    del scope, receive
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain")],
        }
    )
    await send({"type": "http.response.body", "body": b"ok"})


@pytest.mark.anyio
async def test_middleware_rejects_missing_token() -> None:
    app = BearerTokenAuthMiddleware(_ok_app, "s3cr3t")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/sse")
    assert resp.status_code == 401
    assert resp.json()["error"] == "unauthorized"
    assert resp.headers["www-authenticate"] == "Bearer"


@pytest.mark.anyio
async def test_middleware_rejects_wrong_token() -> None:
    app = BearerTokenAuthMiddleware(_ok_app, "s3cr3t")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/sse", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_middleware_accepts_valid_token() -> None:
    app = BearerTokenAuthMiddleware(_ok_app, "s3cr3t")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/sse", headers={"Authorization": "Bearer s3cr3t"})
    assert resp.status_code == 200
    assert resp.text == "ok"


@pytest.mark.anyio
async def test_middleware_passes_non_http_scope() -> None:
    sent: list[MutableMapping[str, Any]] = []

    async def send_stub(message: MutableMapping[str, Any]) -> None:
        sent.append(message)

    async def lifespan_app(scope: ASGIScope, receive: ASGIReceive, send: ASGISend) -> None:
        del scope, receive
        await send({"type": "lifespan.startup.complete"})

    app = BearerTokenAuthMiddleware(lifespan_app, "s3cr3t")

    async def receive_stub() -> MutableMapping[str, Any]:
        return {}

    await app({"type": "lifespan"}, receive_stub, send_stub)
    assert sent == [{"type": "lifespan.startup.complete"}]


def test_settings_accepts_optional_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """mcp_access_token round-trips as a SecretStr and defaults to None.

    Machine-independence pin: the production host's env carries a real token, and
    ``Settings.model_validate`` reads ambient env vars and the checkout's ``.env`` —
    it made ``without.mcp_access_token is None`` report the host's token. The test
    therefore rebuilds that hostile host (ambient env var set below), shows an
    unisolated construction absorbing it, and then asserts with both ambient
    channels cut: an empty working directory and every field-named env var gone.
    """
    hostile_token = "hostile-ambient-token-0123456789abcdef"
    base = {
        "neo4j_uri": "bolt://localhost:7687",
        "neo4j_user": "neo4j",
        "neo4j_password": "secret",
    }
    monkeypatch.setenv("MCP_ACCESS_TOKEN", hostile_token)

    # The channel is live: without isolation this is the host's collision.
    assert Settings.model_validate(dict(base)).mcp_access_token == SecretStr(hostile_token)

    monkeypatch.chdir(tmp_path)
    clear_ambient_settings_env(monkeypatch)

    with_token = Settings.model_validate({**base, "mcp_access_token": "tok-123"})
    assert with_token.mcp_access_token == SecretStr("tok-123")
    without = Settings.model_validate(dict(base))
    assert without.mcp_access_token is None
