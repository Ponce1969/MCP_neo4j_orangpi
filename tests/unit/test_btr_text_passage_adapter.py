"""Behavior tests for the BTRAG text-passage adapter (T7; specs S5-S7, L15).

No network anywhere: the response→passages mapping is a pure module-level
function (``map_search_results``) and the payload selection is exercised with
in-memory ``CallToolResult`` objects. The MCP plumbing in ``passages`` is
driven once with the whole transport monkeypatched (client, stream and
session are fakes), so even that test opens no socket. Construction uses a
neutral ``.test`` placeholder host: naming a production endpoint in a unit
test is exactly what ``tests/unit/test_no_external_endpoints.py`` rejects.

L15's measured contract is the fixture: ``search`` returns
``{mode, index_version, resultados: [{rank, score, document_id, ...,
chunk_id, section_path, pdf_page_start, pdf_page_end, content}]}`` both as
``structuredContent`` and as JSON text in ``content``, with provenance
``book-text-rag:<chunk_id>``.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from mcp.types import CallToolResult, ImageContent, TextContent
from pydantic import SecretStr
from tests.host_env import SettingsFactory

from book_graph_rag.infrastructure.orchestration.btr_text_passage_adapter import (
    BtrTextPassageAdapter,
    TextRagConfigurationError,
    _payload_from_result,
    map_search_results,
)

_QUESTION = "How does the ReAct pattern work?"

#: Neutral, reserved (RFC 2606 ``.test``) endpoint every construction in this
#: suite uses. These tests never open a socket, so the host is a dummy string —
#: the production endpoint belongs only in configuration.
_NEUTRAL_URL = "http://text-rag.test/mcp"


def _result(
    rank: int,
    *,
    chunk_id: str = "",
    content: str = "",
    score: float = 1.5,
) -> dict[str, Any]:
    """One ``resultados`` row shaped exactly like the measured L15 payload."""
    return {
        "rank": rank,
        "score": score,
        "document_id": "huyen-ai-engineering",
        "document_version": "1.0",
        "chunk_id": chunk_id,
        "section_path": "04-evaluation",
        "pdf_page_start": 87,
        "pdf_page_end": 88,
        "content": content,
    }


def _payload(results: list[dict[str, Any]]) -> dict[str, Any]:
    return {"mode": "lexical", "index_version": "2026-11-01", "resultados": results}


def _structured(results: list[dict[str, Any]]) -> CallToolResult:
    """A result carrying the payload as ``structuredContent`` (the preferred path)."""
    return CallToolResult(
        content=[TextContent(type="text", text="unstructured twin")],
        structuredContent=_payload(results),
    )


def _text_only(results: list[dict[str, Any]]) -> CallToolResult:
    """A result carrying the payload only as JSON text in ``content`` (the fallback)."""
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(_payload(results)))],
    )


# ── 1. mapping from a realistic payload ───────────────────────────────────────


def test_mapping_preserves_rank_order_prefixes_provenance_and_leaves_namespace_unset() -> None:
    """S5/S7 + L15: rank order preserved, ``book-text-rag:`` prefix, namespace None.

    Chunk ids are deliberately non-ascending (``z-9`` before ``a-1``) so a
    hidden re-sort by id would be caught; ``rank`` order must survive verbatim.
    """
    results = [
        _result(1, chunk_id="huyen-ai-engineering#z-9", content="First by rank."),
        _result(2, chunk_id="huyen-ai-engineering#3", content="Second by rank."),
        _result(3, chunk_id="huyen-ai-engineering#a-1", content="Third by rank."),
    ]

    passages = map_search_results(_payload(results), limit=5)

    assert [p.chunk_id for p in passages] == [
        "book-text-rag:huyen-ai-engineering#z-9",
        "book-text-rag:huyen-ai-engineering#3",
        "book-text-rag:huyen-ai-engineering#a-1",
    ]
    assert [p.text for p in passages] == [
        "First by rank.",
        "Second by rank.",
        "Third by rank.",
    ]
    # L15 decision (a): BTRAG's document_id is not the graph's SourceNamespace.
    assert all(p.namespace is None for p in passages)


# ── 2. blank content / blank chunk_id are dropped ─────────────────────────────


def test_results_with_blank_content_or_blank_chunk_id_are_dropped() -> None:
    """S6/L15: blank content or blank chunk_id = unverifiable provenance → dropped."""
    results = [
        _result(1, chunk_id="keep#1", content="Verifiable prose."),
        _result(2, chunk_id="blank-content", content="   "),
        _result(3, chunk_id="", content="No chunk id, so no citation target."),
        _result(4, chunk_id="blank-id-only", content="\t"),
        _result(5, chunk_id="   ", content="Whitespace-only id."),
        _result(6, chunk_id="keep#6", content="Also verifiable."),
    ]

    passages = map_search_results(_payload(results), limit=10)

    assert [p.chunk_id for p in passages] == [
        "book-text-rag:keep#1",
        "book-text-rag:keep#6",
    ]


# ── 3. empty result set → (), no Passage constructed ──────────────────────────


def test_empty_resultados_yields_empty_tuple_without_constructing_a_passage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S6/L15 (c): an empty result set returns ``()``; no ``Passage`` is ever built."""
    constructed: list[Any] = []

    class _Spy:
        def __init__(self, **kwargs: Any) -> None:
            constructed.append(kwargs)
            raise AssertionError("no Passage may be constructed for an empty result set")

    monkeypatch.setattr(
        "book_graph_rag.infrastructure.orchestration.btr_text_passage_adapter.Passage",
        _Spy,
    )

    assert map_search_results(_payload([]), limit=5) == ()
    # Also when the key is missing/mistyped entirely — still nothing to map.
    assert map_search_results({"mode": "lexical"}, limit=5) == ()
    assert map_search_results({"resultados": "not-a-list"}, limit=5) == ()
    assert constructed == []


