"""LLMProviderPort implementation using instructor and AsyncOpenAI.

The adapter sends a chunk with editorial metadata to a local or remote
OpenAI-compatible LLM and populates the chunk's ``entities`` and
``relationships`` with the structured response.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Collection, Iterable
from typing import Any, cast

import instructor
from openai import AsyncOpenAI
from pydantic import BaseModel, Field
from tenacity import AsyncRetrying, before_sleep_log, stop_after_attempt, wait_exponential

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

logger = logging.getLogger(__name__)

_TERMINOLOGY_MAPPING = (
    "Terminology mapping (natural language → schema):\n"
    "- \"agente\" / \"agent\" → :Entity {type: 'agent'}\n"
    "- \"patrón\" / \"pattern\" → :Entity {type: 'pattern'}\n"
    "- \"componente\" / \"component\" → :Entity {type: 'component'}\n"
    "- \"concepto\" / \"concept\" → :Entity {type: 'concept'}\n"
    "- \"herramienta\" / \"tool\" → :Entity {type: 'tool'}\n"
    "- \"framework\" → :Entity {type: 'framework'}\n"
    "- \"MCP\" → :Entity {type: 'mcp'}\n"
    "- \"LLMOps\" / \"MLOps\" → :Entity {type: 'llmops'}\n"
    "- \"riesgo\" / \"risk\" / \"vulnerability\" → :Entity {type: 'risk'}\n"
    "- \"usa\" / \"needs\" / \"requires\" → "
    "[:RELATED {type: 'requires'}]\n"
    "- \"alternativa a\" / \"vs\" / \"alternative to\" → "
    "[:RELATED {type: 'alternative_to'}]\n"
    "- \"compone\" / \"part of\" / \"composes\" → "
    "[:RELATED {type: 'composes'}]\n"
    "- \"extiende\" / \"inherits\" / \"extends\" → "
    "[:RELATED {type: 'extends'}]\n"
    "- \"habilita\" / \"enables\" / \"allows\" → "
    "[:RELATED {type: 'enables'}]\n"
    "- \"depende de\" / \"depends on\" → "
    "[:RELATED {type: 'depends_on'}]\n"
    "- \"contrasta con\" / \"differs from\" / \"contrasts with\" → "
    "[:RELATED {type: 'contrasts_with'}]\n"
    "- \"evoluciona a\" / \"evolves to\" → "
    "[:RELATED {type: 'evolves_to'}]\n"
)

_SYSTEM_PROMPT_CYPHER = (
    "You are a Cypher expert for a Neo4j knowledge graph about agentic "
    "architectural patterns for multi-agent systems.\n\n"
    "Graph schema:\n"
    "{schema}\n\n"
    "{terminology_mapping}\n\n"
    "Rules:\n"
    "- Generate a single Cypher query that answers the user's question.\n"
    "- Use ONLY the labels and relationship types in the schema.\n"
    "- The query MUST be read-only (MATCH/RETURN/WHERE/CALL db.index.*).\n"
    "- The query MUST include a LIMIT 100 clause.\n"
    "- If a previous query failed, fix it using the error message.\n"
    "- Emit valid JSON: escape newlines inside string values as \\n, "
    "never raw control characters.\n"
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
    "- Return entities with fields: name, type, description, source_page, "
    "aliases, canonical_name.\n"
    "- Return relationships with fields: source_entity_name, target_entity_name, "
    "type, description, source_page.\n"
    "- source_entity_name and target_entity_name must match entity names exactly.\n"
    "- aliases: list of alternative names or abbreviations for the entity "
    "(e.g. [\"MCP\"] for \"Model Context Protocol\"). Omit when none.\n"
    "- canonical_name: the canonical or long-form name when it differs from "
    "the extracted name. Omit when it is the same as name.\n\n"
    "Rules:\n"
    "- Use ONLY the allowed types; do not invent new ones.\n"
    "- Set source_page to the chunk's starting page when the entity/relationship "
    "is mentioned there.\n"
    "- Keep descriptions concise but informative.\n"
    "- Every relationship must connect two entities you extracted from the same "
    "chunk, and must be directly supported by the text (never invent links).\n"
    "- Connect EVERY extracted entity to at least one other entity in the chunk. "
    "Orphaned entities (extracted but never related) lose their place in the graph "
    "and cannot be clustered, so prefer emitting a plausible, text-grounded "
    "relationship for each one - even for secondary or supporting concepts. If a "
    "link is loose, pick the closest valid type (e.g. composes/requires/enables) "
    "instead of leaving it unconnected.\n"
    "- Emit valid JSON: escape newlines inside string values as \\n, "
    "never raw control characters.\n"
)

_SUMMARY_SYSTEM_PROMPT = (
    "You are a technical summarizer for a book on agentic architectural patterns "
    "for multi-agent systems.\n\n"
    "Given a community of related entities from the knowledge graph, write a concise "
    "summary (500–1000 tokens) that explains what the community represents.\n"
    "Focus on the most important concepts, patterns, and relationships.\n\n"
    "Respond with ONLY the summary text: plain text, no JSON, no markdown fences, "
    "no headings."
)

_SCORE_SYSTEM_PROMPT = (
    "You are a relevance scorer for a knowledge graph question.\n\n"
    "Given a question and a community summary, return an integer score from 0 to 100 "
    "indicating how relevant the summary is to answering the question. "
    "100 means the summary directly answers the question; 0 means it is completely unrelated.\n"
    "Emit valid JSON: escape newlines inside string values as \\n, never raw control characters.\n"
)

_COMPOSE_SYSTEM_PROMPT = (
    "You are an answer composer for a knowledge graph question.\n\n"
    "Given a question and a ranked list of community summaries, compose a clear, "
    "concise answer. Cite each piece of information using the exact format:\n"
    "[Data: CommunitySummary(a1b2c3d4e5f6a7b8)]\n"
    "where the id is the 16-character community summary id shown next to each summary.\n"
    "Respond with ONLY the answer text: plain text, no JSON, no markdown fences.\n"
)

# Some OpenAI-compatible deployments (e.g. DeepSeek-family models on NVIDIA NIM)
# occasionally emit RAW control characters — newlines, tabs, sometimes even NUL
# bytes — inside JSON string values instead of the escaped ``\uXXXX`` forms the
# JSON spec requires.  Both ``json.loads`` and pydantic-core (jiter) reject such
# payloads with "Invalid JSON: control character found while parsing a string".
# Instructor's auto-repair cannot recover from this because the model re-emits
# the same invalid bytes on retry (greedy sampling).  We therefore sanitize the
# completion content BEFORE instructor parses it, making structured calls
# deterministic instead of flaky.
_JSON_STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*"')
_JSON_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f]")


def _escape_json_string_control_chars(content: str) -> str:
    """Escape raw control characters that appear inside JSON string values.

    Only characters inside double-quoted JSON strings are rewritten to their
    ``\\uXXXX`` escape form.  Whitespace and structure outside strings (which
    are valid JSON and must stay untouched) are preserved as-is.
    """

    def _escape(match: re.Match[str]) -> str:
        return _JSON_CONTROL_CHAR_RE.sub(
            lambda m: f"\\u{ord(m.group()):04x}", match.group()
        )

    return _JSON_STRING_RE.sub(_escape, content)


def _build_instructor_client(
    client: AsyncOpenAI, mode: instructor.Mode
) -> instructor.AsyncInstructor:
    """Build an instructor client whose completions are sanitized before parsing.

    Equivalent to ``instructor.from_openai(client, mode=mode)`` except the
    underlying ``chat.completions.create`` wrapper escapes raw control
    characters inside JSON string values first (see
    ``_escape_json_string_control_chars``), so structured responses survive
    models that emit slightly-invalid JSON.
    """

    raw_create = client.chat.completions.create

    async def _sanitizing_create(*args: Any, **kwargs: Any) -> Any:
        completion = await raw_create(*args, **kwargs)
        for choice in completion.choices:
            message = choice.message
            if message is not None and message.content is not None:
                message.content = _escape_json_string_control_chars(message.content)
        return completion

    return instructor.AsyncInstructor(
        client=client,
        create=cast(Callable[..., Any], instructor.patch(create=_sanitizing_create, mode=mode)),
        mode=mode,
    )


def _plain_text_messages(
    system: str, user: str
) -> list[dict[str, str]]:
    """Build the standard system/user message pair for plain-text calls."""
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


class _LLMEntityDTO(BaseModel):
    """LLM-facing entity schema — no id field; the adapter computes it."""

    name: str
    type: EntityType
    description: str = ""
    source_page: int | None = None
    aliases: list[str] = Field(default_factory=list)
    canonical_name: str | None = None


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


class _CommunityScore(BaseModel):
    """Structured LLM output schema for summary relevance scoring."""

    score: int = Field(ge=0, le=100)


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

        # Hard network deadline: without it a stalled API response hangs forever
        # (no timeout => the coroutine never returns). 60s covers LLM + parse.
        # max_retries=0: the OpenAI SDK default is 2, which would retry transport
        # errors IMMEDIATELY on top of instructor's and tenacity's retries,
        # producing a burst of calls that worsens 503/429 saturation. Tenacity
        # (self._retrying) is the sole authority for transport retries, with
        # exponential backoff.
        raw_client = AsyncOpenAI(
            base_url=settings.llm_base_url,
            api_key=api_key,
            timeout=60.0,
            max_retries=0,
        )
        self._client = _build_instructor_client(raw_client, instructor.Mode.MD_JSON)

        # Separate client for community-summary tasks, bound to the cheaper
        # community_model_name (which defaults to llm_model_name when unset).
        summary_model_name = settings.community_model_name or settings.llm_model_name
        self._summary_raw_client = AsyncOpenAI(
            base_url=settings.llm_base_url,
            api_key=api_key,
            timeout=60.0,
            max_retries=0,
        )
        # JSON-mode instructor client, used ONLY for structured outputs
        # (community scoring). Prose outputs (summaries, composed answers) go
        # through _plain_text on the same raw client: they need no JSON
        # envelope, and plain text is immune to the raw-control-character
        # failures (see the note above _escape_json_string_control_chars).
        self._summary_client = _build_instructor_client(
            self._summary_raw_client,
            instructor.Mode.JSON,
        )
        self._summary_model_name = summary_model_name
        # Max input tokens per summary call; larger communities are chunked.
        self._summary_chunk_tokens = settings.summary_chunk_tokens
        # Instructor internal retries for JSON self-healing. A malformed JSON
        # response (trailing characters, etc.) is re-prompted with the parse
        # error so instructor can recover instead of raising immediately.
        self._instructor_retries = settings.llm_instructor_max_retries

        # Retry policy captured at construction time.
        self._retrying = AsyncRetrying(
            stop=stop_after_attempt(settings.llm_max_retries),
            wait=wait_exponential(
                multiplier=settings.llm_retry_wait_multiplier,
                max=settings.llm_retry_wait_max,
            ),
            before_sleep=before_sleep_log(logger, logging.WARNING),
            reraise=True,
        )

    async def _plain_text(self, *, messages: list[Any], model: str) -> str:
        """Run one plain-text completion, then normalize the response.

        Prose outputs (composed answers, community summaries) do not need a
        JSON envelope: asking for JSON forces the model to escape newlines
        inside string values, which is exactly where DeepSeek-class models
        fail (raw control characters). Plain text keeps raw newlines valid,
        so that entire failure class cannot occur. A stale markdown fence is
        stripped defensively.
        """
        content: str | None = None
        async for attempt in self._retrying:
            with attempt:
                completion = await self._summary_raw_client.chat.completions.create(
                    model=model,
                    messages=messages,
                )
                content = completion.choices[0].message.content

        if content is None:
            raise RuntimeError("Plain-text LLM completion returned empty content")

        content = content.strip()
        if content.startswith("```"):
            lines = content.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            content = "\n".join(lines).strip()
        return content

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
                    # Instructor internal retries disabled; tenacity owns the
                    # retry policy (stop_after_attempt in self._retrying).
                    max_retries=self._instructor_retries,
                )

        if extraction is None:  # pragma: no cover
            raise RuntimeError("LLM extraction failed without raising")

        chunk.entities = []
        for dto in extraction.entities:
            entity_id, filtered_aliases = self._resolve_entity_id(
                dto.name, dto.canonical_name, dto.aliases, dto.type
            )
            chunk.entities.append(
                Entity(
                    id=entity_id,
                    name=dto.name,
                    type=dto.type,
                    description=dto.description,
                    source_page=dto.source_page,
                    aliases=filtered_aliases,
                    canonical_name=dto.canonical_name,
                )
            )
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
        system_prompt = _SYSTEM_PROMPT_CYPHER.format(
            schema=schema, terminology_mapping=_TERMINOLOGY_MAPPING
        )
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
                    # Instructor internal retries disabled; tenacity owns the
                    # retry policy (stop_after_attempt in self._retrying).
                    max_retries=self._instructor_retries,
                )

        if response is None:  # pragma: no cover
            raise RuntimeError("Cypher generation failed without raising")
        return response.cypher

    @staticmethod
    def _slugify(text: str) -> str:
        """Normalize a name into a stable URL-friendly identifier."""
        normalized = re.sub(r"[^a-z0-9]+", "-", text.lower().strip())
        return normalized.strip("-")

    @staticmethod
    def _resolve_entity_id(
        name: str,
        canonical_name: str | None,
        aliases: Iterable[str],
        entity_type: EntityType,
        stoplist: Collection[str] | None = None,
    ) -> tuple[str, list[str]]:
        """Return a deterministic entity id and filtered alias list.

        Compatibility rule (REQ-CANON-01):
        - If ``canonical_name`` is present, the id is ``slugify(canonical_name)-type``.
        - Otherwise the id is ``slugify(name)`` (legacy behavior, no type suffix).

        Aliases are normalized (case-insensitive deduplication) and filtered
        against ``stoplist`` so domain stopwords cannot become aliases.
        """
        stoplist_set = {term.lower() for term in (stoplist or ())}
        filtered: list[str] = []
        seen: set[str] = set()
        for alias in aliases:
            lower = alias.lower()
            if lower in stoplist_set or lower in seen:
                continue
            seen.add(lower)
            filtered.append(alias)

        if canonical_name:
            return f"{LLMAdapter._slugify(canonical_name)}-{entity_type}", filtered
        return LLMAdapter._slugify(name), filtered

    async def generate_community_summary(
        self,
        entities: list[Entity],
        relationships: list[Relationship],
        level: int,
    ) -> str:
        """Generate a concise natural-language summary for a community.

        Large communities (e.g. level 0 = whole graph, or a 2000-entity blob)
        would overflow the model context window in a single call.  To avoid that
        we split the community into character-budgeted chunks, summarize each
        chunk, and recursively combine the chunk summaries until the result fits
        one call.  This is the map-reduce pattern: no single LLM call ever
        exceeds ``summary_chunk_tokens``.
        """
        entity_lines = [
            f"- {entity.name} ({entity.type})" for entity in entities
        ]
        relationship_lines = [
            f"- {relationship.source_entity_id} --[{relationship.type}]--> "
            f"{relationship.target_entity_id}"
            for relationship in relationships
        ]
        blocks = entity_lines + relationship_lines
        if not blocks:
            return ""
        return await self._summarize_blocks_recursive(blocks, level)

    async def _summarize_blocks_recursive(self, blocks: list[str], level: int) -> str:
        """Summarize ``blocks``, chunking recursively to stay within context."""
        joined = "\n".join(blocks)
        # Rough token estimate: ~4 chars per token.
        estimated_tokens = len(joined) // 4
        if estimated_tokens <= self._summary_chunk_tokens:
            return await self._summarize_one(joined, level)

        # Split into ~equal chunks by a character budget derived from the token
        # limit.  Bound the number of chunks to avoid pathological splits.
        budget_chars = self._summary_chunk_tokens * 4
        chunks: list[str] = []
        current = ""
        for block in blocks:
            if current and len(current) + len(block) + 1 > budget_chars:
                chunks.append(current)
                current = block
            else:
                current = f"{current}\n{block}" if current else block
        if current:
            chunks.append(current)

        chunk_summaries = [
            await self._summarize_one(chunk, level) for chunk in chunks
        ]
        # Combine the chunk summaries; recurse in case they still overflow.
        combined_blocks = [
            f"Sub-summary {i + 1}:\n{summary}" for i, summary in enumerate(chunk_summaries)
        ]
        return await self._summarize_blocks_recursive(combined_blocks, level)

    async def _summarize_one(self, content: str, level: int) -> str:
        """Single-shot summary call for one chunk of community content."""
        prompt = (
            f"Level: {level}\n\n"
            f"Community items:\n{content}\n\n"
            "Write a concise summary of this community."
        )
        return await self._plain_text(
            messages=_plain_text_messages(_SUMMARY_SYSTEM_PROMPT, prompt),
            model=self._summary_model_name,
        )

    async def generate_summary_from_children(
        self, child_summaries: list[str], level: int
    ) -> str:
        """Summarize a parent community from its children's summaries.

        Bottom-up map-reduce: the input is already-synthesized child texts, never
        raw entities, so each call stays within the context window.  Large parent
        communities (many children) are chunked recursively by ``_summarize_blocks_recursive``.
        """
        if not child_summaries:
            return ""
        blocks = [f"Child community summary:\n{summary}" for summary in child_summaries]
        return await self._summarize_blocks_recursive(blocks, level)

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
                    # Instructor internal retries disabled; tenacity owns the
                    # retry policy (stop_after_attempt in self._retrying).
                    max_retries=self._instructor_retries,
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
        return await self._plain_text(
            messages=_plain_text_messages(_COMPOSE_SYSTEM_PROMPT, prompt),
            model=self._summary_model_name,
        )
