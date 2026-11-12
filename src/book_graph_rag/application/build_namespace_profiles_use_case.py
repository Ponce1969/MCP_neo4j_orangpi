"""Application use case: build namespace routing profiles read-only.

This use case reads source material, embeds it locally, selects ``k``
representative prototypes per namespace by deterministic farthest-point
sampling (S3/S6), keeps the normalized mean of every vector as a diagnostic
centroid, and persists a local artifact. It never mutates Neo4j; only the
configured local profile store is written.
"""

from __future__ import annotations

from book_graph_rag.domain.routing_models import (
    NamespaceProfile,
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
    ) -> None:
        if k < 1:
            raise ValueError(f"k must be >= 1, got {k}")
        self._source_port = source_port
        self._embedding_port = embedding_port
        self._store_port = store_port
        self._model_id = model_id
        self._profile_version = profile_version
        self._catalog_version = catalog_version
        self._graph_snapshot = graph_snapshot
        self._k = k

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
        return NamespaceProfile(
            namespace=source.namespace,
            centroid=centroid,
            prototypes=prototypes,
            prototype_sources=prototype_sources,
            k=len(prototypes),
            dimension=len(centroid),
            model_id=batch.model_id,
            profile_version=self._profile_version,
            catalog_version=self._catalog_version,
            graph_snapshot=self._graph_snapshot,
        )
