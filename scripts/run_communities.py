"""Offline runner for community detection and summarization.

Loads the :Entity/:RELATED graph from Neo4j, runs Leiden clustering at four
resolutions (C0-C3), asks the LLM to summarize each community, and persists
:CommunitySummary nodes.  Re-running replaces previous summaries cleanly.

Usage:
    uv run python scripts/run_communities.py run
"""

from __future__ import annotations

import asyncio
import logging

import click

from book_graph_rag.config import Settings
from book_graph_rag.domain.models import CommunitySummary
from book_graph_rag.infrastructure.community_adapter import Neo4jCommunityAdapter
from book_graph_rag.infrastructure.community_clustering import (
    CommunityDetectionError,
    _community_summary_id,
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
    """Core orchestration: detect communities, summarize bottom-up, persist.

    Bottom-up map-reduce (GraphRAG-style): leaf communities (finest level) are
    summarized from raw entities; every coarser community is summarized from the
    already-synthesized summaries of its immediately-finer child communities.
    This keeps each LLM call within the context window regardless of community
    size.  Levels are processed finest-first (3 -> 2 -> 1 -> 0) so a parent's
    child summaries already exist when the parent is summarized.
    """
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

    # Build the child map: parent_id -> [child community ids].  A community's id
    # is the stable hash _community_summary_id(level, sorted(entity_ids)).
    child_map: dict[str, list[str]] = {}
    for level, communities in assignments.items():
        for community_ids, parent_id in communities:
            cid = _community_summary_id(level, community_ids)
            if parent_id is not None:
                child_map.setdefault(parent_id, []).append(cid)

    semaphore = asyncio.Semaphore(settings.summary_max_concurrency)
    done_counter = 0
    # Summaries keyed by community id; populated finest-first so parents can read
    # their children's summaries when they are processed.
    summaries_by_id: dict[str, CommunitySummary] = {}

    async def _summarize_node(
        cid: str,
        level: int,
        community_ids: list[str],
        parent_id: str | None,
        children: list[str],
    ) -> CommunitySummary:
        nonlocal done_counter
        async with semaphore:
            done_counter += 1
            click.echo(
                f"[{done_counter}/{total_communities}] summarizing "
                f"level {level} community ({len(community_ids)} entities, "
                f"{len(children)} children)",
                err=True,
            )
            if children:
                # Parent: summarize from already-synthesized child summaries.
                child_texts = [
                    summaries_by_id[c].summary for c in children if c in summaries_by_id
                ]
                summary_text = await llm_port.generate_summary_from_children(
                    child_texts, level
                )
            else:
                # Leaf: summarize from raw entities/relationships of the community.
                community_ids_set = set(community_ids)
                community_entities = [
                    entity_map[eid] for eid in community_ids if eid in entity_map
                ]
                community_relationships = [
                    relationship
                    for relationship in relationships
                    if relationship.source_entity_id in community_ids_set
                    and relationship.target_entity_id in community_ids_set
                ]
                summary_text = await llm_port.generate_community_summary(
                    community_entities, community_relationships, level
                )
            if settings.summary_request_delay > 0:
                await asyncio.sleep(settings.summary_request_delay)
        return CommunitySummary(
            level=level,
            summary=summary_text,
            entity_ids=community_ids,
            parent_id=parent_id,
        )

    # Process finest level first so child summaries exist before parents.
    for level in sorted(assignments.keys(), reverse=True):
        level_tasks = [
            _summarize_node(
                _community_summary_id(level, community_ids),
                level,
                community_ids,
                parent_id,
                child_map.get(_community_summary_id(level, community_ids), []),
            )
            for community_ids, parent_id in assignments[level]
        ]
        results = await asyncio.gather(*level_tasks, return_exceptions=True)

        failed = 0
        for result in results:
            if isinstance(result, CommunitySummary):
                summaries_by_id[result.id] = result
            else:
                failed += 1
                click.echo(f"ERROR: community summary failed: {result}", err=True)
        if failed:
            click.echo(
                f"WARNING: level {level}: {failed}/{len(results)} communities failed",
                err=True,
            )

    summaries = list(summaries_by_id.values())
    await write_port.clear_summaries()
    await write_port.upsert_summaries(summaries)

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
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    asyncio.run(_run_main())


if __name__ == "__main__":
    cli()
