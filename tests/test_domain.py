"""Tests for domain models (AC-02.1 and AC-02.2).

Verifies validation, field defaults, immutability of frozen editorial models,
and all valid Literal types for entities and relationships.
"""

from typing import get_args

import pytest
from pydantic import ValidationError

from book_graph_rag.domain.models import (
    Book,
    Chapter,
    Entity,
    EntityType,
    KnowledgeGraphChunk,
    PageRef,
    Relationship,
    RelationshipType,
    Section,
)


def test_entity_invalid_type_raises() -> None:
    """Invalid entity type must raise ValidationError targeting the type field."""
    with pytest.raises(ValidationError, match="type"):
        Entity(name="x", id="x1", type="INVALID_TYPE")  # type: ignore[arg-type]


@pytest.mark.parametrize("entity_type", get_args(EntityType))
def test_entity_all_valid_types_accepted(entity_type: EntityType) -> None:
    """Every documented EntityType must instantiate cleanly with correct attributes and defaults."""
    entity = Entity(name="Test Entity", id="test-1", type=entity_type)

    assert entity.id == "test-1"
    assert entity.name == "Test Entity"
    assert entity.type == entity_type
    assert entity.description == ""
    assert entity.source_page is None
    assert entity.aliases == []
    assert entity.canonical_name is None


def test_entity_with_all_optional_fields() -> None:
    """Entity retains all metadata when fully populated."""
    entity = Entity(
        name="Evaluator-Optimizer",
        id="eval-opt-pattern",
        type="pattern",
        description="Iterative refinement pattern",
        source_page=42,
        aliases=["EvalOpt", "RefinementLoop"],
        canonical_name="Evaluator-Optimizer Pattern",
    )

    assert entity.description == "Iterative refinement pattern"
    assert entity.source_page == 42
    assert entity.aliases == ["EvalOpt", "RefinementLoop"]
    assert entity.canonical_name == "Evaluator-Optimizer Pattern"


def test_relationship_invalid_type_raises() -> None:
    """Invalid relationship type must raise ValidationError targeting the type field."""
    with pytest.raises(ValidationError, match="type"):
        Relationship(
            source_entity_id="a",
            target_entity_id="b",
            type="INVALID_REL",  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("rel_type", get_args(RelationshipType))
def test_relationship_all_valid_types_accepted(rel_type: RelationshipType) -> None:
    """Every documented RelationshipType must instantiate cleanly with correct attributes."""
    rel = Relationship(
        source_entity_id="node-a",
        target_entity_id="node-b",
        type=rel_type,
        description="Connects A to B",
        source_page=15,
        chunk_index=3,
    )

    assert rel.source_entity_id == "node-a"
    assert rel.target_entity_id == "node-b"
    assert rel.type == rel_type
    assert rel.description == "Connects A to B"
    assert rel.source_page == 15
    assert rel.chunk_index == 3


def test_chapter_missing_title_raises() -> None:
    """Chapter requires a title; omission must raise ValidationError."""
    with pytest.raises(ValidationError, match="title"):
        Chapter(number=1, page_start=1)  # type: ignore[call-arg]


def test_chapter_frozen_cannot_mutate() -> None:
    """Editorial Chapter is frozen and forbids attribute mutation."""
    chapter = Chapter(number=1, title="Chap", page_start=1)
    with pytest.raises(ValidationError, match="frozen"):
        chapter.title = "Otro"


def test_section_frozen_cannot_mutate() -> None:
    """Editorial Section is frozen and forbids attribute mutation."""
    section = Section(
        chapter_number=1,
        level=2,
        title="Section",
        page_start=10,
        parent_section_title=None,
    )
    with pytest.raises(ValidationError, match="frozen"):
        section.title = "Otro"


def test_pageref_frozen_cannot_mutate() -> None:
    """Editorial PageRef is frozen and forbids attribute mutation."""
    page_ref = PageRef(start=1, end=2)
    with pytest.raises(ValidationError, match="frozen"):
        page_ref.start = 5


def test_book_missing_title_raises() -> None:
    """Book requires a title; omission must raise ValidationError."""
    with pytest.raises(ValidationError, match="title"):
        Book(id="x", pdf_path="/x.pdf", page_count=100)  # type: ignore[call-arg]


def test_book_frozen_cannot_mutate() -> None:
    """Editorial Book is frozen and forbids attribute mutation."""
    book = Book(id="x", title="T", pdf_path="/x.pdf", page_count=100)
    with pytest.raises(ValidationError, match="frozen"):
        book.title = "Other"


def test_knowledge_graph_chunk_accepts_book() -> None:
    """KnowledgeGraphChunk accepts optional parent Book reference."""
    page_ref = PageRef(start=1, end=2)
    book = Book(
        id="building-ai-apps", title="Building AI Apps", pdf_path="/tmp/b.pdf", page_count=42
    )

    chunk_default = KnowledgeGraphChunk(text="abc", chunk_index=0, page_ref=page_ref)
    assert chunk_default.book is None

    chunk_with_book = KnowledgeGraphChunk(text="abc", chunk_index=0, page_ref=page_ref, book=book)
    assert chunk_with_book.book is book
    assert chunk_with_book.book.id == "building-ai-apps"


def test_knowledge_graph_chunk_defaults() -> None:
    """KnowledgeGraphChunk initializes with empty entities/relationships and None hierarchy."""
    chunk = KnowledgeGraphChunk(
        text="abc",
        chunk_index=0,
        page_ref=PageRef(start=1, end=2),
    )
    assert chunk.text == "abc"
    assert chunk.chunk_index == 0
    assert chunk.page_ref.start == 1
    assert chunk.page_ref.end == 2
    assert chunk.entities == []
    assert chunk.relationships == []
    assert chunk.book is None
    assert chunk.chapter is None
    assert chunk.section is None
