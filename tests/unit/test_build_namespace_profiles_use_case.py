"""Behavior tests for the namespace profile builder use case."""

from __future__ import annotations

import pytest

from book_graph_rag.application.build_namespace_profiles_use_case import (
    BuildNamespaceProfilesUseCase,
)
from book_graph_rag.domain.namespaces import SourceNamespace
from book_graph_rag.domain.routing_models import NamespaceProfile
from book_graph_rag.ports.embedding_provider_port import (
    EmbeddingBatch,
    EmbeddingProviderPort,
    EmbeddingRequest,
    EmbeddingVector,
)
from book_graph_rag.ports.namespace_profile_source_port import (
    NamespaceProfileSourcePort,
    NamespaceSourceTexts,
)
from book_graph_rag.ports.namespace_profile_store_port import NamespaceProfileStorePort


def _ns(namespace_id: str) -> SourceNamespace:
    corpus, source = namespace_id.split(":", maxsplit=1)
    return SourceNamespace(corpus=corpus, source=source)


class _FakeSource(NamespaceProfileSourcePort):
    def __init__(self) -> None:
        self._sources = (
            NamespaceSourceTexts(
                namespace=_ns("knowledge:book-a"),
                chapter_titles=("Chapter One A",),
                chunk_texts=("chunk-a-1", "chunk-a-2"),
                community_summaries=("summary-a",),
            ),
            NamespaceSourceTexts(
                namespace=_ns("knowledge:book-b"),
                chapter_titles=("Chapter One B",),
                chunk_texts=("chunk-b-1",),
            ),
        )

    async def load_source_texts(self) -> tuple[NamespaceSourceTexts, ...]:
        return self._sources


class _EmptySource(NamespaceProfileSourcePort):
    async def load_source_texts(self) -> tuple[NamespaceSourceTexts, ...]:
        return (
            NamespaceSourceTexts(
                namespace=_ns("knowledge:book-a"),
                chapter_titles=(),
                chunk_texts=(),
            ),
        )


class _FakeEmbedding(EmbeddingProviderPort):
    """Deterministic per-namespace vectors: axis-aligned unit vectors."""

    async def embed(self, request: EmbeddingRequest) -> EmbeddingBatch:
        values = (0.0, 1.0, 0.0) if any("b-" in text for text in request.texts) else (1.0, 0.0, 0.0)
        vectors = [EmbeddingVector(values=values, model_id=request.model_id) for _ in request.texts]
        return EmbeddingBatch(model_id=request.model_id, vectors=vectors)

    def model_dim(self, model_id: str) -> int:
        return 3


class _RecordingStore(NamespaceProfileStorePort):
    def __init__(self) -> None:
        self.saved: tuple[NamespaceProfile, ...] | None = None

    async def save_all(self, profiles: tuple[NamespaceProfile, ...]) -> None:
        self.saved = profiles

    async def load_all(self) -> tuple[NamespaceProfile, ...]:
        return self.saved or ()


def _use_case(
    source: NamespaceProfileSourcePort | None = None,
    store: _RecordingStore | None = None,
) -> tuple[BuildNamespaceProfilesUseCase, _RecordingStore]:
    recording_store = store or _RecordingStore()
    use_case = BuildNamespaceProfilesUseCase(
        source or _FakeSource(),
        _FakeEmbedding(),
        recording_store,
        model_id="test-model",
        profile_version="2.0.0",
        catalog_version="1",
        graph_snapshot="snapshot-x",
    )
    return use_case, recording_store


async def test_build_generates_one_profile_per_namespace() -> None:
    use_case, store = _use_case()

    profiles = await use_case.execute()

    assert [p.namespace.source_id for p in profiles] == [
        "knowledge:book-a",
        "knowledge:book-b",
    ]
    assert store.saved is not None
    assert [p.namespace.source_id for p in store.saved] == [
        "knowledge:book-a",
        "knowledge:book-b",
    ]


async def test_build_populates_profile_metadata() -> None:
    use_case, _store = _use_case()
    profiles = await use_case.execute()

    profile = profiles[0]
    assert profile.model_id == "test-model"
    assert profile.profile_version == "2.0.0"
    assert profile.catalog_version == "1"
    assert profile.graph_snapshot == "snapshot-x"
    assert profile.dimension == 3


async def test_build_centroid_has_unit_norm() -> None:
    use_case, _store = _use_case()

    profiles = await use_case.execute()

    for profile in profiles:
        norm_squared = sum(value * value for value in profile.centroid)
        assert pytest.approx(norm_squared) == 1.0


async def test_dry_run_never_persists() -> None:
    use_case, store = _use_case()

    profiles = await use_case.execute(dry_run=True)

    assert profiles
    assert store.saved is None


async def test_build_with_no_material_raises() -> None:
    use_case, _store = _use_case(source=_EmptySource())

    with pytest.raises(ValueError, match="no source material"):
        await use_case.execute()
