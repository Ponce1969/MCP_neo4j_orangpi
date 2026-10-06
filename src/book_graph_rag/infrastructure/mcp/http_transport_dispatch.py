"""Single-port ASGI dispatcher for the two MCP transports.

Serves the SSE app (``/sse`` + ``/messages/``) and the streamable-HTTP app
(``/mcp``) from the SAME FastMCP server on the SAME port, and 404s everything
else. :class:`BearerTokenAuthMiddleware` must wrap THIS dispatcher (not the
sub-apps) so both transports share exactly one auth rule.

Lifespan contract: every sub-app receives its own ASGI lifespan messages.
Startup runs in declaration order (SSE first, streamable second — the
streamable app owns the ``StreamableHTTPSessionManager``, so swallowing its
lifespan would break every ``/mcp`` session); shutdown runs in reverse order.
A sub-app startup failure is reported to the outer server as
``lifespan.startup.failed`` after the already-started apps are shut down in
reverse order.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

logger = logging.getLogger(__name__)

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]

_404_BODY = b"404 Not Found"


def _route(path: str) -> str | None:
    """Return ``"sse"``, ``"streamable"``, or ``None`` (404) for an HTTP path."""
    if path == "/sse" or path.startswith("/messages/"):
        return "sse"
    if path == "/mcp" or path.startswith("/mcp/"):
        return "streamable"
    return None


class _SubAppLifespan:
    """Drive one sub-app's ASGI lifespan from the dispatcher's lifespan."""

    def __init__(self, app: Any) -> None:
        self._app = app
        self._inbox: asyncio.Queue[MutableMapping[str, Any]] = asyncio.Queue()
        self._startup_done = asyncio.Event()
        self._shutdown_done = asyncio.Event()
        self._startup_error: str | None = None
        self._shutdown_error: str | None = None
        self._task: asyncio.Task[None] | None = None

    async def _receive(self) -> MutableMapping[str, Any]:
        return await self._inbox.get()

    async def _send(self, message: MutableMapping[str, Any]) -> None:
        kind = message.get("type")
        if kind == "lifespan.startup.complete":
            self._startup_done.set()
        elif kind == "lifespan.startup.failed":
            self._startup_error = str(message.get("message", "startup failed"))
            self._startup_done.set()
        elif kind == "lifespan.shutdown.complete":
            self._shutdown_done.set()
        elif kind == "lifespan.shutdown.failed":
            self._shutdown_error = str(message.get("message", "shutdown failed"))
            self._shutdown_done.set()

    async def _await(self, event: asyncio.Event, what: str) -> None:
        """Wait for a lifespan signal, failing fast if the sub-app task dies first."""
        if self._task is None:  # pragma: no cover - defensive
            raise RuntimeError(f"sub-app lifespan for {what} was never launched")
        waiter: asyncio.Task[bool] = asyncio.ensure_future(event.wait())
        try:
            await asyncio.wait({waiter, self._task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if not waiter.done():
                waiter.cancel()
        if not event.is_set():
            if self._task.cancelled():  # pragma: no cover - defensive
                detail = "cancelled"
            else:
                exc = self._task.exception()
                detail = repr(exc)
            raise RuntimeError(f"sub-app {what} ended without signalling completion: {detail}")

    async def startup(self) -> None:
        """Launch the sub-app and run its ``lifespan.startup`` phase."""
        scope: Scope = {
            "type": "lifespan",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "state": {},
        }
        self._task = asyncio.create_task(self._app(scope, self._receive, self._send))
        await self._inbox.put({"type": "lifespan.startup"})
        await self._await(self._startup_done, "startup")
        if self._startup_error is not None:
            await self._teardown_after_failed_startup()
            raise RuntimeError(f"sub-app startup failed: {self._startup_error}")

    async def _teardown_after_failed_startup(self) -> None:
        """Best-effort cleanup when the sub-app never reached ``startup.complete``."""
        if self._task is None:  # pragma: no cover - defensive
            return
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            logger.debug("sub-app startup task torn down", exc_info=True)

    async def shutdown(self) -> None:
        """Run the sub-app's ``lifespan.shutdown`` phase and await task exit."""
        if self._task is None:  # pragma: no cover - defensive
            return
        await self._inbox.put({"type": "lifespan.shutdown"})
        await self._await(self._shutdown_done, "shutdown")
        if self._shutdown_error is not None:
            raise RuntimeError(f"sub-app shutdown failed: {self._shutdown_error}")
        await self._task


class TransportDispatcher:
    """Route one HTTP port to the SSE app or the streamable-HTTP app."""

    def __init__(self, *, sse_app: Any, streamable_app: Any) -> None:
        self._sse_app = sse_app
        self._streamable_app = streamable_app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope_type = scope.get("type")
        if scope_type == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope_type != "http":
            if scope_type == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return

        target = _route(str(scope.get("path", "")))
        if target == "sse":
            await self._sse_app(scope, receive, send)
        elif target == "streamable":
            await self._streamable_app(scope, receive, send)
        else:
            await send(
                {
                    "type": "http.response.start",
                    "status": 404,
                    "headers": [(b"content-type", b"text/plain; charset=utf-8")],
                }
            )
            await send({"type": "http.response.body", "body": _404_BODY})

    async def _lifespan(self, receive: Receive, send: Send) -> None:
        # Startup order: SSE first, then the streamable app (session manager);
        # shutdown always unwinds in reverse.
        sub_apps = (
            _SubAppLifespan(self._sse_app),
            _SubAppLifespan(self._streamable_app),
        )
        started: list[_SubAppLifespan] = []

        startup_message = await receive()
        if startup_message.get("type") != "lifespan.startup":
            raise RuntimeError(f"expected lifespan.startup, got {startup_message.get('type')!r}")

        startup_error: Exception | None = None
        for sub_app in sub_apps:
            try:
                await sub_app.startup()
            except Exception as exc:  # noqa: BLE001 - reported to the outer server
                startup_error = exc
                break
            started.append(sub_app)

        if startup_error is not None:
            await self._shutdown_started(started)
            await send(
                {
                    "type": "lifespan.startup.failed",
                    "message": f"{startup_error}",
                }
            )
            return

        await send({"type": "lifespan.startup.complete"})

        shutdown_message = await receive()
        await self._shutdown_started(started)

        if shutdown_message.get("type") != "lifespan.shutdown":
            await send(
                {
                    "type": "lifespan.shutdown.failed",
                    "message": f"expected lifespan.shutdown, got {shutdown_message.get('type')!r}",
                }
            )
            return
        await send({"type": "lifespan.shutdown.complete"})

    async def _shutdown_started(self, started: list[_SubAppLifespan]) -> None:
        """Shut the given sub-apps down in REVERSE startup order, never raising."""
        for sub_app in reversed(started):
            try:
                await sub_app.shutdown()
            except Exception:  # noqa: BLE001 - one failing app must not block the rest
                logger.exception("sub-app lifespan shutdown failed")
