"""Post-hoc entity resolution: merge near-duplicate :Entity nodes.

After indexing, ``slugify``-based ids unify only *exact* names (same
normalized slug).  Near-identical names — ``LangGraph`` vs ``LangGraph
framework``, or ``Lang Graph`` vs ``LangGraph`` — still produce distinct
nodes because their slugs differ.  This script clusters persisted entities by
name similarity *within the same entity type*, picks a canonical node per
cluster, transfers :MENTIONS/:RELATED edges onto it, folds the duplicates'
names into its alias list, and deletes the duplicates.

This is a data-quality improvement, not a correctness fix.  Re-running is
idempotent: already-merged names are identical, so they produce no further
groups.

Usage:
    uv run python scripts/resolve_entities.py --dry-run
    uv run python scripts/resolve_entities.py --threshold 0.9
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from typing import Any

import click
from neo4j import AsyncGraphDatabase

from book_graph_rag.application.resolve_entities_use_case import ResolveEntitiesResult
from book_graph_rag.config import Settings
from book_graph_rag.domain.models import Entity
from book_graph_rag.infrastructure.resolution_wiring import build_resolve_entities_use_case

DEFAULT_THRESHOLD = 0.9

_ENRICH_CANONICAL = """
MATCH (canonical:Entity {id: $canonical_id})
SET canonical.aliases = $aliases,
    canonical.canonical_name = $canonical_name,
    canonical.description = $description
"""

_REPOINT_INCOMING_RELATED = """
MATCH (canonical:Entity {id: $canonical_id})
MATCH (src:Entity)-[r:RELATED]->(dup:Entity)
WHERE dup.id IN $dup_ids
  AND NOT src.id IN $dup_ids
  AND src.id <> $canonical_id
MERGE (src)-[r2:RELATED {type: r.type}]->(canonical)
SET r2.description = coalesce(r2.description, r.description),
    r2.source_page = coalesce(r2.source_page, r.source_page),
    r2.chunk_index = coalesce(r2.chunk_index, r.chunk_index)
"""

_REPOINT_OUTGOING_RELATED = """
MATCH (canonical:Entity {id: $canonical_id})
MATCH (dup:Entity)-[r:RELATED]->(dst:Entity)
WHERE dup.id IN $dup_ids
  AND NOT dst.id IN $dup_ids
  AND dst.id <> $canonical_id
MERGE (canonical)-[r2:RELATED {type: r.type}]->(dst)
SET r2.description = coalesce(r2.description, r.description),
    r2.source_page = coalesce(r2.source_page, r.source_page),
    r2.chunk_index = coalesce(r2.chunk_index, r.chunk_index)
