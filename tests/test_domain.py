"""Tests for domain models (AC-02.1 and AC-02.2)."""

import pytest
from pydantic import ValidationError

from book_graph_rag.domain.models import (
    Book,
    Chapter,
    Entity,
    KnowledgeGraphChunk,
    PageRef,
    Relationship,
    Section,
)


def test_entity_invalid_type_raises() -> None:
    with pytest.raises(ValidationError):
        Entity(name="x", id="x1", type="COSO_INVENTADO")  # type: ignore[arg-type]


def test_entity_valid_types_accepted() -> None:
    Entity(name="Pattern A", id="p1", type="pattern")
    Entity(name="Agent X", id="a1", type="agent")
    Entity(name="Framework Y", id="f1", type="framework")


def test_relationship_invalid_type_raises() -> None:
    with pytest.raises(ValidationError):
        Relationship(
            source_entity_id="a",
            target_entity_id="b",
            type="WHATEVER",  # type: ignore[arg-type]
        )


def test_chapter_missing_title_raises() -> None:
    with pytest.raises(ValidationError):
        Chapter(number=1, page_start=1)  # type: ignore[call-arg]


def test_chapter_frozen_cannot_mutate() -> None:
    chapter = Chapter(number=1, title="Chap", page_start=1)
    with pytest.raises(ValidationError):
        chapter.title = "Otro"


def test_section_frozen_cannot_mutate() -> None:
    section = Section(
        chapter_number=1,
        level=2,
        title="Section",
        page_start=10,
        parent_section_title=None,
    )
    with pytest.raises(ValidationError):
        section.title = "Otro"


def test_pageref_frozen_cannot_mutate() -> None:
    page_ref = PageRef(start=1, end=2)
    with pytest.raises(ValidationError):
        page_ref.start = 5


def test_book_missing_title_raises() -> None:
    with pytest.raises(ValidationError):
        Book(id="x", pdf_path="/x.pdf", page_count=100)  # type: ignore[call-arg]


def test_book_frozen_cannot_mutate() -> None:
    book = Book(id="x", title="T", pdf_path="/x.pdf", page_count=100)
    with pytest.raises(ValidationError):
        book.title = "Other"


def test_knowledge_graph_chunk_accepts_book() -> None:
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
    chunk = KnowledgeGraphChunk(
        text="abc",
        chunk_index=0,
        page_ref=PageRef(start=1, end=2),
    )
    assert chunk.entities == []
    assert chunk.relationships == []
    assert chunk.book is None
    assert chunk.chapter is None
    assert chunk.section is None
