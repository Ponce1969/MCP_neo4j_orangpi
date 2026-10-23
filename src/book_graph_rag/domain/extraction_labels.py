"""Pure label models and normalization for extraction-fidelity measurements."""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from book_graph_rag.domain.models import EntityType

LabelConfidence = Literal["high", "low"]


def key_words(surface: str) -> str:
    """Apply protocol v1 §4.1 steps 1–6, retaining normalized separators."""
    normalized = unicodedata.normalize("NFKC", surface).casefold()
    decomposed = unicodedata.normalize("NFD", normalized)
    accent_folded = "".join(
        character for character in decomposed if not unicodedata.category(character).startswith("M")
    )
    expanded_ampersands = accent_folded.replace("&", "and")
    alphanumeric_spaces = "".join(
        character if character.isalnum() or character.isspace() else " "
        for character in expanded_ampersands
    )
    return " ".join(alphanumeric_spaces.split())


def key_compact(surface: str) -> str:
    """Return the word key without separators for separator-insensitive match."""
    return key_words(surface).replace(" ", "")


def normalize_surface(surface: str) -> str:
    """Compatibility name for the literal, word-separated protocol key."""
    return key_words(surface)


class LabeledEntity(BaseModel):
    """A human-labeled entity and its in-chunk surface forms."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    surfaces: list[str] = Field(min_length=1)
    canonical: str
    type: EntityType
    confidence: LabelConfidence = "high"

    @model_validator(mode="after")
    def validate_surface_forms(self) -> LabeledEntity:
        word_keys = [key_words(surface) for surface in self.surfaces]
        compact_keys = [key_compact(surface) for surface in self.surfaces]
        if any(not key for key in compact_keys):
            raise ValueError("surfaces must not be empty after normalization")
        if len(set(compact_keys)) != len(compact_keys):
            raise ValueError("surfaces must not contain duplicate normalized forms")
        canonical_word_key = key_words(self.canonical)
        canonical_compact_key = key_compact(self.canonical)
        if not canonical_compact_key:
            raise ValueError("canonical must not be empty after normalization")
        if canonical_word_key not in word_keys and canonical_compact_key not in compact_keys:
            raise ValueError("surfaces must include the canonical form")
        return self


class LabeledChunk(BaseModel):
    """Ground-truth entities for one chunk, in protocol v1 label-file shape."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    chunk_id: str
    book_id: str
    chunk_index: int
    entities: list[LabeledEntity]
    notes: str

    @model_validator(mode="after")
    def validate_chunk_and_entity_order(self) -> LabeledChunk:
        if self.chunk_id != f"{self.book_id}:{self.chunk_index}":
            raise ValueError("chunk_id must equal book_id:chunk_index")
        normalized_canonicals = [normalize_surface(entity.canonical) for entity in self.entities]
        if normalized_canonicals != sorted(normalized_canonicals):
            raise ValueError("entities must be sorted by normalized canonical")
        return self


def load_labeled_chunks(
    rows: Iterable[Mapping[str, object]],
) -> list[LabeledChunk]:
    """Validate already-parsed label-file rows without performing any I/O."""
    chunks: list[LabeledChunk] = []
    seen_chunk_ids: set[str] = set()
    for row in rows:
        chunk = LabeledChunk.model_validate(dict(row))
        if chunk.chunk_id in seen_chunk_ids:
            raise ValueError(f"duplicate chunk_id: {chunk.chunk_id}")
        seen_chunk_ids.add(chunk.chunk_id)
        chunks.append(chunk)
    return chunks
