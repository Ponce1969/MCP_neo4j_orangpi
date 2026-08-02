"""Offline runner for community detection and summarization.

Loads the :Entity/:RELATED graph from Neo4j, runs Leiden clustering at four
resolutions (C0-C3), asks the LLM to summarize each community, and persists
:CommunitySummary nodes.  Re-running replaces previous summaries cleanly.

Usage:
    uv run python scripts/run_communities.py run
"""

from __future__ import annotations

import asyncio

import click

from book_graph_rag.config import Settings
from book_graph_rag.domain.models import CommunitySummary
from book_graph_rag.infrastructure.community_adapter import Neo4jCommunityAdapter
from book_graph_rag.infrastructure.community_clustering import (
    CommunityDetectionError,
    assign_parent_ids,
    build_entity_graph,
    run_leiden,
    select_leiden_backend,
)
from book_graph_rag.infrastructure.llm_adapter import LLMAdapter
from book_graph_rag.ports.community_read_port import CommunityReadPort
from book_graph_rag.ports.community_write_port import CommunityWritePort
from book_graph_rag.ports.llm_summary_port import LLMSummaryPort

# Resolutions for Leiden levels 1-3.  Level 0 is the whole graph.
_LEIDEN_RESOLUTIONS = [0.1, 0.5, 1.0]


@click.group()
def cli() -> None:
    """Offline community-summary pipeline."""


async def _run_communities(
    read_port: CommunityReadPort,
    write_port: CommunityWritePort,
    llm_port: LLMSummaryPort,
    settings: Settings,
) -> None:
    """Core orchestration: detect communities, summarize, persist."""
    backend = select_leiden_backend()
    entities, relationships = await read_port.load_entity_graph()
    click.echo(f"Loaded {len(entities)} entities and {len(relationships)} relationships")

    graph = build_entity_graph(entities, relationships)
    entity_map = {entity.id: entity for entity in entities}
    all_ids = list(graph.nodes())

    # Level 0 is the whole graph; levels 1-3 are Leiden resolutions.
    communities_by_level: dict[int, list[list[str]]] = {0: [all_ids]}
    for level, resolution in enumerate(_LEIDEN_RESOLUTIONS, start=1):
        communities_by_level[level] = run_leiden(graph, resolution, backend)

    total_communities = sum(len(communities) for communities in communities_by_level.values())
    if total_communities > settings.community_max_calls:
        raise CommunityDetectionError(
            f"Total communities ({total_communities}) exceeds community_max_calls "
            f"({settings.community_max_calls}). Aborting to avoid runaway LLM costs."
        )

    assignments = assign_parent_ids(communities_by_level)
    semaphore = asyncio.Semaphore(settings.summary_max_concurrency)

    async def _summarize(
        level: int, community_ids: list[str], parent_id: str | None
    ) -> CommunitySummary:
        community_ids_set = set(community_ids)
        community_entities = [entity_map[eid] for eid in community_ids if eid in entity_map]
        community_relationships = [
            relationship
            for relationship in relationships
            if relationship.source_entity_id in community_ids_set
            and relationship.target_entity_id in community_ids_set
        ]
        async with semaphore:
            summary_text = await llm_port.generate_community_summary(
                community_entities, community_relationships, level
            )
        return CommunitySummary(
            level=level,
            summary=summary_text,
            entity_ids=community_ids,
            parent_id=parent_id,
        )

    tasks = [
        _summarize(level, community_ids, parent_id)
        for level, communities in assignments.items()
        for community_ids, parent_id in communities
    ]
    summaries = await asyncio.gather(*tasks)

    await write_port.clear_summaries()
    await write_port.upsert_summaries(list(summaries))

    for level, communities in communities_by_level.items():
        click.echo(f"Level {level}: {len(communities)} communities")
    click.echo(f"Persisted {len(summaries)} CommunitySummary nodes")


async def _run_main() -> None:
    """Single-entry coroutine so the event loop stays open for cleanup."""
    settings = Settings()
    adapter = Neo4jCommunityAdapter(settings)
    llm_port: LLMSummaryPort = LLMAdapter(settings)
    try:
        await _run_communities(adapter, adapter, llm_port, settings)
    finally:
        await adapter.close()


@cli.command()
def run() -> None:
    """Run the community detection + summarization pipeline."""
    asyncio.run(_run_main())


if __name__ == "__main__":
    cli()
