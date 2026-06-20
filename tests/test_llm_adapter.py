"""Tests for LLMAdapter (AC-03.1, AC-03.4)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from instructor.v2.core.errors import InstructorRetryException
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion

from book_graph_rag.config import Settings
from book_graph_rag.domain.models import Book, Chapter, KnowledgeGraphChunk, PageRef, Section
from book_graph_rag.infrastructure.llm_adapter import LLMAdapter

_EXTRACTION_JSON = json.dumps(
    {
        "entities": [
            {
                "id": "e1",
                "name": "Agent Pattern",
                "type": "pattern",
            }
        ],
        "relationships": [],
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


class _FakeCompletions:
    """Records calls and supports fail-then-succeed behaviour."""

    def __init__(self, fail_count: int = 0) -> None:
        self.fail_count = fail_count
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> ChatCompletion:
        self.calls.append(kwargs)
        if len(self.calls) <= self.fail_count:
            raise RuntimeError(f"failure {len(self.calls)}")
        return ChatCompletion(
            id="fake",
            object="chat.completion",
            created=0,
            model="fake",
            choices=[
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": _EXTRACTION_JSON,
                    },
                    "finish_reason": "stop",
                }
            ],
        )


class _FakeChat:
    def __init__(self, completions: _FakeCompletions) -> None:
        self.completions = completions


class _FakeAsyncOpenAI(AsyncOpenAI):
    """AsyncOpenAI stand-in that bypasses network setup and returns fake completions."""

    def __init__(self, *, base_url: str, api_key: str, fail_count: int = 0) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.chat = _FakeChat(_FakeCompletions(fail_count=fail_count))


class _FakeAsyncOpenAIFactory:
    """Callable that produces configured _FakeAsyncOpenAI instances."""

    def __init__(self, fail_count: int = 0) -> None:
        self.fail_count = fail_count
        self.last_kwargs: dict[str, Any] | None = None
        self._last_instance: _FakeAsyncOpenAI | None = None

    def __call__(self, *, base_url: str, api_key: str) -> _FakeAsyncOpenAI:
        self.last_kwargs = {"base_url": base_url, "api_key": api_key}
        self._last_instance = _FakeAsyncOpenAI(
            base_url=base_url,
            api_key=api_key,
            fail_count=self.fail_count,
        )
        return self._last_instance

    @property
    def completions(self) -> _FakeCompletions | None:
        """Convenience accessor to the fake completions of the last instance."""
        if self._last_instance is None:
            return None
        return self._last_instance.chat.completions


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
    assert len(result.entities) == 1
    assert result.entities[0].id == "e1"
    assert factory.completions is not None
    assert len(factory.completions.calls) == 3
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

    assert factory.completions is not None
    assert len(factory.completions.calls) == 3
