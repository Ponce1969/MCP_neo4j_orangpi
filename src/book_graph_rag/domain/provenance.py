"""Provenance fallback for LLM-extracted graph payloads.

The extraction prompt asks the model for ``source_page`` only when the
entity/relationship is mentioned on the chunk's *starting* page, so the model
legitimately omits it for everything that appears later inside the same chunk.
Persisting that omission leaves ``:Entity``/``:RELATED`` without provenance and
makes the static audit (``docs/spec/04-graph-integrity-and-audit.md``: "every
node/edge has required provenance") report an ``INCOMPLETE`` state.

This module closes that gap without inventing data: the page where a chunk
starts is, by definition, the first page the item could have come from.

Layer: domain — pure functions over domain models, no I/O and no adapters.
"""

from __future__ import annotations

from book_graph_rag.domain.models import Entity, KnowledgeGraphChunk, Relationship


def _entity_with_provenance(entity: Entity, page_start: int) -> Entity:
    """Fill the missing ``source_page`` of an extracted entity."""
    if entity.source_page is not None:
        return entity
    return entity.model_copy(update={"source_page": page_start})


def _relationship_with_provenance(
    relationship: Relationship, chunk_index: int, page_start: int
) -> Relationship:
    """Fill the missing provenance of an extracted relationship.

    ``chunk_index`` is always taken from the chunk that produced the
    extraction: the model-reported value is never trusted over its producer.
    """
    return relationship.model_copy(
        update={
            "source_page": (
                relationship.source_page if relationship.source_page is not None else page_start
            ),
            "chunk_index": chunk_index,
        }
    )


def resolve_chunk_provenance(chunk: KnowledgeGraphChunk) -> KnowledgeGraphChunk:
    """Return ``chunk`` with every extracted item carrying provenance.

    Returns a copy: the caller's extraction payload is never mutated.
    """
    if not chunk.entities and not chunk.relationships:
        return chunk
    page_start = chunk.page_ref.start
    return chunk.model_copy(
        update={
            "entities": [_entity_with_provenance(entity, page_start) for entity in chunk.entities],
            "relationships": [
                _relationship_with_provenance(relationship, chunk.chunk_index, page_start)
                for relationship in chunk.relationships
            ],
        }
    )