# ── 4. limit is honoured when the service over-delivers ───────────────────────


def test_limit_is_honoured_when_service_returns_more_than_asked() -> None:
    """S5/S13: the caller's k is the budget — at most ``limit`` passages out."""
    results = [
        _result(rank, chunk_id=f"doc#{rank}", content=f"Passage {rank}.") for rank in range(1, 9)
    ]

    passages = map_search_results(_payload(results), limit=3)

    assert len(passages) == 3
    assert [p.chunk_id for p in passages] == [
        "book-text-rag:doc#1",
        "book-text-rag:doc#2",
        "book-text-rag:doc#3",
    ]


# ── 5. fail-closed construction ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "token", "setting_named"),
    [
        pytest.param("", "a-real-bearer-token-value", "text_rag_url", id="blank-url"),
        pytest.param("   ", "a-real-bearer-token-value", "text_rag_url", id="whitespace-url"),
        pytest.param(_NEUTRAL_URL, "", "text_rag_token", id="empty-token"),
        pytest.param(_NEUTRAL_URL, "   ", "text_rag_token", id="space-token"),
    ],
)
def test_construction_fails_closed_naming_the_missing_setting(
    url: str, token: str, setting_named: str
) -> None:
    """L15 decision (c): blank url / empty or invisible token → raise at construction."""
    with pytest.raises(TextRagConfigurationError) as excinfo:
        BtrTextPassageAdapter(url, token)

    message = str(excinfo.value)
    assert setting_named in message, f"message must name the setting: {message}"
    # Never deferred to query time and never papered over with an empty tuple:
    # the failure happens before any query method can be called.
    assert "fails closed at construction" in message


def test_invisible_only_token_fails_closed() -> None:
    """L15 (c)/repo secret rule: zero-width characters are not a credential."""
    with pytest.raises(TextRagConfigurationError, match="text_rag_token"):
        BtrTextPassageAdapter(_NEUTRAL_URL, SecretStr("\u200b\u200b"))