"""

_REPOINT_MENTIONS = """
MATCH (canonical:Entity {id: $canonical_id})
MATCH (c:Chunk)-[m:MENTIONS]->(dup:Entity)
WHERE dup.id IN $dup_ids
MERGE (c)-[m2:MENTIONS]->(canonical)
SET m2.source_page = coalesce(m2.source_page, m.source_page)
"""

_DELETE_DUPLICATES = """
MATCH (dup:Entity)
WHERE dup.id IN $dup_ids
DETACH DELETE dup
"""


@dataclass(frozen=True)
class MergeGroup:
    """A canonical entity plus the duplicates to fold into it."""

    canonical_id: str
    duplicate_ids: list[str]
    aliases: list[str]
    canonical_name: str | None
    description: str


@dataclass(frozen=True)
class ResolutionReport:
    """Summary of an entity-resolution run."""

    total_entities: int
    duplicate_count: int
    group_count: int
    dry_run: bool


def _normalize(name: str) -> str:
    """Casefold and collapse runs of whitespace for comparison."""
    return re.sub(r"\s+", " ", name.casefold()).strip()


def _compact(name: str) -> str:
    """Strip word separators only, preserving punctuation that carries meaning.

    Whitespace, hyphens, and underscores act as word separators; everything
    else (``+``, ``#``, ``.``) is preserved so distinct names such as ``C++``
    and ``C#`` are never collapsed together.
    """
    return re.sub(r"[\s\-_]+", "", name)


def _tokens(name: str) -> set[str]:
    """Split a normalized name into word tokens.

    Only whitespace, hyphens, and underscores act as word separators; other
    punctuation (``+``, ``#``, ``.``) stays part of the token so distinct
    names such as ``C++`` and ``C#`` are not collapsed together.
    """
    return {token for token in re.split(r"[\s\-_]+", name) if token}


def name_similarity(a: str, b: str) -> float:
    """Return a 0..1 similarity for two entity names.

    Conservative by design.  A duplicate must be one of:
      * the exact same normalized name;
      * the same compact sequence (spacing/hyphen/punctuation variants like
        ``LangGraph`` vs ``Lang Graph`` vs ``Lang-Graph``);
      * near-identical word-token sets (a multi-token name with the same
        tokens, possibly reordered).
    Character-level fuzzy matching is intentionally NOT used: it merges
    distinct concepts such as ``centralized`` vs ``decentralized`` or
    ``agent`` vs ``agent1``, which degrades the graph.  Over-merging is worse
    than under-merging.
    """
    an = _normalize(a)
    bn = _normalize(b)
    if not an or not bn:
        return 0.0
    if an == bn:
        return 1.0

    an_compact = _compact(an)
    bn_compact = _compact(bn)
    if an_compact and bn_compact and an_compact == bn_compact:
        return 1.0

    tokens_a = _tokens(an)
    tokens_b = _tokens(bn)
    if not tokens_a or not tokens_b:
        return 0.0
    shared = tokens_a & tokens_b
    return 2 * len(shared) / (len(tokens_a) + len(tokens_b))


def _cluster_by_similarity(
    entities: list[Entity], threshold: float
) -> list[list[Entity]]:
    """Group entities transitively by name similarity (union-find).

    Compact forms and token sets are precomputed once per entity.  A token-
    count bound (Dice = 2*|shared|/(|A|+|B|) <= 2*min/(|A|+|B|)) skips pairs
    that cannot reach ``threshold`` before the token-set comparison, which
    keeps clustering fast without the false negatives of a character-length
    bound.
    """
    n = len(entities)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    normalized = [_normalize(entity.name) for entity in entities]
    compact = [_compact(name) for name in normalized]
    token_sets = [_tokens(name) for name in normalized]
    token_counts = [len(tokens) for tokens in token_sets]

    for i in range(n):
        for j in range(i + 1, n):
            if compact[i] and compact[j] and compact[i] == compact[j]:
                union(i, j)
                continue
            if (
                2 * min(token_counts[i], token_counts[j])
                < threshold * (token_counts[i] + token_counts[j])
            ):
                continue
            shared = token_sets[i] & token_sets[j]
            if 2 * len(shared) >= threshold * (token_counts[i] + token_counts[j]):
                union(i, j)

    clusters: dict[int, list[Entity]] = {}
    for i, entity in enumerate(entities):
        clusters.setdefault(find(i), []).append(entity)
    return list(clusters.values())


def pick_canonical(group: list[Entity]) -> Entity:
    """Choose the canonical node for a duplicate cluster.

    Prefer an entity that already carries ``canonical_name``, then the most
    aliases, then the shortest name, and finally the lexicographically smallest
    id so the choice is deterministic across runs.
    """

    def key(entity: Entity) -> tuple[int, int, int, str]:
        has_canonical = 0 if entity.canonical_name else 1
        return (has_canonical, -len(entity.aliases), len(entity.name), entity.id)

    return min(group, key=key)


def _merge_metadata(
    group: list[Entity], canonical: Entity
) -> tuple[list[str], str | None, str]:
    """Compute the merged aliases, canonical_name and description.

    Every member's name and aliases (except the canonical's own name) become
    aliases so full-text search keeps finding the merged variants.
    """
    canonical_fold = canonical.name.casefold()
    aliases: list[str] = []
    seen: set[str] = set()

    def add(value: str | None) -> None:
        if not value:
            return
        fold = value.casefold()
        if fold == canonical_fold or fold in seen:
            return
        seen.add(fold)
        aliases.append(value)

    for entity in group:
        add(entity.name)
        for alias in entity.aliases:
            add(alias)

    canonical_name = canonical.canonical_name
    if canonical_name is None:
        for entity in group:
            if entity.canonical_name:
                canonical_name = entity.canonical_name
                break

    description = canonical.description
    if not description:
        description = max((e.description for e in group), key=len, default="")

    return aliases, canonical_name, description


def build_merge_plan(
    entities: list[Entity], threshold: float = DEFAULT_THRESHOLD
) -> list[MergeGroup]:
    """Cluster entities by type + name similarity and build the merge plan."""
    by_type: dict[str, list[Entity]] = {}
    for entity in entities:
        by_type.setdefault(entity.type, []).append(entity)

    groups: list[MergeGroup] = []
    for typed in by_type.values():
        for cluster in _cluster_by_similarity(typed, threshold):
            if len(cluster) < 2:
                continue
            cluster.sort(key=lambda e: e.id)
            canonical = pick_canonical(cluster)
            duplicates = [e for e in cluster if e.id != canonical.id]
            aliases, canonical_name, description = _merge_metadata(cluster, canonical)
            groups.append(
                MergeGroup(
                    canonical_id=canonical.id,
                    duplicate_ids=[e.id for e in duplicates],
                    aliases=aliases,
                    canonical_name=canonical_name,
                    description=description,
                )
            )
    return groups


async def load_entities(driver: Any) -> list[Entity]:
    """Read every :Entity node into domain models."""
    query = """
    MATCH (n:Entity)
    RETURN n.id AS id, n.name AS name, n.type AS type,
           n.description AS description, n.source_page AS source_page,
           n.aliases AS aliases, n.canonical_name AS canonical_name
    ORDER BY n.name
    """
    entities: list[Entity] = []
    async with driver.session() as session:
        result = await session.run(query)
        async for record in result:
            entities.append(
                Entity(
                    id=record["id"],
                    name=record["name"],
                    type=record["type"],
                    description=record["description"] or "",
                    source_page=record["source_page"],
                    aliases=list(record["aliases"] or []),
                    canonical_name=record["canonical_name"],
                )
            )
    return entities


async def apply_merges(driver: Any, groups: list[MergeGroup]) -> None:
    """Re-point edges onto each canonical node and delete its duplicates.

    Each merge group runs inside one explicit transaction so a failure can
    never leave a partially-merged graph (metadata changed but duplicates
    still present, or edges re-pointed without deletion).
    """
    if not groups:
        return
    async with driver.session() as session:
        for group in groups:
            params: dict[str, Any] = {
                "canonical_id": group.canonical_id,
                "dup_ids": group.duplicate_ids,
                "aliases": group.aliases,
                "canonical_name": group.canonical_name,
                "description": group.description,
            }
            tx = await session.begin_transaction()
            try:
                await tx.run(_ENRICH_CANONICAL, params)
                await tx.run(_REPOINT_INCOMING_RELATED, params)
                await tx.run(_REPOINT_OUTGOING_RELATED, params)
                await tx.run(_REPOINT_MENTIONS, params)
                await tx.run(_DELETE_DUPLICATES, params)
                await tx.commit()
            except Exception:
                await tx.rollback()
                raise


async def run_resolution(
    driver: Any, threshold: float = DEFAULT_THRESHOLD, dry_run: bool = False
) -> ResolutionReport:
    """Load entities, plan merges, and optionally apply them."""
    entities = await load_entities(driver)
    groups = build_merge_plan(entities, threshold)
    duplicate_count = sum(len(g.duplicate_ids) for g in groups)
    if not dry_run:
        await apply_merges(driver, groups)
    return ResolutionReport(
        total_entities=len(entities),
        duplicate_count=duplicate_count,
        group_count=len(groups),
        dry_run=dry_run,
    )


def _make_driver(settings: Settings) -> Any:
    """Create an ephemeral bolt driver for the resolution run."""
    return AsyncGraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value()),
    )


def _plan_lines(entities: list[Entity], groups: list[MergeGroup]) -> list[str]:
    """Render the merge plan as reviewable lines (canonical <- duplicates)."""
    by_id = {entity.id: entity for entity in entities}
    lines: list[str] = []
    for group in sorted(groups, key=lambda g: len(g.duplicate_ids), reverse=True):
        canonical = by_id[group.canonical_id]
        duplicates = [by_id[dup_id] for dup_id in group.duplicate_ids]
        dup_names = ", ".join(dup.name for dup in duplicates)
        lines.append(
            f"[{canonical.type}] {canonical.name}  <-  {dup_names} "
            f"({len(duplicates)} dup)"
        )
    return lines


def _print_hybrid_report(result: ResolveEntitiesResult, *, dry_run: bool) -> None:
    """Emit the new JSON summary produced by the hybrid use case."""
    summary = {
        "strategy": "hybrid",
        "dry_run": dry_run,
        "auto_merge_groups": len(result.auto_merge_groups),
        "quarantine_records": len(result.quarantine_records),
        "no_merge_candidates": len(result.no_merge_candidates),
        "total_pairs_evaluated": result.total_pairs_evaluated,
        "merged_entities": sum(len(g.duplicate_ids) for g in result.auto_merge_groups),
    }
    click.echo(json.dumps(summary, indent=2))


async def run_hybrid_resolution(
    dry_run: bool,
    use_case_factory: Any | None = None,
) -> ResolveEntitiesResult:
    """Run the hybrid semantic resolution pipeline.

    ``use_case_factory`` receives ``Settings`` and returns ``(use_case, closables)``
    so tests can inject a fake use case without building real adapters.
    """
    settings = Settings.model_validate({})
    factory = use_case_factory or build_resolve_entities_use_case
    use_case, closables = await factory(settings)
    try:
        return await use_case.analyze(dry_run=dry_run)
    finally:
        for closable in closables:
            if hasattr(closable, "close"):
                await closable.close()


async def _run_main(threshold: float, dry_run: bool) -> None:
    """Single-entry coroutine so the event loop stays open for cleanup."""
    settings = Settings.model_validate({})
    driver = _make_driver(settings)
    try:
        entities = await load_entities(driver)
        groups = build_merge_plan(entities, threshold)
        if not dry_run:
            await apply_merges(driver, groups)
    finally:
        await driver.close()
    duplicate_count = sum(len(g.duplicate_ids) for g in groups)
    if dry_run:
        click.echo(
            f"[dry-run] entities={len(entities)} "
            f"duplicates={duplicate_count} groups={len(groups)}"
        )
        for line in _plan_lines(entities, groups):
            click.echo(f"  {line}")
    else:
        click.echo(
            f"[done] merged {duplicate_count} duplicates into "
            f"{len(groups)} canonical groups (entities={len(entities)})"
        )


@click.command()
@click.option(
    "--threshold",
    type=click.FloatRange(0.5, 1.0),
    default=DEFAULT_THRESHOLD,
    show_default=True,
    help="Name-similarity threshold for merging (0..1).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Preview the merge plan without writing to Neo4j.",
)
def main(threshold: float, dry_run: bool) -> None:
    """Merge near-duplicate :Entity nodes."""
    if os.environ.get("RESOLUTION_STRATEGY") == "hybrid":
        result = asyncio.run(run_hybrid_resolution(dry_run=dry_run))
        _print_hybrid_report(result, dry_run=dry_run)
        return
    asyncio.run(_run_main(threshold, dry_run))


if __name__ == "__main__":
    main()
