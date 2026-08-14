"""Tests for LLMAdapter (AC-03.1, AC-03.4)."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest
from instructor.v2.core.errors import InstructorRetryException
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion
from pydantic import BaseModel

from book_graph_rag.config import Settings
from book_graph_rag.domain.models import (
    Book,
    Chapter,
    CommunitySummary,
    Entity,
    KnowledgeGraphChunk,
    PageRef,
    Relationship,
    Section,
)
from book_graph_rag.infrastructure.llm_adapter import (
    LLMAdapter,
    _CypherResponse,
    _escape_json_string_control_chars,
)
from book_graph_rag.ports.cypher_generator_port import (
    CypherFailureContext,
    CypherGeneratorPort,
)
from book_graph_rag.ports.llm_summary_port import LLMSummaryPort

_EXTRACTION_JSON = json.dumps(
    {
        "entities": [
            {
                "name": "Agent Pattern",
                "type": "pattern",
                "description": "A reusable solution for agent construction.",
                "source_page": 1,
            },
            {
                "name": "Multi-Agent System",
                "type": "concept",
                "description": "A system composed of multiple interacting agents.",
                "source_page": 1,
            },
        ],
        "relationships": [
            {
                "source_entity_name": "Agent Pattern",
                "target_entity_name": "Multi-Agent System",
                "type": "composes",
                "description": "Agent patterns build multi-agent systems.",
                "source_page": 1,
            }
        ],
    }
)


def _make_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> Settings:
    """Build Settings in a hermetic tmp directory without external env vars."""
    monkeypatch.chdir(tmp_path)
    for var in ("NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    data = {
        "neo4j_uri": "bolt://localhost:7687",
        "neo4j_user": "neo4j",
        "neo4j_password": "secret",
    }
    data.update(overrides)
    return Settings.model_validate(data)


def _make_chunk() -> KnowledgeGraphChunk:
    """Return a minimal chunk with editorial metadata for extraction tests."""
    book = Book(
        id="agentic-patterns",
        title="Agentic Architectural Patterns",
        author="",
        pdf_path="/tmp/book.pdf",
        page_count=100,
    )
    chapter = Chapter(number=1, title="Introduction", page_start=1)
    section = Section(
        chapter_number=1,
        level=2,
        title="Why Multi-Agent Systems",
        page_start=1,
        parent_section_title=None,
    )
    return KnowledgeGraphChunk(
        text="The agent pattern is fundamental to multi-agent systems.",
        chunk_index=0,
        book=book,
        chapter=chapter,
        section=section,
        page_ref=PageRef(start=1, end=2),
    )


def _make_completion(content: str) -> ChatCompletion:
    """Build a minimal ChatCompletion carrying ``content`` as the assistant message."""
    return ChatCompletion(
        id="fake",
        object="chat.completion",
        created=0,
        model="fake",
        choices=[
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    )


class _FakeCompletions:
    """Records calls and supports fail-then-succeed behaviour."""

    def __init__(
        self, fail_count: int = 0, extraction_json: str | None = None
    ) -> None:
        self.fail_count = fail_count
        self.extraction_json = extraction_json or _EXTRACTION_JSON
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> ChatCompletion:
        self.calls.append(kwargs)
        if len(self.calls) <= self.fail_count:
            raise RuntimeError(f"failure {len(self.calls)}")
        return _make_completion(self.extraction_json)


class _FakeChat:
    def __init__(self, completions: _FakeCompletions) -> None:
        self.completions = completions


class _FakeAsyncOpenAI(AsyncOpenAI):
    """AsyncOpenAI stand-in that bypasses network setup and returns fake completions."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout: float = 60.0,
        max_retries: int = 0,
        fail_count: int = 0,
        extraction_json: str | None = None,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self.chat = _FakeChat(
            _FakeCompletions(
                fail_count=fail_count, extraction_json=extraction_json
            )
        )


