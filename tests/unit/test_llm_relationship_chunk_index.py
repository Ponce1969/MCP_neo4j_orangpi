"""Regression test: LLMAdapter populates ``Relationship.chunk_index``.

The audit rule ``PROVENANCE_RELATIONSHIP_MISSING`` requires every ``:RELATED``
edge to carry both ``source_page`` and ``chunk_index``. ``LLMAdapter`` builds
``Relationship`` from the extraction DTOs and must copy the chunk's
``chunk_index`` alongside the LLM-provided ``source_page``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from openai.types.chat import ChatCompletion

from book_graph_rag.config import Settings
from book_graph_rag.domain.models import KnowledgeGraphChunk, PageRef
from book_graph_rag.domain.namespaces import SourceNamespace
from book_graph_rag.infrastructure.llm_adapter import LLMAdapter

_NAMESPACE = SourceNamespace(corpus="knowledge", source="agentic-architectural-patterns")

_EXTRACTION_JSON = json.dumps(
    {
        "entities": [
            {
                "name": "Agent Pattern",
                "type": "pattern",
                "description": "A reusable solution for agent construction.",
                "source_page": 4,
            },
            {
                "name": "Multi-Agent System",
                "type": "concept",
                "description": "A system composed of multiple interacting agents.",
                "source_page": 4,
            },
        ],
        "relationships": [
            {
                "source_entity_name": "Agent Pattern",
                "target_entity_name": "Multi-Agent System",
                "type": "composes",
                "description": "Agent patterns build multi-agent systems.",
                "source_page": 4,
            }
        ],
    }
)


def _make_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Build hermetic Settings without external environment variables."""
    monkeypatch.chdir(tmp_path)
    for var in ("NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    return Settings.model_validate(
        {
            "neo4j_uri": "bolt://localhost:7687",
            "neo4j_user": "neo4j",
            "neo4j_password": "secret",
            "graph_llm_api_key": "graph-test-key",
            "graph_llm_base_url": "https://graph.example.test/v1",
            "graph_llm_model_name": "graph-model",
            "query_llm_api_key": "query-test-key",
            "query_llm_base_url": "https://query.example.test/v1",
            "query_llm_model_name": "query-model",
        }
    )


def _make_chunk(chunk_index: int) -> KnowledgeGraphChunk:
    """Return a minimal chunk at an arbitrary, non-zero ``chunk_index``."""
    return KnowledgeGraphChunk(
        text="The agent pattern is fundamental to multi-agent systems.",
        chunk_index=chunk_index,
        page_ref=PageRef(start=4, end=5),
    )


def _make_completion(content: str) -> ChatCompletion:
    return ChatCompletion.model_validate(
        {
            "id": "fake",
            "object": "chat.completion",
            "created": 0,
            "model": "fake",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
        }
    )


class _FakeCompletions:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> ChatCompletion:
        self.calls.append(kwargs)
        return _make_completion(_EXTRACTION_JSON)


class _FakeChat:
    def __init__(self) -> None:
        self.completions = _FakeCompletions()


class _FakeAsyncOpenAI:
    def __init__(self, **kwargs: Any) -> None:
        self.chat = _FakeChat()


class _FakeAsyncOpenAIFactory:
    def __init__(self) -> None:
        self.instances: list[_FakeAsyncOpenAI] = []

    def __call__(self, **kwargs: Any) -> _FakeAsyncOpenAI:
        instance = _FakeAsyncOpenAI(**kwargs)
        self.instances.append(instance)
        return instance


async def test_extract_graph_propagates_chunk_index_to_relationships(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every extracted Relationship carries the chunk's ``chunk_index`` and the
    LLM-provided ``source_page``."""
    settings = _make_settings(tmp_path, monkeypatch)
    factory = _FakeAsyncOpenAIFactory()
    monkeypatch.setattr(
        "book_graph_rag.infrastructure.llm_adapter.AsyncOpenAI",
        factory,
    )

    adapter = LLMAdapter(settings, _NAMESPACE)
    chunk = _make_chunk(chunk_index=7)
    result = await adapter.extract_graph(chunk)

    assert result is chunk
    assert len(result.relationships) == 1

    relationship = result.relationships[0]
    assert relationship.source_page == 4
    assert relationship.chunk_index == 7
    assert relationship.chunk_index == chunk.chunk_index