def test_construction_accepts_secretstr_and_plain_str() -> None:
    """The token comes from configuration as SecretStr; a plain str is equally valid."""
    from_secret = BtrTextPassageAdapter(_NEUTRAL_URL, SecretStr("a-real-bearer-token-value"))
    from_str = BtrTextPassageAdapter(_NEUTRAL_URL, "a-real-bearer-token-value")
    assert from_secret._token == from_str._token == "a-real-bearer-token-value"


def test_constructor_no_longer_accepts_the_dead_top_k_knob() -> None:
    """L10/S13: the caller's ``limit`` is the only passage budget.

    The constructor's ``top_k`` was stored and never read — a knob that lied
    about having an effect — so it was removed rather than kept.
    """
    with pytest.raises(TypeError, match="top_k"):
        BtrTextPassageAdapter(  # type: ignore[call-arg]
            _NEUTRAL_URL, "a-real-bearer-token-value", top_k=5
        )


# ── 6. structuredContent preferred; text-JSON fallback maps identically ───────


def test_structured_content_preferred_and_text_json_fallback_produce_same_passages() -> None:
    """L15: both delivery shapes of the same payload map to byte-identical passages."""
    results = [
        _result(1, chunk_id="doc#1", content="Alpha passage."),
        _result(2, chunk_id="doc#2", content="Beta passage."),
    ]

    structured_payload = _payload_from_result(_structured(results))
    fallback_payload = _payload_from_result(_text_only(results))

    assert structured_payload is not None
    assert fallback_payload is not None
    # Preference order (docstring): structuredContent wins when both are present.
    both = CallToolResult(
        content=[TextContent(type="text", text='{"resultados": []}')],
        structuredContent=_payload(results),
    )
    assert _payload_from_result(both) == _payload(results)

    structured_passages = map_search_results(structured_payload, limit=5)
    fallback_passages = map_search_results(fallback_payload, limit=5)
    assert structured_passages == fallback_passages
    assert [p.chunk_id for p in structured_passages] == [
        "book-text-rag:doc#1",
        "book-text-rag:doc#2",
    ]


def test_text_block_without_a_json_object_maps_to_empty() -> None:
    """A text block exists but holds no JSON object → ``None`` payload → ``()``.

    Contrast with malformed JSON (raises, below), with a *successful* result
    carrying no text block at all (protocol surprise → ``RuntimeError``, below)
    and with ``isError``: an unusable text block is "nothing to map", a broken
    or absent payload shape is a protocol violation.
    """
    not_an_object = CallToolResult(content=[TextContent(type="text", text="[]")])
    assert _payload_from_result(not_an_object) is None


def test_malformed_json_text_propagates_instead_of_paging_over() -> None:
    """No swallowing (L15/transport posture): broken JSON raises, never returns ``()``."""
    broken = CallToolResult(content=[TextContent(type="text", text='{"resultados": [')])
    with pytest.raises(json.JSONDecodeError):
        _payload_from_result(broken)


def test_is_error_tool_result_raises_instead_of_returning_nothing() -> None:
    """An MCP tool error is a failure, not an empty result set."""
    error = CallToolResult(
        content=[TextContent(type="text", text="index unavailable")],
        isError=True,
    )
    with pytest.raises(RuntimeError, match="index unavailable"):
        _payload_from_result(error)


def test_success_with_neither_structured_content_nor_text_block_raises() -> None:
    """A protocol surprise must not read as "no passages" (defect 5).

    A successful call carrying neither ``structuredContent`` nor a text block
    (e.g. only image content) used to return ``()``, which downstream cannot
    tell apart from S6's insufficient coverage — it now raises ``RuntimeError``,
    consistently with how ``isError`` results are handled.
    """
    image_only = CallToolResult(
        content=[ImageContent(type="image", data="aGk=", mimeType="image/png")]
    )
    with pytest.raises(RuntimeError, match="structuredContent"):
        _payload_from_result(image_only)

    empty = CallToolResult(content=[])
    with pytest.raises(RuntimeError, match="protocol surprise"):
        _payload_from_result(empty)


