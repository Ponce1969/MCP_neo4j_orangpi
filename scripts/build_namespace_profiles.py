"""Build the read-only namespace routing profile artifact.

Reads source material for every active catalog namespace from Neo4j
(parameterized MATCH statements only), embeds it with the local
sentence-transformers model, reduces each namespace to one normalized
centroid, and writes a local JSON artifact. Includes ``--dry-run``.

The graph is never mutated; only the local artifact path is written.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import click

from book_graph_rag.application.build_namespace_profiles_use_case import (
    BuildNamespaceProfilesUseCase,
)
from book_graph_rag.config import Settings
from book_graph_rag.domain.routing_models import NamespaceProfile
from book_graph_rag.infrastructure.catalog_loader import CatalogLoader
from book_graph_rag.infrastructure.json_namespace_profile_store import (
    JsonNamespaceProfileStore,
)
from book_graph_rag.infrastructure.neo4j_namespace_profile_source import (
    Neo4jNamespaceProfileSource,
)
from book_graph_rag.infrastructure.sentence_transformer_adapter import (
    SentenceTransformerAdapter,
)


@click.command()
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Build profiles in memory and print a summary without writing.",
)
@click.option("--model-id", default=None, help="Embedding model id (default: settings).")
@click.option("--profile-version", default=None, help="Profile version (default: settings).")
@click.option(
    "--catalog-version", default=None, help="Catalog version (default: from catalog.yaml)."
)
@click.option(
    "--graph-snapshot",
    default="",
    help="Snapshot marker for provenance (e.g. a commit or backup id).",
)
@click.option(
    "--output",
    type=Path,
    default=None,
    help="Artifact path (default: settings.namespace_profile_store_path).",
)
def build_namespace_profiles(
    dry_run: bool,
    model_id: str | None,
    profile_version: str | None,
    catalog_version: str | None,
    graph_snapshot: str,
    output: Path | None,
) -> None:
    """Build the local namespace routing profile artifact (read-only)."""
    try:
        settings = Settings.model_validate({})
    except Exception as exc:  # noqa: BLE001
        click.echo(f"Configuration error: {exc}", err=True)
        sys.exit(1)

    catalog = CatalogLoader(settings.catalog_path).load()
    embedding = SentenceTransformerAdapter(settings)
    store = JsonNamespaceProfileStore(output or settings.namespace_profile_store_path)

    async def _build() -> tuple[NamespaceProfile, ...]:
        """Create the driver inside the event loop so open/close share it."""
        source_adapter = Neo4jNamespaceProfileSource(settings, CatalogLoader(settings.catalog_path))
        use_case = BuildNamespaceProfilesUseCase(
            source_adapter,
            embedding,
            store,
            model_id=model_id or settings.embedding_model_id,
            profile_version=profile_version or settings.namespace_profile_version,
            catalog_version=catalog_version or str(catalog.version),
            graph_snapshot=graph_snapshot,
        )
        try:
            return await use_case.execute(dry_run=dry_run)
        finally:
            await source_adapter.close()

    try:
        profiles = asyncio.run(_build())
    except Exception as exc:  # noqa: BLE001
        click.echo(f"Profile build error: {exc}", err=True)
        sys.exit(2)

    human = "dry-run" if dry_run else "wrote"
    click.echo(f"profiles built ({human}): {len(profiles)}")
    for profile in profiles:
        click.echo(
            f"  {profile.namespace.source_id}: dim={profile.dimension} "
            f"model={profile.model_id} version={profile.profile_version}"
        )
    if not dry_run:
        click.echo(f"artifact: {store.path}")


if __name__ == "__main__":
    build_namespace_profiles()