class _FakeAsyncOpenAIFactory:
    """Callable that produces configured _FakeAsyncOpenAI instances."""

    def __init__(
        self, fail_count: int = 0, extraction_json: str | None = None
    ) -> None:
        self.fail_count = fail_count
        self.extraction_json = extraction_json
        self.last_kwargs: dict[str, Any] | None = None
        self._last_instance: _FakeAsyncOpenAI | None = None
        self._instances: list[_FakeAsyncOpenAI] = []

    def __call__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout: float = 60.0,
        max_retries: int = 0,
    ) -> _FakeAsyncOpenAI:
        self.last_kwargs = {
            "base_url": base_url,
            "api_key": api_key,
            "timeout": timeout,
            "max_retries": max_retries,
        }
        self._last_instance = _FakeAsyncOpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
            max_retries=max_retries,
            fail_count=self.fail_count,
            extraction_json=self.extraction_json,
        )
        self._instances.append(self._last_instance)
        return self._last_instance

    @property
    def completions(self) -> _FakeCompletions | None:
        """Convenience accessor to the fake completions of the last instance."""
        if self._last_instance is None:
            return None
        return self._last_instance.chat.completions

    @property
    def all_calls(self) -> list[list[Any]]:
        """Aggregate calls across all produced instances."""
        return [
            instance.chat.completions.calls
            for instance in self._instances
            if instance.chat.completions.calls
        ]


def test_llm_adapter_requires_settings() -> None:
    """AC-03.1: LLMAdapter requires Settings to construct."""
    with pytest.raises(TypeError):
        LLMAdapter()  # type: ignore[call-arg]


def test_llm_adapter_api_key_placeholder_when_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When llm_api_key is None the AsyncOpenAI client is built with api_key='ollama'."""
    settings = _make_settings(
        tmp_path,
        monkeypatch,
        llm_api_key=None,
        llm_base_url="http://localhost:11434/v1",
    )
    factory = _FakeAsyncOpenAIFactory()
    monkeypatch.setattr(
        "book_graph_rag.infrastructure.llm_adapter.AsyncOpenAI",
        factory,
    )

    LLMAdapter(settings)

    assert factory.last_kwargs is not None
    assert factory.last_kwargs["api_key"] == "ollama"
    assert factory.last_kwargs["base_url"] == settings.llm_base_url


async def test_llm_adapter_retries_with_exponential_backoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-03.4: transient failures are retried with exponential backoff."""
    settings = _make_settings(
        tmp_path,
        monkeypatch,
        llm_max_retries=3,
        llm_retry_wait_multiplier=1.0,
        llm_retry_wait_max=30.0,
    )
    factory = _FakeAsyncOpenAIFactory(fail_count=2)
    monkeypatch.setattr(
        "book_graph_rag.infrastructure.llm_adapter.AsyncOpenAI",
        factory,
    )

    sleep_delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleep_delays.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    adapter = LLMAdapter(settings)
    chunk = _make_chunk()
    result = await adapter.extract_graph(chunk)

    assert result is chunk
    assert len(result.entities) == 2
    assert result.entities[0].id == "agent-pattern"
    assert result.entities[1].id == "multi-agent-system"
    assert len(result.relationships) == 1
    assert result.relationships[0].source_entity_id == "agent-pattern"
    assert result.relationships[0].target_entity_id == "multi-agent-system"
    assert factory._instances
    assert len(factory._instances[0].chat.completions.calls) == 3
    assert sleep_delays == [1.0, 2.0]


