"""Pydantic models for the book knowledge graph.

The module contains two groups of models:

* Editorial entities (``Book``, ``Chapter``, ``Section``, ``PageRef``):
  immutable representations of the PDF/book structure. They are
  ``frozen=True`` because they model inherited content that does not mutate at
  runtime.
* Knowledge entities (``Entity``, ``Relationship``, ``KnowledgeGraphChunk``):
  mutable entities extracted or populated by the LLM during indexing.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from book_graph_rag.domain.mcp_security import QueryFingerprint

# ── Editorial entities (inherited book structure, frozen) ────────────────────


class Book(BaseModel):
    """A book being indexed — root node of the editorial hierarchy."""

    model_config = ConfigDict(frozen=True)
    id: str  # stable: slug of title or hash of (title, author)
    title: str
    author: str = ""  # may be empty for multi-author books (which this one is)
    pdf_path: str  # absolute or relative path to source PDF (provenance)
    page_count: int  # total pages (from PyMuPDF doc.page_count)


class PageRef(BaseModel):
    """Page range occupied by a chunk inside the PDF."""

    model_config = ConfigDict(frozen=True)
    start: int
    end: int


class Chapter(BaseModel):
    """A chapter in the book table of contents."""

    model_config = ConfigDict(frozen=True)
    number: int | None  # e.g. 1, 2, 3 — None for prefaces/indices
    title: str
    page_start: int


class Section(BaseModel):
    """A section or subsection in the book table of contents."""

    model_config = ConfigDict(frozen=True)
    chapter_number: int | None
    level: int  # depth in the TOC: 2 = section, 3 = subsection
    title: str
    page_start: int
    parent_section_title: str | None  # None when it is a direct child of a chapter


# ── Knowledge entities (extracted/populated by the LLM) ──────────────────────

EntityType = Literal[
    "pattern",
    "agent",
    "component",
    "concept",
    "tool",
    "framework",
    "mcp",
    "llmops",
    "risk",
]

RelationshipType = Literal[
    "requires",
    "alternative_to",
    "composes",
    "extends",
    "enables",
    "depends_on",
    "contrasts_with",
    "evolves_to",
]


class Entity(BaseModel):
    """A node in the knowledge graph extracted by the LLM from a chunk."""

    model_config = ConfigDict()
    id: str = Field(description="Stable hash-based or slug-based id")
    name: str
    type: EntityType
    description: str = ""
    source_page: int | None = None  # page where this entity was first mentioned
    aliases: list[str] = Field(default_factory=list)
    canonical_name: str | None = None


class Relationship(BaseModel):
    """A directed edge between two entities."""

    model_config = ConfigDict()
    source_entity_id: str
    target_entity_id: str
    type: RelationshipType
    description: str = ""
    source_page: int | None = None
    chunk_index: int | None = None


class KnowledgeGraphChunk(BaseModel):
    """Result of LLM extraction over a single PDF chunk.

    Carries both the source text + editorial metadata (``Chapter``,
    ``Section``, ``PageRef``) and the entities/relationships extracted from
    that chunk by the LLM.
    """

    model_config = ConfigDict()
    # Original chunk data
    text: str
    chunk_index: int
    # Editorial metadata (filled by PDFAdapter, may be None for PDFs without TOC)
    book: Book | None = None  # None only when the chunk has no parent book
    chapter: Chapter | None = None
    section: Section | None = None
    section_ancestors: tuple[Section, ...] = ()
    page_ref: PageRef
    # LLM-extracted content (filled by LLMAdapter) — starts empty
    entities: list[Entity] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)


# ── Query models (read-side graph queries, Fase 06) ──────────────────────────


class GraphQuery(BaseModel):
    """Base for all read-side graph queries; discriminated by ``type``."""

    model_config = ConfigDict()
    type: Literal[
        "entity", "relation", "path", "similarity", "batch_entity",
        "community", "text2cypher",
    ]


class EntityQuery(GraphQuery):
    """Lookup entities by name with an optional type filter."""

    type: Literal["entity"] = "entity"
    name: str
    entity_type: EntityType | None = None
    limit: int = 100


class RelationQuery(GraphQuery):
    """Traverse relationships outward from a source entity."""

    type: Literal["relation"] = "relation"
    source_id: str
    rel_type: RelationshipType | None = None
    depth: int = 1


class PathQuery(GraphQuery):
    """Shortest path between two entities."""

    type: Literal["path"] = "path"
    start_id: str
    end_id: str
    max_depth: int = 3


class SimilarityQuery(GraphQuery):
    """Reserved semantic similarity query (not implemented in Fase 06)."""

    type: Literal["similarity"] = "similarity"
    text: str
    top_k: int = 10


class BatchEntityQuery(GraphQuery):
    """Batch lookup of entities by their stable ids."""

    type: Literal["batch_entity"] = "batch_entity"
    ids: list[str]


class CommunityQuery(GraphQuery):
    """Retrieve community summaries for global, high-level questions.

    Community summaries are Leiden-derived hierarchical clusters (level 0–3)
    persisted as ``:CommunitySummary`` nodes in Neo4j.
    """

    type: Literal["community"] = "community"
    level: int = Field(ge=0, le=3, default=1)
    keyword: str | None = None
    top_k: int = 5


class Text2CypherQuery(GraphQuery):
    """Free-text question transformed into a Cypher MATCH query by the LLM.

    ``validated_cypher`` is populated by the text-to-Cypher adapter after
    syntactic validation (EXPLAIN) and safety guard (read-only check).
    """

    type: Literal["text2cypher"] = "text2cypher"
    natural_language: str
    validated_cypher: str | None = None


GraphQueryUnion = Annotated[
    EntityQuery | RelationQuery | PathQuery | SimilarityQuery | BatchEntityQuery
    | CommunityQuery | Text2CypherQuery,
    Field(discriminator="type"),
]


class GraphPath(BaseModel):
    """A path through the graph: ordered nodes plus the edges between them."""

    model_config = ConfigDict()
    nodes: list[Entity]
    relationships: list[Relationship]


class QueryMetadata(BaseModel):
    """Metadata returned with every query execution."""

    model_config = ConfigDict()
    total_count: int
    query_ms: float
    depth: int | None = None
    cursor: int | None = None
    timed_out: bool = False


class EntityWithContext(BaseModel):
    """Entity enriched with optional provenance fields.

    ``source`` carries the originating chunk provenance as a structured string
    containing ``chunk_index`` and ``book_id`` when available, for example
    ``"book_id=agentic-patterns,chunk_index=5"``. This supports citation in
    retrieval evidence and RAGAS debugging.
    """

    model_config = ConfigDict()
    entity: Entity
    status: str | None = None
    confidence: float | None = None
    source: str | None = None


class GraphQueryResult(BaseModel):
    """Unified result payload for any graph query."""

    model_config = ConfigDict()
    entities: list[EntityWithContext] = []
    relationships: list[Relationship] = []
    paths: list[GraphPath] = []
    chunks: list[dict[str, Any]] = []
    community_summaries: list[CommunitySummary] = []
    metadata: QueryMetadata


# ── Query logging models (MCP server observability, Fase 07) ─────────────────

#: Placeholder substituted for secret-looking values during log redaction (R5).
REDACTED_PLACEHOLDER = "<redacted>"

#: Current persisted schema version for the query log (R5).
#: v1 stored raw ``query_params`` and ``error``; v2 is metadata-only.
QUERY_LOG_SCHEMA_VERSION = 2

#: Credential-like key names whose values must never be persisted in clear.
_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|"
    r"auth[_-]?token|token|secret|password|passwd|pwd|credential|"
    r"authorization|bearer|private[_-]?key|session[_-]?token|"
    r"client[_-]?secret)"
)

#: Secret-shaped value patterns (API keys, tokens, JWTs, auth headers).
_SECRET_VALUE_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),  # OpenAI-style keys
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access keys
    re.compile(r"gh[pousr]_[0-9A-Za-z]{32,}"),  # GitHub tokens
    re.compile(r"xox[baprs]-[0-9A-Za-z\-]{10,}"),  # Slack tokens
    re.compile(  # JWT
        r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"
    ),
    re.compile(r"(?i)^bearer\s+[A-Za-z0-9._\-]{8,}"),  # Authorization header
    re.compile(r"(?i)^basic\s+[A-Za-z0-9+/=]{8,}"),  # Basic auth header
)


def _is_sensitive_key(key: str) -> bool:
    """Return True when ``key`` names a credential-like field."""
    return bool(_SENSITIVE_KEY_RE.search(key))


def _is_secret_value(value: str) -> bool:
    """Return True when ``value`` looks like a secret (key, token, JWT, header)."""
    return any(pattern.search(value) for pattern in _SECRET_VALUE_RES)


def redact_sensitive(value: Any, *, key: str | None = None) -> Any:
    """Recursively redact secret-looking values, returning a new structure.

    Detection is key-name based (``api_key``, ``token``, ``password``, ...) and
    value-pattern based (API keys, tokens, JWTs, and ``Bearer``/``Basic`` auth
    headers). Matches are replaced with :data:`REDACTED_PLACEHOLDER`. The
    function is pure and stdlib-only so the domain keeps no infra dependency.
    """
    if isinstance(value, dict):
        return {k: redact_sensitive(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_sensitive(v) for v in value]
    if key is not None and _is_sensitive_key(key):
        return REDACTED_PLACEHOLDER
    if isinstance(value, str) and _is_secret_value(value):
        return REDACTED_PLACEHOLDER
    return value


def redact_sensitive_metadata(
    metadata: dict[str, str | int | float | bool | None],
) -> dict[str, str | int | float | bool | None]:
    """Return ``metadata`` with sensitive values redacted (R5, defense-in-depth).

    ``query_metadata`` is intentionally non-sensitive, but redaction is applied
    anyway so it cannot be bypassed by a future caller or scalar that carries a
    secret-looking value.
    """
    return {
        key: redact_sensitive(value, key=key)
        for key, value in metadata.items()
    }


#: A bare exception class name (``TimeoutError``) — the only legacy ``error``
#: shape that is safe to promote verbatim to the v2 ``error_code`` field.
_VERBATIM_CLASS_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def migrate_query_log_record(raw: dict[str, Any]) -> dict[str, Any]:
    """Return a v2-shaped record dict, migrating a legacy v1 line when needed.

    v1 lines carried raw ``query_params`` and ``error`` (``str(exc)``) fields;
    both are free text that the v2 metadata-only contract forbids persisting.
    The migration is intentionally lossy and never writes to the source log:

    * ``query_params`` is dropped entirely (never mapped to ``query_metadata``,
      so raw query/prompt values cannot survive).
    * ``error`` is promoted to ``error_code`` only when it looks like a bare
      exception class name (e.g. ``TimeoutError``); free-text messages are
      dropped to ``None`` so no raw error text survives.
    * fingerprints are left unset (``None``) and ``query_metadata`` defaults to
      ``{}`` when absent.
    * ``schema_version`` is stamped with the current version.

    v2 lines (``schema_version == 2`` or absent but v2-shaped) pass through
    unchanged except for having ``schema_version`` normalized when missing.
    """
    # v1 detection: presence of either legacy raw field.
    if "query_params" not in raw and "error" not in raw:
        migrated = dict(raw)
        migrated.setdefault("schema_version", QUERY_LOG_SCHEMA_VERSION)
        return migrated

    migrated = {k: v for k, v in raw.items() if k not in ("query_params", "error")}
    error = raw.get("error")
    if isinstance(error, str) and _VERBATIM_CLASS_NAME_RE.match(error):
        migrated["error_code"] = error
    else:
        migrated["error_code"] = None
    migrated.setdefault("query_metadata", {})
    migrated["schema_version"] = QUERY_LOG_SCHEMA_VERSION
    return migrated


class QueryLogEntry(BaseModel):
    """Metadata-only structured log entry for an MCP tool execution (R5).

    The persisted schema never carries raw query, prompt, or error text.
    Free-text inputs are replaced by keyed HMAC-SHA256 fingerprints
    (``query_fingerprint`` and ``prompt_fingerprint``) and failures are reduced
    to a stable ``error_code``. ``query_metadata`` holds only non-sensitive
    scalar metadata (presence flags and configuration knobs), never the values
    of the actual query.
    """

    model_config = ConfigDict()
    schema_version: int = QUERY_LOG_SCHEMA_VERSION
    timestamp: datetime
    tool_name: str
    query_type: str
    query_metadata: dict[str, str | int | float | bool | None]
    result_count: int
    zero_results: bool
    entity_not_found: bool
    duration_ms: float
    error_code: str | None = None
    query_fingerprint: QueryFingerprint | None = None
    prompt_fingerprint: QueryFingerprint | None = None


# ── Community summary models (GraphRAG global-query layer) ───────────────────


def _community_summary_id(level: int, entity_ids: list[str]) -> str:
    """Stable 16-char hex id for a community summary.

    The hash covers the level and the sorted membership so the same community
    always receives the same id across runs.
    """
    key = f"{level}:{','.join(sorted(entity_ids))}"
    return hashlib.sha1(key.encode()).hexdigest()[:16]


class CommunitySummary(BaseModel):
    """A Leiden-derived community summary at a given hierarchical level.

    The model is frozen because it represents a derived snapshot of the graph.
    Its ``id`` is a stable hash of the level and member entity ids, so repeated
    community runs produce the same identity for the same community.
    """

    model_config = ConfigDict(frozen=True)
    id: str = ""
    level: int = Field(ge=0, le=3)
    summary: str
    entity_ids: list[str] = Field(min_length=1)
    parent_id: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _compute_and_validate_id(cls, data: Any) -> Any:
        """Compute a stable id when missing and validate it when present."""
        if isinstance(data, dict):
            level = data.get("level")
            entity_ids = data.get("entity_ids")
            if level is not None and entity_ids is not None:
                computed = _community_summary_id(level, entity_ids)
                provided_id = data.get("id")
                if provided_id and provided_id != computed:
                    raise ValueError(
                        "id must be the stable hash of level and sorted entity_ids"
                    )
                data["id"] = computed
        return data

    @model_validator(mode="after")
    def _validate_parent_id(self) -> CommunitySummary:
        """Level 0 must have no parent; levels 1-3 must have one."""
        if self.level == 0 and self.parent_id is not None:
            raise ValueError("level 0 community cannot have a parent_id")
        if self.level > 0 and self.parent_id is None:
            raise ValueError("level > 0 community must have a parent_id")
        return self


# ── Domain errors (query layer) ──────────────────────────────────────────────


class QueryTimeoutError(Exception):
    """Raised when a query exceeds the configured timeout (3 seconds)."""

    def __init__(self, message: str = "Query exceeded the 3-second timeout") -> None:
        super().__init__(message)


class BatchSizeExceededError(Exception):
    """Raised when a batch request exceeds the configured limit."""

    def __init__(self, limit: int, received: int) -> None:
        self.limit = limit
        self.received = received
        super().__init__(f"Batch size {received} exceeds limit of {limit}")


class UnsupportedQueryTypeError(Exception):
    """Raised when the use case receives an unknown query type."""

    def __init__(self, query_type: str) -> None:
        self.query_type = query_type
        super().__init__(f"Unsupported query type: {query_type}")


class UnsafeCypherQueryError(Exception):
    """Raised when a generated Cypher query contains a write or admin operation."""

    def __init__(self, message: str = "Generated Cypher query is not read-only") -> None:
        super().__init__(message)


class Text2CypherTimeoutError(Exception):
    """Raised when the text-to-Cypher pipeline exceeds the configured timeout."""

    def __init__(self, message: str = "Text-to-Cypher pipeline exceeded the timeout") -> None:
        super().__init__(message)


class CypherGenerationError(Exception):
    """Raised when the text-to-Cypher pipeline exhausts its self-healing retries."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
