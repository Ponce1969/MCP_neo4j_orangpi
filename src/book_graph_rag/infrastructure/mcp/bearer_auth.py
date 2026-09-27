"""Bearer-token authentication middleware for the MCP SSE server.

Pure ASGI wrapper: every HTTP request to the MCP server must carry
``Authorization: Bearer <token>`` matching the configured access token
(compared in constant time). Requests without a valid token receive
``401 Unauthorized`` before reaching the MCP application.

The check is opt-in at runtime: callers enable it only when a token is
configured (``Settings.mcp_access_token``).
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

_JSON_401 = json.dumps({"error": "unauthorized", "detail": "valid Bearer token required"}).encode(
    "utf-8"
)


class BearerTokenAuthMiddleware:
    """Reject HTTP requests lacking the exact configured Bearer token."""

    def __init__(self, app: Any, token: str) -> None:
        self._app = app
        self._expected = f"Bearer {token}".encode()

    async def __call__(
        self,
        scope: MutableMapping[str, Any],
        receive: Callable[[], Awaitable[MutableMapping[str, Any]]],
        send: Callable[[MutableMapping[str, Any]], Awaitable[None]],
    ) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization", b"")
        if not secrets.compare_digest(auth, self._expected):
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"www-authenticate", b"Bearer"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": _JSON_401})
            return
        await self._app(scope, receive, send)