async def test_llm_adapter_fails_after_max_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-03.4: after exhausting max_retries the last exception is re-raised."""
    settings = _make_settings(
        tmp_path,
        monkeypatch,
        llm_max_retries=3,
        llm_retry_wait_multiplier=1.0,
        llm_retry_wait_max=30.0,
    )
    factory = _FakeAsyncOpenAIFactory(fail_count=5)
    monkeypatch.setattr(
        "book_graph_rag.infrastructure.llm_adapter.AsyncOpenAI",
        factory,
    )

    async def _fake_sleep(delay: float) -> None:
        pass

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    adapter = LLMAdapter(settings)

    with pytest.raises(InstructorRetryException):
        await adapter.extract_graph(_make_chunk())

    assert factory._instances
    assert len(factory._instances[0].chat.completions.calls) == 3


async def test_llm_adapter_computes_entity_id_from_name_slug(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The adapter derives entity.id from slug(name), not from the LLM response."""
    settings = _make_settings(tmp_path, monkeypatch)
    factory = _FakeAsyncOpenAIFactory()
    monkeypatch.setattr(
        "book_graph_rag.infrastructure.llm_adapter.AsyncOpenAI",
        factory,
    )

    adapter = LLMAdapter(settings)
    result = await adapter.extract_graph(_make_chunk())

    assert result.entities[0].id == "agent-pattern"
    assert result.entities[0].name == "Agent Pattern"


async def test_llm_adapter_computes_relationship_ids_from_entity_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The adapter derives relationship ids from slug(entity names)."""
    settings = _make_settings(tmp_path, monkeypatch)
    factory = _FakeAsyncOpenAIFactory()
    monkeypatch.setattr(
        "book_graph_rag.infrastructure.llm_adapter.AsyncOpenAI",
        factory,
    )

    adapter = LLMAdapter(settings)
    result = await adapter.extract_graph(_make_chunk())

    relationship = result.relationships[0]
    assert relationship.source_entity_id == "agent-pattern"
    assert relationship.target_entity_id == "multi-agent-system"


# ── Canonicalization (REQ-CANON-01/02/05, AC-CANON-01/04) ───────────────────


def test_resolve_entity_id_legacy_path_no_canonical() -> None:
    """AC-CANON-01: entities without canonical_name keep id == slugify(name)."""
    entity_id, aliases = LLMAdapter._resolve_entity_id(
        name="Agent Pattern",
        canonical_name=None,
        aliases=[],
        entity_type="pattern",
    )
    assert entity_id == "agent-pattern"
    assert aliases == []


def test_resolve_entity_id_legacy_path_ignores_aliases() -> None:
    """AC-CANON-01: aliases alone do not change the legacy slugify(name) id."""
    entity_id, aliases = LLMAdapter._resolve_entity_id(
        name="Model Context Protocol",
        canonical_name=None,
        aliases=["MCP"],
        entity_type="concept",
    )
    assert entity_id == "model-context-protocol"
    assert aliases == ["MCP"]


def test_resolve_entity_id_canonical_appends_type() -> None:
    """AC-CANON-01: canonical_name produces type-aware id."""
    entity_id, aliases = LLMAdapter._resolve_entity_id(
        name="MCP",
        canonical_name="Model Context Protocol",
        aliases=["MCP"],
        entity_type="concept",
    )
    assert entity_id == "model-context-protocol-concept"
    assert aliases == ["MCP"]


def test_resolve_entity_id_type_aware_distinct_nodes() -> None:
    """REQ-CANON-05: same canonical name with different types stays distinct."""
    tool_id, _ = LLMAdapter._resolve_entity_id(
        name="MCP",
        canonical_name="Model Context Protocol",
        aliases=[],
        entity_type="tool",
    )
    concept_id, _ = LLMAdapter._resolve_entity_id(
        name="MCP",
        canonical_name="Model Context Protocol",
        aliases=[],
        entity_type="concept",
    )
    assert tool_id == "model-context-protocol-tool"
    assert concept_id == "model-context-protocol-concept"
    assert tool_id != concept_id


def test_resolve_entity_id_filters_stoplist() -> None:
    """AC-CANON-04: stoplisted aliases are removed before persistence."""
    entity_id, aliases = LLMAdapter._resolve_entity_id(
        name="Model Context Protocol",
        canonical_name=None,
        aliases=["MCP", "protocol", "model"],
        entity_type="concept",
        stoplist=["protocol", "MODEL"],
    )
    assert entity_id == "model-context-protocol"
    assert aliases == ["MCP"]


def test_resolve_entity_id_deduplicates_aliases_case_insensitive() -> None:
    """Aliases are deduplicated case-insensitively while preserving first form."""
    _, aliases = LLMAdapter._resolve_entity_id(
        name="Model Context Protocol",
        canonical_name=None,
        aliases=["MCP", "mcp", "MCP"],
        entity_type="concept",
    )
    assert aliases == ["MCP"]


_EXTRACTION_CANONICAL_JSON = json.dumps(
    {
        "entities": [
            {
                "name": "MCP",
                "type": "concept",
                "description": "A protocol for model context.",
                "source_page": 10,
                "aliases": ["MCP", "Model Context Protocol"],
                "canonical_name": "Model Context Protocol",
            }
        ],
        "relationships": [],
    }
)


async def test_extract_graph_populates_aliases_and_canonical_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REQ-CANON-02: extracted entities carry aliases and canonical_name."""
    settings = _make_settings(tmp_path, monkeypatch)
    factory = _FakeAsyncOpenAIFactory(extraction_json=_EXTRACTION_CANONICAL_JSON)
    monkeypatch.setattr(
        "book_graph_rag.infrastructure.llm_adapter.AsyncOpenAI",
        factory,
    )

    adapter = LLMAdapter(settings)
    chunk = _make_chunk()
    result = await adapter.extract_graph(chunk)

    assert len(result.entities) == 1
    entity = result.entities[0]
    assert entity.id == "model-context-protocol-concept"
    assert entity.name == "MCP"
    assert entity.canonical_name == "Model Context Protocol"
    assert entity.aliases == ["MCP", "Model Context Protocol"]


