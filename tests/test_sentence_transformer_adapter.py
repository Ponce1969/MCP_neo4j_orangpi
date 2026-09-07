"""Tests for SentenceTransformerAdapter with fake backend injection.

These tests never load real sentence-transformers model weights; they inject a
fake ``_encode_sync`` and a fake ``_model_loader`` so the adapter can be unit
tested deterministically without the heavy dependency.
"""

from __future__ import annotations

from typing import Any

from book_graph_rag.config import Settings
from book_graph_rag.infrastructure.sentence_transformer_adapter import (
    SentenceTransformerAdapter,
)
from book_graph_rag.ports.embedding_provider_port import EmbeddingRequest


class _FakeModel:
    """Stand-in for a loaded sentence-transformers model."""

    def __init__(self, dim: int) -> None:
        self._dim = dim

    def get_embedding_dimension(self) -> int:
        return self._dim

    def get_sentence_embedding_dimension(self) -> int:
        return self._dim


def _fake_encode_sync(_model: Any, texts: tuple[str, ...]) -> list[tuple[float, ...]]:
    """Return a deterministic one-hot vector per distinct text."""
    return [
        (1.0, 0.0, 0.0) if t == "foo" else (0.0, 1.0, 0.0) if t == "bar" else (0.0, 0.0, 1.0)
        for t in texts
    ]


def _fake_model_loader(model_id: str) -> _FakeModel:
    return _FakeModel(dim=3)


def _make_adapter() -> SentenceTransformerAdapter:
    settings = Settings.model_validate(
        {
            "neo4j_uri": "bolt://localhost:7687",
            "neo4j_user": "neo4j",
            "neo4j_password": "secret",
        }
    )
    return SentenceTransformerAdapter(
        settings,
        encode_sync=_fake_encode_sync,
        model_loader=_fake_model_loader,
    )


async def test_embed_returns_ordered_vectors() -> None:
    """``embed`` preserves text order and returns one vector per text."""
    adapter = _make_adapter()
    request = EmbeddingRequest(texts=("foo", "bar"), model_id="fake-x")

    batch = await adapter.embed(request)

    assert batch.model_id == "fake-x"
    assert len(batch.vectors) == 2
    assert batch.vectors[0].values == (1.0, 0.0, 0.0)
    assert batch.vectors[1].values == (0.0, 1.0, 0.0)
    assert batch.vectors[0].model_id == "fake-x"


async def test_embed_is_deterministic_for_same_texts() -> None:
    """Calling embed twice on the same texts yields identical vectors."""
    adapter = _make_adapter()
    request = EmbeddingRequest(texts=("foo",), model_id="fake-x")

    batch_a = await adapter.embed(request)
    batch_b = await adapter.embed(request)

    assert batch_a.vectors[0].values == batch_b.vectors[0].values


async def test_model_dim_reports_fake_dimension() -> None:
    """``model_dim`` reads the dimension reported by the loaded model."""
    adapter = _make_adapter()

    assert adapter.model_dim("fake-x") == 3


async def test_model_dim_uses_legacy_fallback() -> None:
    """If the model only exposes ``get_sentence_embedding_dimension``, use it."""

    class LegacyModel:
        def get_sentence_embedding_dimension(self) -> int:
            return 7

    adapter = SentenceTransformerAdapter(
        Settings.model_validate(
            {
                "neo4j_uri": "bolt://localhost:7687",
                "neo4j_user": "neo4j",
                "neo4j_password": "secret",
            }
        ),
        encode_sync=_fake_encode_sync,
        model_loader=lambda _mid: LegacyModel(),
    )

    assert adapter.model_dim("legacy") == 7


def test_load_model_caches_same_instance() -> None:
    """Loading the same model_id twice returns the cached instance."""
    adapter = _make_adapter()

    first = adapter._load_model("fake-x")  # noqa: SLF001
    second = adapter._load_model("fake-x")  # noqa: SLF001

    assert first is second


def test_load_model_returns_distinct_instances_per_id() -> None:
    """Different model_ids receive distinct cached instances."""
    adapter = _make_adapter()

    a = adapter._load_model("fake-a")  # noqa: SLF001
    b = adapter._load_model("fake-b")  # noqa: SLF001

    assert a is not b
