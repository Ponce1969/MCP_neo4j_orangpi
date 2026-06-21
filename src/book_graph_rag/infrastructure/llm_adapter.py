"""LLMProviderPort implementation using instructor and AsyncOpenAI.

The adapter sends a chunk with editorial metadata to a local or remote
OpenAI-compatible LLM and populates the chunk's ``entities`` and
``relationships`` with the structured response.
"""

from __future__ import annotations

import instructor
from openai import AsyncOpenAI
from pydantic import BaseModel, Field
from tenacity import AsyncRetrying, stop_after_attempt, wait_exponential

from book_graph_rag.config import Settings
from book_graph_rag.domain.models import Entity, KnowledgeGraphChunk, Relationship
from book_graph_rag.ports.llm_port import LLMProviderPort

_SYSTEM_PROMPT = (
    "You are a knowledge-graph extractor for a book on agentic architectural "
    "patterns for multi-agent systems.\n\n"
    "Your task is to extract entities and relationships from the provided book chunk.\n\n"
    "Allowed entity types: pattern, agent, component, concept, tool, framework, "
    "mcp, llmops, risk.\n"
    "Allowed relationship types: requires, alternative_to, composes, extends, "
    "enables, depends_on, contrasts_with, evolves_to.\n\n"
    "Rules:\n"
    "- Use ONLY the allowed types; do not invent new ones.\n"
    "- Set source_page to the chunk's starting page when the entity/relationship "
    "is mentioned there.\n"
    "- Keep descriptions concise but informative.\n"
    "- Relationships must connect entities that appear in the same chunk.\n"
)


class _LLMExtraction(BaseModel):
    """Structured LLM output schema for graph extraction."""

    entities: list[Entity] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)


class LLMAdapter(LLMProviderPort):
    """Instructor + AsyncOpenAI implementation of ``LLMProviderPort``."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

        # Handle None llm_api_key for local Ollama: the SDK requires a
        # non-None string, but Ollama ignores the value.
        api_key: str = (
            settings.llm_api_key.get_secret_value()
            if settings.llm_api_key is not None
            else "ollama"
        )

        raw_client = AsyncOpenAI(
            base_url=settings.llm_base_url,
            api_key=api_key,
        )
        self._client = instructor.from_openai(raw_client, mode=instructor.Mode.MD_JSON)

        # Retry policy captured at construction time.
        self._retrying = AsyncRetrying(
            stop=stop_after_attempt(settings.llm_max_retries),
            wait=wait_exponential(
                multiplier=settings.llm_retry_wait_multiplier,
                max=settings.llm_retry_wait_max,
            ),
            reraise=True,
        )

    async def extract_graph(self, chunk: KnowledgeGraphChunk) -> KnowledgeGraphChunk:
        """Extract entities/relationships from ``chunk`` and mutate it in place."""
        prompt_content = self._build_prompt(chunk)
        extraction: _LLMExtraction | None = None

        async for attempt in self._retrying:
            with attempt:
                extraction = await self._client.create(
                    response_model=_LLMExtraction,
                    messages=[
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user", "content": prompt_content},
                    ],
                    model=self._settings.llm_model_name,
                    # Disable instructor's internal retries; tenacity owns retry policy.
                    max_retries=AsyncRetrying(stop=stop_after_attempt(1)),
                )

        if extraction is None:  # pragma: no cover
            raise RuntimeError("LLM extraction failed without raising")

        chunk.entities = extraction.entities
        chunk.relationships = extraction.relationships
        return chunk

    def _build_prompt(self, chunk: KnowledgeGraphChunk) -> str:
        """Build the user prompt including bibliographic context."""
        context_parts: list[str] = []
        if chunk.book is not None:
            context_parts.append(f"Book: {chunk.book.title}")
        if chunk.chapter is not None:
            context_parts.append(f"Chapter: {chunk.chapter.title}")
        if chunk.section is not None:
            context_parts.append(f"Section: {chunk.section.title}")
        context_parts.append(f"Page range: {chunk.page_ref.start}-{chunk.page_ref.end}")

        return "\n".join(context_parts) + f"\n\nText:\n{chunk.text}"
