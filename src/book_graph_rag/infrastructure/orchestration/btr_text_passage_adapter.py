"""BTRAG text-passage adapter: ``TextPassagePort`` over the external text store (T7).

Implements the port the orchestrator derives to when the graph does not cover a
question (S6/S7) and the port S5 asks for when textual citations are required.
The service is the **read-only** book-text-rag MCP endpoint, reached over
streamable HTTP with a bearer token; the contract was measured in L15, not
assumed:

* tool ``search(query, top_k)`` — lexical only — returns ``{mode,
  index_version, resultados: [{rank, score, document_id, document_version,
  chunk_id, section_path, pdf_page_start, pdf_page_end, content}]}`` both as
  ``structuredContent`` and as JSON text in ``content``;
* provenance format for citations in this stack: ``book-text-rag:<chunk_id>``
  (L15 precedent).

Decisions recorded in L15, not re-litigated here:

* **(a)** ``Passage.namespace`` stays ``None``: BTRAG's ``document_id``
  (``huyen-ai-engineering``) is a different catalogue from the graph's
  ``SourceNamespace`` (``ai-engineering-huyen``). Reconciling the two is a
  separate unit; a fabricated mapping would poison provenance, so the
  namespace is left unset rather than guessed.
* **(b)** the transport needs an ``httpx.AsyncClient`` because
  ``streamable_http_client`` takes no ``headers``, which is why ``httpx`` is a
  declared dependency rather than an undeclared transitive one.
* **(c)** construction is fail-closed: a blank URL or an empty/invisible token
  raises :class:`TextRagConfigurationError` **at construction** — never at
  query time, and never as an empty tuple papering over a missing setting.

Transport errors are never swallowed: whatever ``httpx`` or ``mcp`` raise
propagates untouched, per the port contract.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, TextContent
from pydantic import SecretStr

from book_graph_rag.application.orchestration.ports import TextPassagePort
from book_graph_rag.domain.mcp_security import is_effective_secret_shape
from book_graph_rag.domain.orchestration_outcomes import Passage

#: Provenance prefix for every citation this adapter produces (L15).
_PROVENANCE_PREFIX = "book-text-rag:"

#: Read-phase timeout in seconds: the MCP SDK's own ``MCP_DEFAULT_SSE_READ_TIMEOUT``
#: (``mcp.shared._httpx_utils``) is 300 s — streamable HTTP holds a GET open for the
#: whole session, so the read phase must outlive the plain 30 s default.
_MCP_READ_TIMEOUT_SECONDS = 300.0


class TextRagConfigurationError(RuntimeError):
    """A required text-rag setting is missing; raised at construction, never at query."""


class BtrTextPassageAdapter(TextPassagePort):
    """``TextPassagePort`` over the read-only book-text-rag ``search`` tool (L15).

    Construction is fail-closed (L15 decision c): a blank ``url`` or an
    empty/invisible ``token`` raises :class:`TextRagConfigurationError`
    immediately, naming the missing setting — the failure is never deferred to
    query time and never turned into an empty tuple. The token arrives as a
    ``SecretStr`` or ``str``, is validated with the repo's single secret-shape
    rule (:func:`is_effective_secret_shape`), stored stripped, and used only to
    build the ``Authorization: Bearer`` header.

    There is no constructor-level ``k``: every request carries the per-call
    ``limit``, because the port and S13's tuned ``k`` make the caller's passage
    budget authoritative (L10) — a constructor knob would either lie dead or
    override a caller's budget, so it does not exist. ``timeout_seconds``
    bounds the connection phase only (30 s by default, matching the MCP SDK's
    ``MCP_DEFAULT_TIMEOUT``); the read phase is fixed at the SDK's 300 s.
    """

    def __init__(
        self,
        url: str,
        token: SecretStr | str,
        *,
        timeout_seconds: float = 30.0,
    ) -> None:
        endpoint = url.strip()
        if not endpoint:
            raise TextRagConfigurationError(
                "text_rag_url is blank: BtrTextPassageAdapter fails closed at construction "
                "(L15 decision c); set text_rag_url (env TEXT_RAG_URL)."
            )
        secret = token.get_secret_value() if isinstance(token, SecretStr) else token
        if not is_effective_secret_shape(secret):
            raise TextRagConfigurationError(
                "text_rag_token is empty or invisible: BtrTextPassageAdapter fails closed at "
                "construction (L15 decision c); set text_rag_token (env TEXT_RAG_TOKEN)."
            )
        self._url = endpoint
        self._token = secret.strip()
        self._timeout_seconds = timeout_seconds

    async def passages(self, question: str, *, limit: int) -> tuple[Passage, ...]:
        """Return at most ``limit`` passages for ``question``, best-first.

        **Response order (L15):** ``structuredContent`` is preferred; the JSON
        text in ``content`` is parsed **only** when structured content is
        absent. Results keep the service's ``rank`` order (the payload order);
        a result with blank ``content`` or blank ``chunk_id`` is dropped
        because its provenance would be unverifiable; an empty result set
        yields ``()`` — a passage is never invented. Citations carry the
        ``book-text-rag:`` provenance prefix and ``namespace`` stays ``None``
        (L15 decision a: BTRAG's ``document_id`` catalogue is not the graph's
        ``SourceNamespace``).

        Contexts close innermost-first: session, then the stream (which
        terminates the MCP session), then the ``httpx`` client.

        Raises:
            Whatever ``httpx``/``mcp`` raise — transport errors propagate,
            never swallowed. An ``isError`` tool result raises
            ``RuntimeError``, and so does a successful result carrying neither
            ``structuredContent`` nor a text block: both are failures, not
            "no passages", which must stay reserved for S6's insufficient
            coverage.
        """
        if limit <= 0:
            return ()
        headers = {"Authorization": f"Bearer {self._token}"}
        # Two deliberate transport choices: trust_env=False keeps ambient proxies and
        # ~/.netrc away from a client that carries a bearer token; the timeouts object
        # mirrors the MCP SDK's own shape (mcp.shared._httpx_utils: 30 s connect by
        # default, 300 s read) because a scalar 30 s timeout would cut the long-lived
        # streamable-HTTP GET that the SDK itself allows to run for 300 s.
        async with (
            httpx.AsyncClient(
                headers=headers,
                timeout=httpx.Timeout(self._timeout_seconds, read=_MCP_READ_TIMEOUT_SECONDS),
                trust_env=False,
            ) as client,
            streamable_http_client(self._url, http_client=client) as (
                read,
                write,
                _session_id,
            ),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.call_tool("search", {"query": question, "top_k": limit})
        payload = _payload_from_result(result)
        if payload is None:
            # A text block exists but carries no JSON object: nothing to map.
            # The protocol surprise (neither structured content nor a text block)
            # already raised inside _payload_from_result.
            return ()
        return map_search_results(payload, limit=limit)


def map_search_results(payload: Mapping[str, Any], *, limit: int) -> tuple[Passage, ...]:
    """Map a ``search`` payload's ``resultados`` to passages, best-first.

    Pure and network-free so it can be tested against recorded payloads. The
    payload order (= the service's ``rank`` order) is preserved verbatim —
    never re-sorted. Results with blank ``content`` or blank ``chunk_id`` are
    dropped (unverifiable provenance), at most ``limit`` passages are
    returned, and an empty or absent ``resultados`` yields ``()`` without
    constructing a single :class:`Passage`.
    """
    if limit <= 0:
        return ()
    results = payload.get("resultados")
    if not isinstance(results, list):
        return ()
    passages: list[Passage] = []
    for item in results:
        if len(passages) >= limit:
            break
        if not isinstance(item, Mapping):
            continue
        content = item.get("content")
        chunk_id = item.get("chunk_id")
        if not isinstance(content, str) or not content.strip():
            continue
        if not isinstance(chunk_id, str) or not chunk_id.strip():
            continue
        passages.append(
            Passage(
                chunk_id=f"{_PROVENANCE_PREFIX}{chunk_id}",
                text=content,
                namespace=None,
            )
        )
    return tuple(passages)


def _payload_from_result(result: CallToolResult) -> Mapping[str, Any] | None:
    """Prefer ``structuredContent``; parse JSON text in ``content`` only when absent (L15).

    Two failure shapes are told apart on purpose:

    * an ``isError`` tool result raises ``RuntimeError``, and so does a
      successful result carrying neither ``structuredContent`` nor a text
      block (e.g. only image content) — reporting either as zero passages
      would hide a real failure behind "insufficient coverage" (S6);
    * a text block that exists but holds no JSON object returns ``None``
      (nothing to map, mapped to ``()`` by the caller).

    Malformed JSON is neither of those — it is a broken response, so
    ``json.JSONDecodeError`` propagates.
    """
    if result.isError:
        detail = " ".join(block.text for block in result.content if isinstance(block, TextContent))
        raise RuntimeError(f"book-text-rag search returned an error: {detail.strip()}")
    structured = result.structuredContent
    if structured is not None:
        return structured
    text_blocks = [block for block in result.content if isinstance(block, TextContent)]
    if not text_blocks:
        # Protocol surprise: a successful call must carry either structuredContent or
        # at least one text block. Returning () here would be indistinguishable from
        # S6's insufficient coverage, so name the surprise instead (like isError does).
        content_types = [type(block).__name__ for block in result.content]
        raise RuntimeError(
            "book-text-rag search succeeded but carried neither structuredContent nor a "
            f"text content block (content types: {content_types}); refusing to report a "
            "protocol surprise as zero passages"
        )
    for block in text_blocks:
        parsed = json.loads(block.text)
        if isinstance(parsed, Mapping):
            return parsed
    return None
