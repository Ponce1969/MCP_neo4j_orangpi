"""LLMProviderPort implementation using instructor and AsyncOpenAI.

The adapter sends a chunk with editorial metadata to a local or remote
OpenAI-compatible LLM and populates the chunk's ``entities`` and
``relationships`` with the structured response.
"""

from __future__ import annotations

import re

import instructor
from openai import AsyncOpenAI
from pydantic import BaseModel, Field
from tenacity import AsyncRetrying, stop_after_attempt, wait_exponential

from book_graph_rag.config import Settings
from book_graph_rag.domain.models import (
    CommunitySummary,
    Entity,
    EntityType,
    KnowledgeGraphChunk,
    Relationship,
    RelationshipType,
)
from book_graph_rag.ports.cypher_generator_port import (
    CypherFailureContext,
    CypherGeneratorPort,
)
from book_graph_rag.ports.llm_port import LLMProviderPort
from book_graph_rag.ports.llm_summary_port import LLMSummaryPort

_SYSTEM_PROMPT_CYPHER = (
    "You are a Cypher expert for a Neo4j knowledge graph about agentic "
    "architectural patterns for multi-agent systems.\n\n"
    "Graph schema:\n"
    "{schema}\n\n"
    "Rules:\n"
    "- Generate a single Cypher query that answers the user's question.\n"
    "- Use ONLY the labels and relationship types in the schema.\n"
    "- The query MUST be read-only (MATCH/RETURN/WHERE/CALL db.index.*).\n"
    "- The query MUST include a LIMIT 100 clause.\n"
    "- If a previous query failed, fix it using the error message.\n"
)

_SYSTEM_PROMPT = (
    "You are a knowledge-graph extractor for a book on agentic architectural "
    "patterns for multi-agent systems.\n\n"
    "Your task is to extract entities and relationships from the provided book chunk.\n\n"
    "Allowed entity types: pattern, agent, component, concept, tool, framework, "
    "mcp, llmops, risk.\n"
    "Allowed relationship types: requires, alternative_to, composes, extends, "
    "enables, depends_on, contrasts_with, evolves_to.\n\n"
    "Output format:\n"
    "- Return entities with fields: name, type, description, source_page.\n"
    "- Return relationships with fields: source_entity_name, target_entity_name, "
    "type, description, source_page.\n"
    "- source_entity_name and target_entity_name must match entity names exactly.\n\n"
    "Rules:\n"
    "- Use ONLY the allowed types; do not invent new ones.\n"
    "- Set source_page to the chunk's starting page when the entity/relationship "
    "is mentioned there.\n"
    "- Keep descriptions concise but informative.\n"
    "- Relationships must connect entities that appear in the same chunk.\n"
)

_SUMMARY_SYSTEM_PROMPT = (
    "You are a technical summarizer for a book on agentic architectural patterns "
    "for multi-agent systems.\n\n"
    "Given a community of related entities from the knowledge graph, write a concise "
    "summary (500–1000 tokens) that explains what the community represents.\n"
    "Focus on the most important concepts, patterns, and relationships.\n"
)

_SCORE_SYSTEM_PROMPT = (
    "You are a relevance scorer for a knowledge graph question.\n\n"
    "Given a question and a community summary, return an integer score from 0 to 100 "
    "indicating how relevant the summary is to answering the question. "
    "100 means the summary directly answers the question; 0 means it is completely unrelated.\n"
)

_COMPOSE_SYSTEM_PROMPT = (
    "You are an answer composer for a knowledge graph question.\n\n"
    "Given a question and a ranked list of community summaries, compose a clear, "
    "concise answer. Cite each piece of information using the exact format:\n"
    "[Data: CommunitySummary(a1b2c3d4e5f6a7b8)]\n"
    "where the id is the 16-character community summary id shown next to each summary.\n"
)


class _LLMEntityDTO(BaseModel):
    """LLM-facing entity schema — no id field; the adapter computes it."""

    name: str
    type: EntityType
    description: str = ""
    source_page: int | None = None


class _LLMRelationshipDTO(BaseModel):
    """LLM-facing relationship schema — references by entity name, not id."""

    source_entity_name: str
    target_entity_name: str
    type: RelationshipType
    description: str = ""
    source_page: int | None = None


class _LLMExtraction(BaseModel):
    """Structured LLM output schema for graph extraction."""

    entities: list[_LLMEntityDTO] = Field(default_factory=list)
    relationships: list[_LLMRelationshipDTO] = Field(default_factory=list)


class _CypherResponse(BaseModel):
    """Structured LLM output schema for Cypher generation."""

    cypher: str


class _CommunitySummaryText(BaseModel):
    """Structured LLM output schema for a single community summary."""

    summary: str


class _CommunityScore(BaseModel):
    """Structured LLM output schema for summary relevance scoring."""

    score: int = Field(ge=0, le=100)


class _ComposedAnswer(BaseModel):
    """Structured LLM output schema for the final answer."""

    answer: str


