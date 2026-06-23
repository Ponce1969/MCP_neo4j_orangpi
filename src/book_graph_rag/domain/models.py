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

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

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


class Relationship(BaseModel):
    """A directed edge between two entities."""

    model_config = ConfigDict()
    source_entity_id: str
    target_entity_id: str
    type: RelationshipType
    description: str = ""
    source_page: int | None = None


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
    page_ref: PageRef
    # LLM-extracted content (filled by LLMAdapter) — starts empty
    entities: list[Entity] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)


# ── Query models (read-side graph queries, Fase 06) ──────────────────────────


class GraphQuery(BaseModel):
    """Base for all read-side graph queries; discriminated by ``type``."""

    model_config = ConfigDict()
    type: Literal["entity", "relation", "path", "similarity", "batch_entity"]


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


GraphQueryUnion = Annotated[
    EntityQuery | RelationQuery | PathQuery | SimilarityQuery | BatchEntityQuery,
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
    """Entity enriched with optional provenance fields reserved for Fase 08."""

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
    metadata: QueryMetadata


# ── Query logging models (MCP server observability, Fase 07) ─────────────────


class QueryLogEntry(BaseModel):
    """Structured log entry for an MCP tool execution.

    Captures the signals needed for 07.2 gap analysis: which tool was called,
    what kind of query it represents, the input parameters, how many results
    were returned, whether the query produced zero results or a missing entity,
    how long it took, and any error that occurred.
    """

    model_config = ConfigDict()
    timestamp: datetime
    tool_name: str
    query_type: str
    query_params: dict[str, Any]
    result_count: int
    zero_results: bool
    entity_not_found: bool
    duration_ms: float
    error: str | None = None


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