# ── 7. config.py reads both settings ──────────────────────────────────────────


def test_settings_read_text_rag_url_and_token_from_payload(
    isolated_settings_factory: SettingsFactory,
) -> None:
    """Plain dict payload on the model — same style as the existing config tests.

    Built through ``isolated_settings_factory``, which chdirs into an empty
    ``tmp_path`` (the checkout's ``.env`` is invisible from there) and deletes
    every field-named env var before constructing: only the explicit payload
    decides, so the developer's real ``.env`` is never read by construction.
    """
    settings = isolated_settings_factory(
        text_rag_url="http://text-rag.test/mcp",
        text_rag_token="a-real-bearer-token-value",
    )
    assert settings.text_rag_url == "http://text-rag.test/mcp"
    assert isinstance(settings.text_rag_token, SecretStr)
    assert settings.text_rag_token.get_secret_value() == "a-real-bearer-token-value"


def test_settings_text_rag_defaults_are_empty_fail_closed(
    isolated_settings_factory: SettingsFactory,
) -> None:
    """No default host, no default token (AGENTS §3): both default to empty.

    Same isolation as the test above: without it this would assert about
    whatever the machine's ``.env`` and env vars happen to carry.
    """
    settings = isolated_settings_factory()
    assert settings.text_rag_url == ""
    assert settings.text_rag_token.get_secret_value() == ""


# ── 8. transport hygiene: no ambient env, MCP SDK timeout shape ───────────────


async def test_http_client_is_built_without_env_and_with_mcp_sdk_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``trust_env=False`` and an MCP-SDK-shaped timeouts object (defect 3).

    A bearer-token client must not consult ambient proxies or ``~/.netrc``
    (``trust_env=False``), and its read timeout must match the MCP SDK's own
    client (30 s connect, 300 s read) so a long-lived streamable-HTTP GET is
    not cut at 30 s. The whole transport chain — client, stream, session — is
    faked, so no socket is opened.
    """
    captured: dict[str, Any] = {}

    class _FakeAsyncClient:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        async def __aenter__(self) -> _FakeAsyncClient:
            return self

        async def __aexit__(self, *exc_info: Any) -> bool:
            return False

    @contextlib.asynccontextmanager
    async def _fake_streamable(
        url: str, *, http_client: Any
    ) -> AsyncIterator[tuple[object, object, None]]:
        yield object(), object(), None

    class _FakeSession:
        def __init__(self, *args: Any) -> None:
            pass

        async def __aenter__(self) -> _FakeSession:
            return self

        async def __aexit__(self, *exc_info: Any) -> bool:
            return False

        async def initialize(self) -> None:
            pass

        async def call_tool(self, name: str, arguments: dict[str, Any]) -> CallToolResult:
            payload = json.dumps(_payload([]))
            return CallToolResult(content=[TextContent(type="text", text=payload)])

    adapter_module = "book_graph_rag.infrastructure.orchestration.btr_text_passage_adapter"
    monkeypatch.setattr("httpx.AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(f"{adapter_module}.streamable_http_client", _fake_streamable)
    monkeypatch.setattr(f"{adapter_module}.ClientSession", _FakeSession)

    adapter = BtrTextPassageAdapter(_NEUTRAL_URL, "a-real-bearer-token-value")
    passages = await adapter.passages(_QUESTION, limit=3)

    assert passages == ()
    assert captured["trust_env"] is False
    timeout = captured["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.connect == 30.0  # MCP_DEFAULT_TIMEOUT
    assert timeout.read == 300.0  # MCP_DEFAULT_SSE_READ_TIMEOUT (long-lived GET)
    assert captured["headers"] == {"Authorization": "Bearer a-real-bearer-token-value"}
