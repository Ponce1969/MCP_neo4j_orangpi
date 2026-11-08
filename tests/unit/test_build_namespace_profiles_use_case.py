"""Behavior tests for the namespace profile builder use case."""

from __future__ import annotations

import math

import pytest

from book_graph_rag.application.build_namespace_profiles_use_case import (
    DEFAULT_PROFILE_K,
    BuildNamespaceProfilesUseCase,
)
from book_graph_rag.domain.namespaces import SourceNamespace
from book_graph_rag.domain.routing_models import NamespaceProfile, mean_normalized
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


class _FixedSource(NamespaceProfileSourcePort):
    """One caller-chosen namespace, so tests know every text and id."""

    def __init__(self, texts: NamespaceSourceTexts) -> None:
        self._texts = texts

    async def load_source_texts(self) -> tuple[NamespaceSourceTexts, ...]:
        return (self._texts,)


class _FakeEmbedding(EmbeddingProviderPort):
    """Deterministic per-namespace vectors: axis-aligned unit vectors."""

    async def embed(self, request: EmbeddingRequest) -> EmbeddingBatch:
        values = (0.0, 1.0, 0.0) if any("b-" in text for text in request.texts) else (1.0, 0.0, 0.0)
        vectors = [EmbeddingVector(values=values, model_id=request.model_id) for _ in request.texts]
        return EmbeddingBatch(model_id=request.model_id, vectors=vectors)

    def model_dim(self, model_id: str) -> int:
        return 3


class _SequentialEmbedding(EmbeddingProviderPort):
    """One distinct rotating unit vector per text, in input order."""

    async def embed(self, request: EmbeddingRequest) -> EmbeddingBatch:
        vectors = [
            EmbeddingVector(
                values=tuple(1.0 if dim == index % 3 else 0.0 for dim in range(3)),
                model_id=request.model_id,
            )
            for index, _text in enumerate(request.texts)
        ]
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
    embedding: EmbeddingProviderPort | None = None,
) -> tuple[BuildNamespaceProfilesUseCase, _RecordingStore]:
    recording_store = store or _RecordingStore()
    use_case = BuildNamespaceProfilesUseCase(
        source or _FakeSource(),
        embedding or _FakeEmbedding(),
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


async def test_centroid_is_normalized_mean_of_port_vectors() -> None:
    """The builder is agnostic: whatever the port returns, centroid = norm mean."""
    use_case, _store = _use_case(embedding=_SequentialEmbedding())

    profiles = await use_case.execute()

    # book-a has 4 texts → (1,0,0), (0,1,0), (0,0,1), (1,0,0).
    vectors = [
        (1.0, 0.0, 0.0),
        (0.0, 1.0, 0.0),
        (0.0, 0.0, 1.0),
        (1.0, 0.0, 0.0),
    ]
    mean = tuple(sum(vector[dim] for vector in vectors) / len(vectors) for dim in range(3))
    norm = math.sqrt(sum(value * value for value in mean))
    expected_a = tuple(value / norm for value in mean)
    assert profiles[0].centroid == pytest.approx(expected_a)
    # book-b has 2 texts → (1,0,0), (0,1,0) → mean (0.5, 0.5, 0).
    expected_b = (math.sqrt(0.5), math.sqrt(0.5), 0.0)
    assert profiles[1].centroid == pytest.approx(expected_b)
    assert profiles[0].dimension == 3


def _use_case_with_k(
    source: NamespaceProfileSourcePort,
    embedding: EmbeddingProviderPort,
    *,
    k: int,
) -> BuildNamespaceProfilesUseCase:
    """Same composition as ``_use_case`` but with an explicit ``k``."""
    return BuildNamespaceProfilesUseCase(
        source,
        embedding,
        _RecordingStore(),
        model_id="test-model",
        profile_version="2.0.0",
        catalog_version="1",
        graph_snapshot="snapshot-x",
        k=k,
    )


async def test_builder_prototype_sources_point_at_producing_texts() -> None:
    """S6/S1: each prototype is paired with the id of the text that produced
    it; the diagnostic centroid is still the normalised mean."""
    source_texts = NamespaceSourceTexts(
        namespace=_ns("knowledge:book-a"),
        chapter_titles=("Chapter One A",),
        chunk_texts=("chunk-a-1", "chunk-a-2"),
        community_summaries=("summary-a",),
    )
    use_case = _use_case_with_k(_FixedSource(source_texts), _SequentialEmbedding(), k=2)

    profiles = await use_case.execute()
    profile = profiles[0]

    # Text order and ids the port exposes (0-based ordinal per kind).
    assert source_texts.ordered_profile_texts == (
        "Chapter One A",
        "chunk-a-1",
        "chunk-a-2",
        "summary-a",
    )
    assert source_texts.ordered_profile_text_ids == (
        "chapter:0",
        "chunk:0",
        "chunk:1",
        "summary:0",
    )
    # Vectors in that order: (1,0,0), (0,1,0), (0,0,1), (1,0,0).
    # k=2 → seed = closest to the mean (index 0, lowest index wins the tie),
    # second = farthest from it (index 1, lowest index wins the tie).
    assert profile.k == 2
    assert profile.prototypes == ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0))
    assert profile.prototype_sources == ("chapter:0", "chunk:0")
    # Unchanged behaviour: centroid is the normalised mean of ALL texts.
    expected_centroid = mean_normalized(
        ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0), (1.0, 0.0, 0.0))
    )
    assert profile.centroid == pytest.approx(expected_centroid)


def test_builder_rejects_k_below_one() -> None:
    with pytest.raises(ValueError, match="k must be >= 1"):
        BuildNamespaceProfilesUseCase(
            _FakeSource(),
            _FakeEmbedding(),
            _RecordingStore(),
            model_id="test-model",
            profile_version="2.0.0",
            catalog_version="1",
            graph_snapshot="snapshot-x",
            k=0,
        )


async def test_builder_defaults_to_eight_prototypes() -> None:
    many_texts = NamespaceSourceTexts(
        namespace=_ns("knowledge:book-a"),
        chunk_texts=tuple(f"chunk-{index}" for index in range(12)),
    )
    use_case = BuildNamespaceProfilesUseCase(
        _FixedSource(many_texts),
        _FakeEmbedding(),
        _RecordingStore(),
        model_id="test-model",
        profile_version="2.0.0",
        catalog_version="1",
        graph_snapshot="snapshot-x",
    )

    profile = (await use_case.execute())[0]

    assert DEFAULT_PROFILE_K == 8
    assert profile.k == 8
    assert len(profile.prototypes) == 8
    assert len(profile.prototype_sources) == 8


async def test_builder_caps_k_at_the_available_text_pool() -> None:
    """Fewer texts than k → every text becomes a prototype, k reports the
    real count (S1 validator: k == len(prototypes))."""
    use_case, _store = _use_case()  # book-a: 4 texts, book-b: 2 texts

    profiles = await use_case.execute()

    assert [profile.k for profile in profiles] == [4, 2]
    for profile in profiles:
        assert profile.k == len(profile.prototypes) == len(profile.prototype_sources)
