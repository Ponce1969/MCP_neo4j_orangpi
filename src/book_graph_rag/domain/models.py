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

from typing import Literal

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
