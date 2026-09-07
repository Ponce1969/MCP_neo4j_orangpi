"""Tests for the EmbeddingProviderPort domain contract."""

from __future__ import annotations

import pytest

from book_graph_rag.ports.embedding_provider_port import (
    EmbeddingBatch,
    EmbeddingProviderPort,
    EmbeddingRequest,
    EmbeddingVector,
)


def test_embedding_vector_is_frozen_and_hashable() -> None:
    """Vectors must be immutable value objects suitable for caching keys."""
    v1 = EmbeddingVector(values=(0.1, 0.2, 0.3), model_id="fake")
    v2 = EmbeddingVector(values=(0.1, 0.2, 0.3), model_id="fake")

    assert v1 == v2
    assert hash(v1) == hash(v2)


def test_embedding_request_preserves_text_order() -> None:
    """The request carries an ordered batch of texts to embed."""
    request = EmbeddingRequest(texts=("first", "second", "third"), model_id="fake")

    assert request.texts == ("first", "second", "third")
    assert request.model_id == "fake"


def test_embedding_batch_holds_vectors() -> None:
    """A batch groups vectors produced from a single embed call."""
    vectors = [
        EmbeddingVector(values=(0.1, 0.2), model_id="fake"),
        EmbeddingVector(values=(0.3, 0.4), model_id="fake"),
    ]
    batch = EmbeddingBatch(model_id="fake", vectors=vectors)

    assert batch.model_id == "fake"
    assert len(batch.vectors) == 2
    assert batch.vectors[0].values == (0.1, 0.2)


def test_embedding_provider_port_is_abstract() -> None:
    """The port cannot be instantiated; implementations must override methods."""
    with pytest.raises(TypeError, match="abstract"):
        EmbeddingProviderPort()  # type: ignore[abstract]
