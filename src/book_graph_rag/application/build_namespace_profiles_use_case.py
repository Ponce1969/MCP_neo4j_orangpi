"""Application use case: build namespace routing profiles read-only.

This use case reads source material, embeds it locally, reduces it into one
normalized centroid per namespace, and persists a local artifact. It never
mutates Neo4j; only the configured local profile store is written.
"""

from __future__ import annotations

from book_graph_rag.domain.routing_models import (
    NamespaceProfile,
    mean_normalized,
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


class BuildNamespaceProfilesUseCase:
    """Reduce per-namespace source texts into a local centroid artifact."""

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
    ) -> None:
        self._source_port = source_port
        self._embedding_port = embedding_port
        self._store_port = store_port
        self._model_id = model_id
        self._profile_version = profile_version
        self._catalog_version = catalog_version
        self._graph_snapshot = graph_snapshot

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
        texts = source.ordered_profile_texts
        if not texts:
            raise ValueError(f"no source material for namespace {source.namespace.source_id}")
        batch = await self._embedding_port.embed(
            EmbeddingRequest(texts=texts, model_id=self._model_id)
        )
        vectors = tuple(vector.values for vector in batch.vectors)
        centroid = mean_normalized(vectors)
        return NamespaceProfile(
            namespace=source.namespace,
            centroid=centroid,
            dimension=len(centroid),
            model_id=batch.model_id,
            profile_version=self._profile_version,
            catalog_version=self._catalog_version,
            graph_snapshot=self._graph_snapshot,
        )