class LLMAdapter(LLMProviderPort, CypherGeneratorPort, LLMSummaryPort):
    """Instructor + AsyncOpenAI implementation of ``LLMProviderPort``,
    ``CypherGeneratorPort`` and ``LLMSummaryPort``.
    """

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

        # Separate client for community-summary tasks, bound to the cheaper
        # community_model_name (which defaults to llm_model_name when unset).
        summary_model_name = settings.community_model_name or settings.llm_model_name
        self._summary_client = instructor.from_openai(
            AsyncOpenAI(base_url=settings.llm_base_url, api_key=api_key),
            mode=instructor.Mode.MD_JSON,
        )
        self._summary_model_name = summary_model_name

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

        chunk.entities = [
            Entity(
                id=self._slugify(dto.name),
                name=dto.name,
                type=dto.type,
                description=dto.description,
                source_page=dto.source_page,
            )
            for dto in extraction.entities
        ]
        chunk.relationships = [
            Relationship(
                source_entity_id=self._slugify(dto.source_entity_name),
                target_entity_id=self._slugify(dto.target_entity_name),
                type=dto.type,
                description=dto.description,
                source_page=dto.source_page,
            )
            for dto in extraction.relationships
        ]
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

    async def generate_cypher(
        self, schema: str, question: str, failure: CypherFailureContext | None
    ) -> str:
        """Generate a read-only Cypher query from ``question`` and ``schema``."""
        system_prompt = _SYSTEM_PROMPT_CYPHER.format(schema=schema)
        user_prompt = f"Question: {question}"
        failure_prompt = ""
        if failure is not None:
            failure_prompt = (
                f"The previous query failed:\n{failure.failed_cypher}\n\n"
                f"Error: {failure.error_message}\n\n"
                "Please fix it."
            )

        response: _CypherResponse | None = None
        async for attempt in self._retrying:
            with attempt:
                response = await self._client.create(
                    response_model=_CypherResponse,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                        {"role": "user", "content": failure_prompt},
                    ],
                    model=self._settings.llm_model_name,
                    max_retries=AsyncRetrying(stop=stop_after_attempt(1)),
                )

        if response is None:  # pragma: no cover
            raise RuntimeError("Cypher generation failed without raising")
        return response.cypher

    @staticmethod
    def _slugify(text: str) -> str:
        """Normalize a name into a stable URL-friendly identifier."""
        normalized = re.sub(r"[^a-z0-9]+", "-", text.lower().strip())
        return normalized.strip("-")

    async def generate_community_summary(
        self,
        entities: list[Entity],
        relationships: list[Relationship],
        level: int,
    ) -> str:
        """Generate a concise natural-language summary for a community."""
        entity_lines = "\n".join(
            f"- {entity.name} ({entity.type})" for entity in entities
        )
        relationship_lines = "\n".join(
            f"- {relationship.source_entity_id} --[{relationship.type}]--> "
            f"{relationship.target_entity_id}"
            for relationship in relationships
        )
        prompt = (
            f"Level: {level}\n\n"
            f"Entities:\n{entity_lines}\n\n"
            f"Relationships:\n{relationship_lines}\n\n"
            "Write a concise summary of this community."
        )

        response: _CommunitySummaryText | None = None
        async for attempt in self._retrying:
            with attempt:
                response = await self._summary_client.create(
                    response_model=_CommunitySummaryText,
                    messages=[
                        {"role": "system", "content": _SUMMARY_SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    model=self._summary_model_name,
                    max_retries=AsyncRetrying(stop=stop_after_attempt(1)),
                )

        if response is None:  # pragma: no cover
            raise RuntimeError("Community summary generation failed without raising")
        return response.summary

    async def score_community(self, question: str, summary: CommunitySummary) -> int:
        """Score a community summary for relevance to ``question``."""
        prompt = (
            f"Question: {question}\n\n"
            f"Community summary (id: {summary.id}):\n{summary.summary}\n\n"
            "Return a relevance score from 0 to 100."
        )

        response: _CommunityScore | None = None
        async for attempt in self._retrying:
            with attempt:
                response = await self._summary_client.create(
                    response_model=_CommunityScore,
                    messages=[
                        {"role": "system", "content": _SCORE_SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    model=self._summary_model_name,
                    max_retries=AsyncRetrying(stop=stop_after_attempt(1)),
                )

        if response is None:  # pragma: no cover
            raise RuntimeError("Community scoring failed without raising")
        return response.score

    async def compose_answer(
        self,
        question: str,
        ranked: list[tuple[CommunitySummary, int]],
    ) -> str:
        """Compose a final answer with citations from the ranked summaries."""
        summary_lines = "\n\n".join(
            f"[{i + 1}] id: {summary.id}\nscore: {score}\n{summary.summary}"
            for i, (summary, score) in enumerate(ranked)
        )
        prompt = (
            f"Question: {question}\n\n"
            "Ranked community summaries:\n\n"
            f"{summary_lines}\n\n"
            "Compose an answer that cites the summaries using "
            "[Data: CommunitySummary(id)]."
        )

        response: _ComposedAnswer | None = None
        async for attempt in self._retrying:
            with attempt:
                response = await self._summary_client.create(
                    response_model=_ComposedAnswer,
                    messages=[
                        {"role": "system", "content": _COMPOSE_SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    model=self._summary_model_name,
                    max_retries=AsyncRetrying(stop=stop_after_attempt(1)),
                )

        if response is None:  # pragma: no cover
            raise RuntimeError("Answer composition failed without raising")
        return response.answer
