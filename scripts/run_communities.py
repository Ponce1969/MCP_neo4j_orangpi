"""Offline runner for community detection and summarization.

Loads the :Entity/:RELATED graph from Neo4j, runs Leiden clustering at four
resolutions (C0-C3), and summarizes each community bottom-up (GraphRAG-style):
leaf communities from raw entities, coarser communities from their children's
already-synthesized summaries.  Re-running replaces previous summaries cleanly.

Hardening features:
- Bottom-up strict: levels processed finest-first (3 -> 2 -> 1 -> 0) so a parent
  is only summarized after its children.
- Checkpointing: communities already summarized in Neo4j are skipped; each new
  summary is persisted immediately, so a crash/rate-limit mid-run loses nothing.
- Resilience: a failed leaf cascades a skip to its ancestors (no incomplete
  parent summaries).  Retries/backoff (503/429/timeout) and JSON self-healing
  live in the LLM adapter.

Usage:
    uv run python scripts/run_communities.py run
    uv run python scripts/run_communities.py run --fresh   # clear all first
"""

from __future__ import annotations

import asyncio
import logging
import warnings

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

# Third-party warnings that add no signal to the community pipeline output are
# suppressed so the terminal stays clean:
# - numba: FNV hashing falls back to siphash24 (internal, harmless).
# - graspologic: Leiden excludes degree-0 (isolated) nodes from its partitions;
#   those nodes are still covered by the level-0 global summary.
warnings.filterwarnings("ignore", category=UserWarning, module="numba.*")
warnings.filterwarnings("ignore", category=UserWarning, module="graspologic.*")

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
    fresh: bool = False,
) -> None:
    """Core orchestration: detect communities, summarize bottom-up, persist.

    Bottom-up map-reduce (GraphRAG-style): leaf communities (finest level) are
    summarized from raw entities; every coarser community is summarized from the
    already-synthesized summaries of its immediately-finer child communities.
    This keeps each LLM call within the context window regardless of community
    size.  Levels are processed finest-first (3 -> 2 -> 1 -> 0) so a parent's
    child summaries already exist when the parent is summarized.

    Checkpointing: communities already persisted in Neo4j are skipped (printing a
    [Skipped] line); every freshly summarized community is written immediately so
    a failure mid-run does not discard prior progress.  A failed leaf cascades a
    skip to its ancestors, avoiding incomplete parent summaries.
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

    # -- Checkpointing: load already-persisted summaries (unless --fresh). --
    existing_by_id: dict[str, CommunitySummary] = {}
    if fresh:
        await write_port.clear_summaries()
        click.echo("Cleared existing summaries (--fresh)")
    else:
        for level in communities_by_level:
            for summary in await read_port.get_summaries_by_level(level):
                existing_by_id[summary.id] = summary
        if existing_by_id:
            click.echo(f"Checkpoint: {len(existing_by_id)} communities already summarized")

    semaphore = asyncio.Semaphore(settings.summary_max_concurrency)
    done_counter = 0
    skipped_count = 0
    failed_count = 0
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
        done_counter += 1
        click.echo(
            f"[{done_counter}/{total_communities}] summarizing "
            f"level {level} community ({len(community_ids)} entities, "
            f"{len(children)} children)",
            err=True,
        )
        async with semaphore:
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
        # Checkpoint immediately: persist as soon as a summary is produced.
        summary = CommunitySummary(
            level=level,
            summary=summary_text,
            entity_ids=community_ids,
            parent_id=parent_id,
        )
        await write_port.upsert_summary(summary)
        return summary

    # Process finest level first so child summaries exist before parents.
    for level in sorted(assignments.keys(), reverse=True):
        level_tasks = []
        for community_ids, parent_id in assignments[level]:
            cid = _community_summary_id(level, community_ids)
            children = child_map.get(cid, [])
            # Checkpoint: skip already-summarized communities.
            existing = existing_by_id.get(cid)
            if existing is not None and existing.summary:
                summaries_by_id[cid] = existing
                done_counter += 1
                skipped_count += 1
                click.echo(
                    f"[{done_counter}/{total_communities}] [Skipped] level {level} "
                    f"community ({len(community_ids)} entities) already summarized",
                    err=True,
                )
                continue
            # Bottom-up strict: skip a parent if any child failed this run.
            if children and any(c not in summaries_by_id for c in children):
                done_counter += 1
                skipped_count += 1
                click.echo(
                    f"[{done_counter}/{total_communities}] [Skipped] level {level} "
                    f"community ({len(community_ids)} entities) parent of failed child",
                    err=True,
                )
                continue
            level_tasks.append(
                _summarize_node(cid, level, community_ids, parent_id, children)
            )
        results = await asyncio.gather(*level_tasks, return_exceptions=True)
        level_failed = 0
        for result in results:
            if isinstance(result, CommunitySummary):
                summaries_by_id[result.id] = result
            elif isinstance(result, Exception):
                level_failed += 1
                failed_count += 1
                click.echo(f"ERROR: community summary failed: {result}", err=True)
        if level_failed:
            click.echo(
                f"WARNING: level {level}: {level_failed}/{len(level_tasks)} "
                f"communities failed",
                err=True,
            )

    summaries = list(summaries_by_id.values())
    for level, communities in communities_by_level.items():
        click.echo(f"Level {level}: {len(communities)} communities")
    click.echo(
        f"Done: {len(summaries)} summaries available "
        f"({skipped_count} skipped from checkpoint, {failed_count} failed)"
    )


async def _run_main(fresh: bool = False) -> None:
    """Single-entry coroutine so the event loop stays open for cleanup."""
    settings = Settings()
    adapter = Neo4jCommunityAdapter(settings)
    llm_port: LLMSummaryPort = LLMAdapter(settings)
    try:
        await adapter.ensure_indexes()
        await _run_communities(adapter, adapter, llm_port, settings, fresh=fresh)
    finally:
        await adapter.close()


@cli.command()
@click.option("--fresh", is_flag=True, help="Clear all existing summaries before running.")
def run(fresh: bool) -> None:
    """Run the community detection + summarization pipeline."""
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    asyncio.run(_run_main(fresh=fresh))


if __name__ == "__main__":
    cli()
