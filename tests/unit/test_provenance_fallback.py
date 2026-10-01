"""Unit tests for the extraction-time provenance fallback.

The extraction prompt asks the LLM for ``source_page`` only when the item is
mentioned on the chunk's *starting* page, so the model legitimately omits it
for everything else.  Without a fallback those rows land without provenance and
the static audit (spec 04: "every node/edge has required provenance") reports
``incomplete``.
"""

from __future__ import annotations

from book_graph_rag.domain.models import (
    Book,
    Entity,
    KnowledgeGraphChunk,
    PageRef,
    Relationship,
)
from book_graph_rag.domain.provenance import resolve_chunk_provenance


def _chunk(
    *,
    chunk_index: int = 3,
    page_start: int = 41,
    entities: list[Entity] | None = None,
    relationships: list[Relationship] | None = None,
) -> KnowledgeGraphChunk:
    return KnowledgeGraphChunk(
        text="a chunk",
        chunk_index=chunk_index,
        book=Book(id="b", title="B", author="", pdf_path="/tmp/b.pdf", page_count=10),
        chapter=None,
        section=None,
        section_ancestors=(),
        page_ref=PageRef(start=page_start, end=page_start + 2),
        entities=entities if entities is not None else [],
        relationships=relationships if relationships is not None else [],
    )


def test_resolve_fills_missing_entity_source_page_from_chunk_start() -> None:
    """An entity the LLM left without provenance inherits the chunk's first page."""
    chunk = _chunk(entities=[Entity(id="e-1", name="E1", type="concept")])

    resolved = resolve_chunk_provenance(chunk)

    assert resolved.entities[0].source_page == 41


def test_resolve_fills_missing_relationship_provenance() -> None:
    """Relationships inherit both the page and the chunk index that produced them."""
    chunk = _chunk(
        chunk_index=3,
        relationships=[
            Relationship(source_entity_id="e-1", target_entity_id="e-2", type="depends_on")
        ],
    )

    resolved = resolve_chunk_provenance(chunk)

    assert resolved.relationships[0].source_page == 41
    assert resolved.relationships[0].chunk_index == 3


def test_resolve_preserves_explicit_source_page_but_binds_chunk_index() -> None:
    """A reported page is trusted; the chunk index always comes from the chunk.

    The chunk that produced the extraction is the authority for ``chunk_index``
    (the LLM-reported value is never trusted over it), while an explicit
    ``source_page`` is kept as the page where the item was first mentioned.
    """
    chunk = _chunk(
        chunk_index=3,
        relationships=[
            Relationship(
                source_entity_id="e-1",
                target_entity_id="e-2",
                type="depends_on",
                source_page=12,
                chunk_index=99,
            )
        ],
        entities=[Entity(id="e-1", name="E1", type="concept", source_page=12)],
    )

    resolved = resolve_chunk_provenance(chunk)

    assert resolved.entities[0].source_page == 12
    assert resolved.relationships[0].source_page == 12
    assert resolved.relationships[0].chunk_index == 3


def test_resolve_does_not_mutate_the_extracted_chunk() -> None:
    """The fallback returns a copy; the caller's extraction payload stays intact."""
    chunk = _chunk(entities=[Entity(id="e-1", name="E1", type="concept")])

    resolve_chunk_provenance(chunk)

    assert chunk.entities[0].source_page is None
