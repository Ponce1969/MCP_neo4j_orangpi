"""Application use case: build namespace routing profiles read-only.

This use case reads source material, embeds it locally, selects ``k``
representative prototypes per namespace by deterministic farthest-point
sampling (S3/S6), keeps the normalized mean of every vector as a diagnostic
centroid, computes the optional ``central_centroid`` (the L9 ``central_mean``
hypothesis: mean of the ``central_pool`` vectors closest to the book mean,
T6/L10), and persists a local artifact. It never mutates Neo4j; only the
configured local profile store is written.
"""

from __future__ import annotations

from collections.abc import Sequence

from book_graph_rag.domain.routing_models import (
    NamespaceProfile,
    cosine_similarity,
    mean_normalized,
    select_prototypes,
)
from book_graph_rag.ports.embedding_provider_port import (
    EmbeddingProviderPort,
    EmbeddingRequest,
)
from book_graph_rag.ports.namespace_profile_source_port import (
    NamespaceProfileSourcePort,
    NamespaceSourceTexts,
)
from book_graph_rag.ports.namespace_profile_store_port import NamespaceProfileStorePort

#: Default prototypes per book (S6). The CLI exposes it as ``--k`` so the
#: 4/8/16 sweep (§9 L1) is three builds of the same code, not three edits.
DEFAULT_PROFILE_K = 8

#: Default texts per book whose mean forms ``central_centroid`` (T6/L9/L10):
#: the L9 sweep measured ``central_mean`` over the 20 chunks closest to the
#: book mean. Exposed as ``--central-pool`` so the pool size can be swept with
#: three builds instead of three code changes (same rationale as ``--k``).
DEFAULT_CENTRAL_POOL = 20


class BuildNamespaceProfilesUseCase:
    """Reduce per-namespace source texts into a local prototype artifact."""

    def __init__(
        self,
        source_port: NamespaceProfileSourcePort,
        embedding_port: EmbeddingProviderPort,
        store_port: NamespaceProfileStorePort,
        *,
        model_id: str,
        profile_version: str,
        catalog_version: str,
        graph_snapshot: str,
        k: int = DEFAULT_PROFILE_K,
        central_pool: int = DEFAULT_CENTRAL_POOL,
    ) -> None:
        if k < 1:
            raise ValueError(f"k must be >= 1, got {k}")
        if central_pool < 1:
            raise ValueError(f"central_pool must be >= 1, got {central_pool}")
        self._source_port = source_port
        self._embedding_port = embedding_port
        self._store_port = store_port
        self._model_id = model_id
        self._profile_version = profile_version
        self._catalog_version = catalog_version
        self._graph_snapshot = graph_snapshot
        self._k = k
        self._central_pool = central_pool

    async def execute(
        self,
        *,
        dry_run: bool = False,
    ) -> tuple[NamespaceProfile, ...]:
        """Build and optionally persist profiles; dry-run never writes."""
        sources = await self._source_port.load_source_texts()
        built: list[NamespaceProfile] = []
        for source in sources:
            built.append(await self._build_profile(source))
        profiles = tuple(built)
        if not dry_run:
            await self._store_port.save_all(profiles)
        return profiles

    async def _build_profile(self, source: NamespaceSourceTexts) -> NamespaceProfile:
        pairs = source.identified_profile_texts
        if not pairs:
            raise ValueError(f"no source material for namespace {source.namespace.source_id}")
        texts = tuple(text for _identifier, text in pairs)
        identifiers = tuple(identifier for identifier, _text in pairs)
        batch = await self._embedding_port.embed(
            EmbeddingRequest(texts=texts, model_id=self._model_id)
        )
        vectors = tuple(vector.values for vector in batch.vectors)
        if len(vectors) != len(texts):
            raise ValueError(
                f"embedding provider returned {len(vectors)} vectors for "
                f"{len(texts)} profile texts of {source.namespace.source_id}"
            )
        # S6: k prototypes selected from the book's own texts, each one paired
        # with the stable ``kind:ordinal`` id of the text that produced it.
        # When the pool holds fewer texts than k, every text is kept (S3).
        selected = select_prototypes(vectors, self._k)
        prototypes = tuple(vectors[index] for index in selected)
        prototype_sources = tuple(identifiers[index] for index in selected)
        # The mean stays persisted as a passive diagnostic (S1/L2); the
        # prototype scoring path never reads it.
        centroid = mean_normalized(vectors)
        # T6/L10: additional field, never a replacement — ``centroid`` above
        # is untouched, and this is the L9 central_mean candidate geometry.
        central_centroid = self._central_centroid(vectors)
        return NamespaceProfile(
            namespace=source.namespace,
            centroid=centroid,
            central_centroid=central_centroid,
            prototypes=prototypes,
            prototype_sources=prototype_sources,
            k=len(prototypes),
            dimension=len(centroid),
            model_id=batch.model_id,
            profile_version=self._profile_version,
            catalog_version=self._catalog_version,
            graph_snapshot=self._graph_snapshot,
        )

    def _central_centroid(self, vectors: Sequence[tuple[float, ...]]) -> tuple[float, ...]:
        """Return the normalized mean of the ``central_pool`` closest vectors (T6/L9/L10).

        "Closest" means by cosine to the book's arithmetic mean vector — the
        L9 ``central_mean`` definition. Ties resolve to the lowest input
        index (same deterministic rule as ``select_prototypes``), so the same
        input always yields the same field, byte for byte. The selected
        vectors are averaged and normalized exactly like ``centroid``
        (``mean_normalized``). With a pool at least as large as the text set
        the result equals ``centroid`` by construction.

        Raises:
            ValueError: Propagated from ``mean_normalized`` when no meaningful
                mean can be formed.
        """
        dimension = len(vectors[0])
        mean = tuple(
            sum(vector[index] for vector in vectors) / len(vectors) for index in range(dimension)
        )
        ranked = sorted(
            range(len(vectors)),
            key=lambda index: (-cosine_similarity(vectors[index], mean), index),
        )
        closest = [vectors[index] for index in ranked[: self._central_pool]]
        return mean_normalized(closest)