async def test_llm_adapter_generate_cypher_returns_cypher_string(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """generate_cypher returns the cypher string extracted from the LLM response."""
    settings = _make_settings(tmp_path, monkeypatch)
    adapter = LLMAdapter(settings)

    async def fake_create(*args: Any, **kwargs: Any) -> _CypherResponse:
        return _CypherResponse(cypher="MATCH (n:Entity) RETURN n LIMIT 100")

    monkeypatch.setattr(adapter._client, "create", fake_create)

    cypher = await adapter.generate_cypher("schema", "question", None)

    assert cypher == "MATCH (n:Entity) RETURN n LIMIT 100"


async def test_llm_adapter_generate_cypher_prompt_includes_schema_and_question(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The LLM prompt contains the schema and the user question."""
    settings = _make_settings(tmp_path, monkeypatch)
    adapter = LLMAdapter(settings)
    captured: dict[str, Any] = {}

    async def fake_create(*args: Any, **kwargs: Any) -> _CypherResponse:
        captured.update(kwargs)
        return _CypherResponse(cypher="MATCH (n) RETURN n LIMIT 100")

    monkeypatch.setattr(adapter._client, "create", fake_create)

    await adapter.generate_cypher("(:Entity)-[:RELATED]->(:Entity)", "find patterns", None)

    messages = captured["messages"]
    assert any("(:Entity)-[:RELATED]->(:Entity)" in msg["content"] for msg in messages)
    assert any("find patterns" in msg["content"] for msg in messages)


async def test_llm_adapter_generate_cypher_includes_failure_context_on_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On retry the prompt includes the failed query and the Neo4j error."""
    settings = _make_settings(tmp_path, monkeypatch)
    adapter = LLMAdapter(settings)
    captured: dict[str, Any] = {}

    async def fake_create(*args: Any, **kwargs: Any) -> _CypherResponse:
        captured.update(kwargs)
        return _CypherResponse(cypher="MATCH (n) RETURN n LIMIT 100")

    monkeypatch.setattr(adapter._client, "create", fake_create)

    failure = CypherFailureContext(failed_cypher="BAD", error_message="syntax error")
    await adapter.generate_cypher("schema", "question", failure)

    messages = captured["messages"]
    content = "\n".join(msg["content"] for msg in messages)
    assert "BAD" in content
    assert "syntax error" in content


async def test_llm_adapter_generate_cypher_system_prompt_demands_limit_100(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The system prompt instructs the LLM to always include LIMIT 100."""
    settings = _make_settings(tmp_path, monkeypatch)
    adapter = LLMAdapter(settings)
    captured: dict[str, Any] = {}

    async def fake_create(*args: Any, **kwargs: Any) -> _CypherResponse:
        captured.update(kwargs)
        return _CypherResponse(cypher="MATCH (n) RETURN n LIMIT 100")

    monkeypatch.setattr(adapter._client, "create", fake_create)

    await adapter.generate_cypher("schema", "question", None)

    messages = captured["messages"]
    system_content = next(msg["content"] for msg in messages if msg["role"] == "system")
    assert "LIMIT 100" in system_content


def test_llm_adapter_implements_cypher_generator_port(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLMAdapter is a concrete implementation of CypherGeneratorPort."""
    settings = _make_settings(tmp_path, monkeypatch)
    adapter = LLMAdapter(settings)
    assert isinstance(adapter, CypherGeneratorPort)


# ── LLMSummaryPort implementation (REQ-GR.1 PR3) ───────────────────────────────


def test_llm_adapter_implements_llm_summary_port(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLMAdapter is a concrete implementation of LLMSummaryPort."""
    settings = _make_settings(tmp_path, monkeypatch)
    adapter = LLMAdapter(settings)
    assert isinstance(adapter, LLMSummaryPort)


async def test_llm_adapter_summary_client_uses_community_model_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The summary client is built with the dedicated community_model_name."""
    settings = _make_settings(
        tmp_path,
        monkeypatch,
        community_model_name="gpt-4.1-mini",
    )
    factory = _FakeAsyncOpenAIFactory()
    monkeypatch.setattr(
        "book_graph_rag.infrastructure.llm_adapter.AsyncOpenAI",
        factory,
    )

    LLMAdapter(settings)

    assert factory.last_kwargs is not None
    # Two clients are built: extraction/cypher and summary.


async def test_generate_community_summary_prompt_includes_entities_and_level(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The summary prompt mentions the community entities, relationships and level."""
    settings = _make_settings(tmp_path, monkeypatch)
    adapter = LLMAdapter(settings)
    captured: dict[str, Any] = {}

    async def fake_create(**kwargs: Any) -> ChatCompletion:
        captured.update(kwargs)
        return _make_completion("Generated summary.")

    monkeypatch.setattr(
        adapter._summary_raw_client.chat.completions, "create", fake_create
    )

    entities = [
        Entity(id="e1", name="Agent Pattern", type="pattern"),
        Entity(id="e2", name="MCP", type="mcp"),
    ]
    relationships = [
        Relationship(
            source_entity_id="e1",
            target_entity_id="e2",
            type="enables",
        )
    ]
    result = await adapter.generate_community_summary(entities, relationships, level=2)

    assert result == "Generated summary."
    messages = captured["messages"]
    content = "\n".join(msg["content"] for msg in messages)
    assert "Agent Pattern" in content
    assert "MCP" in content
    assert "enables" in content
    assert "level: 2" in content.lower()
    assert captured["model"] == settings.community_model_name


async def test_score_community_returns_parsed_score(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """score_community returns the integer score from the LLM response."""
    settings = _make_settings(tmp_path, monkeypatch)
    adapter = LLMAdapter(settings)
    captured: dict[str, Any] = {}

    class _FakeScore(BaseModel):
        score: int

    async def fake_create(*args: Any, **kwargs: Any) -> _FakeScore:
        captured.update(kwargs)
        return _FakeScore(score=85)

    monkeypatch.setattr(adapter._summary_client, "create", fake_create)

    summary = CommunitySummary(level=1, summary="A summary", entity_ids=["e1"], parent_id="p1")
    score = await adapter.score_community("what is MCP?", summary)

    assert score == 85
    messages = captured["messages"]
    content = "\n".join(msg["content"] for msg in messages)
    assert "what is MCP?" in content
    assert "A summary" in content
    assert captured["model"] == settings.community_model_name


async def test_compose_answer_prompt_requires_citation_format(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The compose prompt instructs the LLM to emit [Data: CommunitySummary(id)] citations."""
    settings = _make_settings(tmp_path, monkeypatch)
    adapter = LLMAdapter(settings)
    captured: dict[str, Any] = {}

    async def fake_create(**kwargs: Any) -> ChatCompletion:
        captured.update(kwargs)
        return _make_completion("MCP is a protocol.")

    monkeypatch.setattr(
        adapter._summary_raw_client.chat.completions, "create", fake_create
    )

    summary = CommunitySummary(level=1, summary="A summary", entity_ids=["e1"], parent_id="p1")
    ranked = [(summary, 85)]
    result = await adapter.compose_answer("what is MCP?", ranked)

    assert result == "MCP is a protocol."
    messages = captured["messages"]
    content = "\n".join(msg["content"] for msg in messages)
    assert re.search(r"\[Data: CommunitySummary\([a-f0-9]{16}\)\]", content)
    assert summary.id in content
    assert captured["model"] == settings.community_model_name


async def test_plain_text_preserves_raw_newlines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plain-text prose keeps raw newlines: the JSON control-char class cannot occur."""
    settings = _make_settings(tmp_path, monkeypatch)
    adapter = LLMAdapter(settings)

    async def fake_create(**kwargs: Any) -> ChatCompletion:
        return _make_completion("line 1\n\nline 2")

    monkeypatch.setattr(
        adapter._summary_raw_client.chat.completions, "create", fake_create
    )

    summary = CommunitySummary(level=1, summary="s", entity_ids=["e1"], parent_id="p1")
    result = await adapter.compose_answer("q?", [(summary, 85)])

    assert result == "line 1\n\nline 2"


async def test_plain_text_strips_stale_markdown_fence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale markdown fence around the plain answer is stripped defensively."""
    settings = _make_settings(tmp_path, monkeypatch)
    adapter = LLMAdapter(settings)

    async def fake_create(**kwargs: Any) -> ChatCompletion:
        return _make_completion("```markdown\nMCP is a protocol.\n```")

    monkeypatch.setattr(
        adapter._summary_raw_client.chat.completions, "create", fake_create
    )

    summary = CommunitySummary(level=1, summary="s", entity_ids=["e1"], parent_id="p1")
    result = await adapter.compose_answer("q?", [(summary, 85)])

    assert result == "MCP is a protocol."


# ── _escape_json_string_control_chars ──────────────────────────────────────


def test_escape_json_string_control_chars_escapes_raw_control_chars() -> None:
    """Raw newlines/tabs inside JSON strings are rewritten to \\uXXXX escapes."""
    raw = (
        '{"answer": "line 1\nline 2\tok [Data: CommunitySummary(4edef17af58d735f)]", '
        '"n": 1}'
    )
    sanitized = _escape_json_string_control_chars(raw)

    assert "line 1" in sanitized
    assert "line 2" in sanitized
    assert json.loads(sanitized)["answer"] == (
        "line 1\nline 2\tok [Data: CommunitySummary(4edef17af58d735f)]"
    )


def test_escape_json_string_control_chars_preserves_escapes_and_structure() -> None:
    """Already-escaped sequences and whitespace outside strings stay untouched."""
    raw = '{\n  "a": "x\\ny\\\\z", "b": [1, 2], "c": "tail"\n}'
    sanitized = _escape_json_string_control_chars(raw)

    assert sanitized == raw
    assert json.loads(sanitized) == {"a": "x\ny\\z", "b": [1, 2], "c": "tail"}


def test_escape_json_string_control_chars_handles_nul_bytes() -> None:
    """NUL bytes inside strings are escaped so the payload still parses."""
    raw = '{"answer": "a\x00b"}'
    sanitized = _escape_json_string_control_chars(raw)

    assert "\x00" not in sanitized
    assert json.loads(sanitized)["answer"] == "a\x00b"
