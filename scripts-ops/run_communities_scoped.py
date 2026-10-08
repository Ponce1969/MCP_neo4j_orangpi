"""Community detection + summarization SCOPED to a single knowledge namespace.

Self-contained bottom-up pipeline (GraphRAG-style) that runs ENTIRELY on the active entities
of one book: ``id STARTS WITH <corpus>:<source>:`` and ``merged_into`` NULL/empty, plus the
RELATED edges internal to that set. It does NOT reload the global graph inside the orchestrator,
so the scope is respected. The namespace comes from ``--namespace corpus:source`` and defaults
to ``knowledge:graphrag-agentic``, the book this tool was first written for.

Checkpoint scoping: community ids are hashes of namespaced entity ids (cannot collide across
books) and already-persisted summaries are read via ``get_summaries_by_level(level, scope=...)``
so only this book's summaries are resumed. Runs are resumable: every summary is persisted
immediately (upsert MERGE).

Usage (run from the repo on the OrangePi with the project venv):
  uv run python run_communities_scoped.py --detect-only            # Leiden counts, no LLM
  uv run python run_communities_scoped.py --detect-only --namespace knowledge:ai-engineering-huyen
  uv run python run_communities_scoped.py --run                    # detect + summarize (resumable)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import warnings

from neo4j import Driver, GraphDatabase

from book_graph_rag.config import Settings
from book_graph_rag.domain.mcp_security import ScopeContext
from book_graph_rag.domain.models import CommunitySummary, Entity, Relationship
from book_graph_rag.domain.namespaces import SEPARATOR, SourceNamespace
from book_graph_rag.infrastructure.community_adapter import Neo4jCommunityAdapter
from book_graph_rag.infrastructure.community_clustering import (
    CommunityDetectionError,
    _community_summary_id,
    assign_parent_ids,
    build_entity_graph,
    missing_community_ids,
    run_leiden,
    select_leiden_backend,
)
from book_graph_rag.infrastructure.llm_adapter import LLMAdapter
from book_graph_rag.ports.community_read_port import CommunityReadPort
from book_graph_rag.ports.community_write_port import CommunityWritePort
from book_graph_rag.ports.llm_summary_port import LLMSummaryPort

warnings.filterwarnings("ignore", category=UserWarning, module="numba.*")
warnings.filterwarnings("ignore", category=UserWarning, module="graspologic.*")

DEFAULT_NAMESPACE = "knowledge:graphrag-agentic"
LEIDEN_RESOLUTIONS = [0.1, 0.5, 1.0]


def _resolve_namespace(value: str) -> tuple[SourceNamespace, ScopeContext, str]:
    """Turn a ``--namespace corpus:source`` value into the namespace, its scope and its prefix.

    The grammar is one corpus slug, one colon, one source slug; anything else is refused here
    rather than silently scoping a run to the wrong book.
    """
    corpus, separator, source = value.partition(SEPARATOR)
    if not separator or not corpus or not source or SEPARATOR in source:
        raise SystemExit(f"--namespace must be 'corpus:source' (exactly one colon), got {value!r}")
    namespace = SourceNamespace(corpus=corpus, source=source)
    return namespace, ScopeContext(source=namespace), f"{namespace.source_id}:"


def _active_ids(driver: Driver, prefix: str) -> set[str]:
    with driver.session() as s:
        rows = s.run(
            "MATCH (n:Entity) WHERE n.id STARTS WITH $prefix "
            "AND (n.merged_into IS NULL OR n.merged_into = '') RETURN n.id AS id",
            prefix=prefix,
        )
        return {r["id"] for r in rows}


async def _load_scoped(settings: Settings, prefix: str) -> tuple[list[Entity], list[Relationship]]:
    adapter = Neo4jCommunityAdapter(settings)
    try:
        all_entities, all_rels = await adapter.load_entity_graph()
    finally:
        await adapter.close()
    driver = GraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value()),
    )
    try:
        active = _active_ids(driver, prefix)
    finally:
        driver.close()
    entities = [e for e in all_entities if e.id in active]
    rels = [r for r in all_rels if r.source_entity_id in active and r.target_entity_id in active]
    return entities, rels


def _detect(
    entities: list[Entity], relationships: list[Relationship]
) -> dict[int, list[list[str]]]:
    graph = build_entity_graph(entities, relationships)
    all_ids = list(graph.nodes())
    by_level: dict[int, list[list[str]]] = {0: [all_ids]}
    backend = select_leiden_backend()
    for level, resolution in enumerate(LEIDEN_RESOLUTIONS, start=1):
        by_level[level] = run_leiden(graph, resolution, backend)
    return by_level


async def _run_scoped_communities(
    read_port: CommunityReadPort,
    write_port: CommunityWritePort,
    llm_port: LLMSummaryPort,
    settings: Settings,
    entities: list[Entity],
    relationships: list[Relationship],
    scope: ScopeContext,
) -> None:
    """Bottom-up summary orchestration scoped to one namespace (port re-load free).

    Faithful copy of scripts/run_communities._run_communities but the entity graph
    and summaries are pre-scoped: entities/relationships arrive filtered and
    checkpoint reads use get_summaries_by_level(level, scope=scope).
    """
    graph = build_entity_graph(entities, relationships)
    entity_map = {entity.id: entity for entity in entities}
    all_ids = list(graph.nodes())

    communities_by_level: dict[int, list[list[str]]] = {0: [all_ids]}
    for level, resolution in enumerate(LEIDEN_RESOLUTIONS, start=1):
        communities_by_level[level] = run_leiden(graph, resolution, backend=select_leiden_backend())

    total_communities = sum(len(c) for c in communities_by_level.values())

    # The checkpoint read happens before the cost guard on purpose: the guard bounds the LLM calls
    # a run will make, and those are the *missing* summaries, not every community detection sees.
    existing_by_id: dict[str, CommunitySummary] = {}
    for level in communities_by_level:
        for summary in await read_port.get_summaries_by_level(level, scope=scope):
            existing_by_id[summary.id] = summary
    missing_communities = missing_community_ids(communities_by_level, set(existing_by_id))
    if len(missing_communities) > settings.community_max_calls:
        raise CommunityDetectionError(
            f"Communities still missing a summary ({len(missing_communities)} of "
            f"{total_communities} detected) exceed community_max_calls "
            f"({settings.community_max_calls}). Aborting to avoid runaway LLM costs."
        )
    if existing_by_id:
        print(
            f"Checkpoint: {len(existing_by_id)} communities already summarized; "
            f"{len(missing_communities)} to summarize in this run"
        )

    assignments = assign_parent_ids(communities_by_level)
    child_map: dict[str, list[str]] = {}
    for level, communities in assignments.items():
        for community_ids, parent_id in communities:
            cid = _community_summary_id(level, community_ids)
            if parent_id is not None:
                child_map.setdefault(parent_id, []).append(cid)

    semaphore = asyncio.Semaphore(settings.summary_max_concurrency)
    done_counter = 0
    skipped_count = 0
    failed_count = 0
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
        print(
            f"[{done_counter}/{total_communities}] summarizing "
            f"level {level} community ({len(community_ids)} entities, "
            f"{len(children)} children)",
            file=sys.stderr,
        )
        async with semaphore:
            if children:
                child_texts = [summaries_by_id[c].summary for c in children if c in summaries_by_id]
                summary_text = await llm_port.generate_summary_from_children(child_texts, level)
            else:
                community_ids_set = set(community_ids)
                community_entities = [entity_map[eid] for eid in community_ids if eid in entity_map]
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
        summary = CommunitySummary(
            level=level,
            summary=summary_text,
            entity_ids=community_ids,
            parent_id=parent_id,
        )
        await write_port.upsert_summary(summary)
        return summary

    for level in sorted(assignments.keys(), reverse=True):
        level_tasks = []
        for community_ids, parent_id in assignments[level]:
            cid = _community_summary_id(level, community_ids)
            children = child_map.get(cid, [])
            existing = existing_by_id.get(cid)
            if existing is not None and existing.summary:
                summaries_by_id[cid] = existing
                done_counter += 1
                skipped_count += 1
                print(
                    f"[{done_counter}/{total_communities}] [Skipped] level {level} "
                    f"community ({len(community_ids)} entities) already summarized",
                    file=sys.stderr,
                )
                continue
            if children and any(c not in summaries_by_id for c in children):
                done_counter += 1
                skipped_count += 1
                print(
                    f"[{done_counter}/{total_communities}] [Skipped] level {level} "
                    f"community ({len(community_ids)} entities) parent of failed child",
                    file=sys.stderr,
                )
                continue
            level_tasks.append(_summarize_node(cid, level, community_ids, parent_id, children))
        results = await asyncio.gather(*level_tasks, return_exceptions=True)
        level_failed = 0
        for result in results:
            if isinstance(result, CommunitySummary):
                summaries_by_id[result.id] = result
            elif isinstance(result, Exception):
                level_failed += 1
                failed_count += 1
                print(f"ERROR: community summary failed: {result}", file=sys.stderr)
        if level_failed:
            print(
                f"WARNING: level {level}: {level_failed}/{len(level_tasks)} communities failed",
                file=sys.stderr,
            )

    for level, level_comms in communities_by_level.items():
        print(f"Level {level}: {len(level_comms)} communities")
    print(
        f"Done: {len(summaries_by_id)} summaries available "
        f"({skipped_count} skipped from checkpoint, {failed_count} failed)"
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--detect-only", action="store_true", help="Leiden counts, no LLM calls")
    mode.add_argument("--run", action="store_true", help="detect + summarize (resumable)")
    parser.add_argument(
        "--namespace",
        default=DEFAULT_NAMESPACE,
        help=f"Book to scope the run to: corpus:source (default {DEFAULT_NAMESPACE}).",
    )
    args = parser.parse_args()

    namespace, scope, prefix = _resolve_namespace(args.namespace)
    settings = Settings.model_validate({})
    entities, relationships = await _load_scoped(settings, prefix)
    print(
        f"SCOPED load [{namespace.source_id}]: {len(entities)} active entities, "
        f"{len(relationships)} internal RELATED"
    )
    if not entities:
        print(f"No active entities for {namespace.source_id}; aborting")
        return 1

    by_level = _detect(entities, relationships)
    total = sum(len(c) for c in by_level.values())
    for level, comms in by_level.items():
        print(f"Level {level}: {len(comms)} communities")
    print(f"TOTAL communities: {total} (guard community_max_calls={settings.community_max_calls})")
    if args.detect_only:
        print("DETECT ONLY - no summaries written")
        return 0

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    adapter = Neo4jCommunityAdapter(settings)
    llm_port: LLMSummaryPort = LLMAdapter(settings)
    try:
        await adapter.ensure_indexes()
        await _run_scoped_communities(
            adapter, adapter, llm_port, settings, entities, relationships, scope
        )
    finally:
        await adapter.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
