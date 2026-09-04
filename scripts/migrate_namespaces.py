"""Migrate the existing global entity/book ids to namespaced ids.

The current graph stores *global* ids: ``:Entity`` nodes carry ``slug-type``
ids and ``:Book`` nodes carry a title slug (or a ``(title, author)`` hash), with
no corpus/source component.  This script rewrites those ids in place to the
namespaced form ``corpus:source:slug-type`` (entities) and ``corpus:source``
(books) defined by :mod:`book_graph_rag.domain.namespaces`.

Re-pointing note
----------------
``:MENTIONS`` (``Chunk -> Entity``), ``:RELATED`` (``Entity -> Entity``) and the
editorial ``HAS_*``/``CONTAINS`` edges are **node references**, not id
properties.  Because this migration mutates the ``id``/``book_id`` properties
of the existing nodes *in place* (never creates a replacement node), every edge
keeps pointing at the same node and no explicit edge re-pointing is required.
The plan still verifies that every entity id referenced by a relationship is
covered by the migration, so no edge can silently dangle.

This is a data-migration script, not a correctness fix.  Re-running is a no-op:
already-namespaced ids are recognized and skipped.

Usage:
    uv run python scripts/migrate_namespaces.py                 # dry-run (default)
    uv run python scripts/migrate_namespaces.py --apply         # write (confirmation)
    uv run python scripts/migrate_namespaces.py --apply --yes   # write (scripted)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import click
from neo4j import AsyncGraphDatabase

from book_graph_rag.config import Settings
from book_graph_rag.domain.models import CommunitySummary, Entity, _community_summary_id
from book_graph_rag.domain.namespaces import SourceNamespace, UnknownNamespaceError
from book_graph_rag.infrastructure.catalog_loader import CatalogLoader

_SAMPLE_LIMIT = 5

_MIGRATE_ENTITY = """
MATCH (n:Entity {id: $old_id})
SET n.id = $new_id,
    n.aliases = $aliases
"""

_MIGRATE_BOOK = """
MATCH (b:Book {id: $old_id})
SET b.id = $new_id
"""

_MIGRATE_CHUNK = """
MATCH (c:Chunk {chunk_index: $chunk_index, book_id: $old_book_id})
SET c.book_id = $new_book_id
"""

_MIGRATE_SUMMARY = """
MATCH (c:CommunitySummary {id: $old_id})
SET c.id = $new_id,
    c.entity_ids = $entity_ids,
    c.parent_id = $parent_id
"""


@dataclass(frozen=True)
class EntityIdMapping:
    """Result of mapping one persisted entity id to a namespaced id."""

    old_id: str
    new_id: str | None
    slug: str | None
    entity_type: str
    status: str  # "migrate" | "already-namespaced" | "unresolved"
    reason: str | None = None


@dataclass(frozen=True)
class BookIdMapping:
    """Result of mapping one persisted book id to the namespaced book id."""

    old_id: str
    new_id: str | None
    status: str  # "migrate" | "already-namespaced" | "unresolved"
    reason: str | None = None


@dataclass(frozen=True)
class EntityMigration:
    """A single ``:Entity`` node id rewrite plus its folded aliases."""

    old_id: str
    new_id: str
    aliases: list[str]


@dataclass(frozen=True)
class BookMigration:
    """A single ``:Book`` node id rewrite."""

    old_id: str
    new_id: str


@dataclass(frozen=True)
class ChunkMigration:
    """A single ``:Chunk.book_id`` property rewrite."""

    chunk_index: int
    old_book_id: str
    new_book_id: str


@dataclass(frozen=True)
class SummaryMigration:
    """A ``:CommunitySummary`` rewrite: id, entity ids, and remapped parent."""

    old_id: str
    new_id: str
    entity_ids: list[str]
    parent_id: str | None


@dataclass(frozen=True)
class ReviewItem:
    """An unresolved or ambiguous case that must be decided by a human."""

    kind: str
    identifier: str
    reason: str


@dataclass(frozen=True)
class MigrationPlan:
    """The full re-identification plan produced by ``build_migration_plan``."""

    entities: list[EntityMigration]
    books: list[BookMigration]
    chunks: list[ChunkMigration]
    summaries: list[SummaryMigration]
    review: list[ReviewItem]

    @property
    def is_noop(self) -> bool:
        """True when there is nothing to write (already namespaced)."""
        return not (self.entities or self.books or self.chunks or self.summaries)


@dataclass(frozen=True)
class GraphSnapshot:
    """Everything the migration needs to read from Neo4j."""

    entities: list[Entity]
    book_ids: list[str]
    chunks: list[tuple[int, str | None]]
    summaries: list[CommunitySummary]
    relationship_entity_ids: set[str]
    edge_counts: dict[str, int]


@dataclass(frozen=True)
class MigrationReport:
    """Summary of a migration run (dry-run or applied)."""

    total_entities: int
    migrated_entities: int
    migrated_books: int
    migrated_chunks: int
    migrated_summaries: int
    review_count: int
    dry_run: bool


def map_entity_id(
    namespace: SourceNamespace, old_id: str, entity_type: str
) -> EntityIdMapping:
    """Map a persisted global entity id ``slug-type`` to its namespaced id.

    Already-namespaced ids are recognized (and left untouched) so re-running the
    migration is a no-op.  A namespaced id from a *different* source, or a
    global id whose suffix does not match the node's stored ``type``, is
    unresolved and routed to the review list rather than guessed.
    """
    try:
        parsed_ns, _slug, parsed_type = SourceNamespace.parse_entity_id(old_id)
    except UnknownNamespaceError:
        parsed_ns = None
        parsed_type = None

    if parsed_ns is not None:
        if parsed_ns == namespace and parsed_type == entity_type:
            return EntityIdMapping(old_id, None, None, entity_type, "already-namespaced")
        return EntityIdMapping(
            old_id,
            None,
            None,
            entity_type,
            "unresolved",
            f"id is already namespaced under {parsed_ns.source_id!r}, "
            f"not {namespace.source_id!r}",
        )

    suffix = f"-{entity_type}"
    if not old_id.endswith(suffix):
        return EntityIdMapping(
            old_id,
            None,
            None,
            entity_type,
            "unresolved",
            f"global id {old_id!r} does not end with '-{entity_type}'",
        )
    slug = old_id[: -len(suffix)]
    if not slug:
        return EntityIdMapping(
            old_id, None, None, entity_type, "unresolved", f"global id {old_id!r} has an empty slug"
        )
    return EntityIdMapping(
        old_id, namespace.entity_id(slug, entity_type), slug, entity_type, "migrate"
    )


def map_book_id(namespace: SourceNamespace, old_id: str) -> BookIdMapping:
    """Map a persisted global book id to the namespaced book id ``corpus:source``."""
    new_id = namespace.book_id()
    if old_id == new_id:
        return BookIdMapping(old_id, None, "already-namespaced")
    try:
        parsed = SourceNamespace.parse_book_id(old_id)
    except UnknownNamespaceError:
        parsed = None
    if parsed is not None:
        return BookIdMapping(
            old_id,
            None,
            "unresolved",
            f"book id {old_id!r} is already namespaced under a different source",
        )
    return BookIdMapping(old_id, new_id, "migrate")


def fold_aliases(old_id: str, aliases: list[str]) -> list[str]:
    """Return ``aliases`` with the old canonical id appended (case-insensitive dedup)."""
    result = list(aliases)
    seen = {alias.casefold() for alias in aliases}
    if old_id.casefold() not in seen:
        result.append(old_id)
    return result


def build_migration_plan(
    namespace: SourceNamespace,
    entities: list[Entity],
    book_ids: list[str],
    chunks: list[tuple[int, str | None]],
    summaries: list[CommunitySummary],
    relationship_entity_ids: set[str] | None = None,
) -> MigrationPlan:
    """Build the full re-identification plan from a graph snapshot.

    Pure logic: no Neo4j access.  Any unresolved or ambiguous case is placed on
    ``review`` and is never silently dropped.
    """
    entity_resolution: dict[str, EntityIdMapping] = {}
    entity_plans: list[EntityMigration] = []
    review: list[ReviewItem] = []

    for entity in entities:
        mapping = map_entity_id(namespace, entity.id, entity.type)
        entity_resolution[entity.id] = mapping
        if mapping.status == "migrate":
            entity_plans.append(
                EntityMigration(
                    old_id=entity.id,
                    new_id=mapping.new_id if mapping.new_id is not None else entity.id,
                    aliases=fold_aliases(entity.id, entity.aliases),
                )
            )
        elif mapping.status == "unresolved":
            review.append(ReviewItem("entity", entity.id, mapping.reason or "unresolved entity id"))

    # Books and chunks share a single target id.  More than one distinct old
    # book id means the graph was indexed under different title slugs/hashes and
    # cannot be merged automatically without a human decision.
    old_book_ids = {bid for bid in book_ids if bid} | {bid for _, bid in chunks if bid is not None}
    book_plans: list[BookMigration] = []
    chunk_plans: list[ChunkMigration] = []
    if len(old_book_ids) > 1:
        for bid in sorted(old_book_ids):
            review.append(
                ReviewItem(
                    "book",
                    bid,
                    "multiple distinct book ids present; cannot merge into a single "
                    "namespaced book id automatically",
                )
            )
    else:
        for old_book_id in sorted(old_book_ids):
            book_mapping = map_book_id(namespace, old_book_id)
            if book_mapping.status == "migrate":
                new_book_id = (
                    book_mapping.new_id if book_mapping.new_id is not None else old_book_id
                )
                if old_book_id in set(book_ids):
                    book_plans.append(BookMigration(old_id=old_book_id, new_id=new_book_id))
                for chunk_index, chunk_book_id in chunks:
                    if chunk_book_id == old_book_id:
                        chunk_plans.append(
                            ChunkMigration(
                                chunk_index=chunk_index,
                                old_book_id=chunk_book_id,
                                new_book_id=new_book_id,
                            )
                        )
            elif book_mapping.status == "unresolved":
                review.append(
                    ReviewItem("book", old_book_id, book_mapping.reason or "unresolved book id")
                )

    # Every entity id referenced by a :RELATED/:MENTIONS edge must be covered by
    # the entity migration; anything else is a dangling reference to review.
    for eid in sorted(relationship_entity_ids or ()):
        if eid not in entity_resolution:
            review.append(
                ReviewItem(
                    "relationship-endpoint",
                    eid,
                    "relationship references an entity id with no matching :Entity node",
                )
            )

    summary_plans: list[SummaryMigration] = []
    summary_by_id = {summary.id: summary for summary in summaries}
    summary_new_ids: dict[str, str] = {}
    summary_new_entity_ids: dict[str, list[str]] = {}
    migrating_summary_ids: list[str] = []

    for summary in summaries:
        new_entity_ids: list[str] = []
        ok = True
        for old_eid in summary.entity_ids:
            resolution = entity_resolution.get(old_eid)
            if resolution is None:
                ok = False
                review.append(
                    ReviewItem(
                        "summary-entity",
                        old_eid,
                        f"summary {summary.id} references an entity id with no matching "
                        ":Entity node",
                    )
                )
                break
            if resolution.status == "migrate":
                new_entity_ids.append(
                    resolution.new_id if resolution.new_id is not None else old_eid
                )
            elif resolution.status == "already-namespaced":
                new_entity_ids.append(old_eid)
            else:
                ok = False
                review.append(
                    ReviewItem(
                        "summary-entity",
                        old_eid,
                        f"summary {summary.id}: {resolution.reason or 'unresolved entity id'}",
                    )
                )
                break
        if not ok:
            continue

        new_summary_id = _community_summary_id(summary.level, new_entity_ids)
        summary_new_ids[summary.id] = new_summary_id
        summary_new_entity_ids[summary.id] = new_entity_ids
        if new_summary_id != summary.id:
            migrating_summary_ids.append(summary.id)

    for old_id in migrating_summary_ids:
        summary = summary_by_id[old_id]
        if summary.parent_id is None:
            new_parent_id = None
        else:
            new_parent_id = summary_new_ids.get(summary.parent_id)
            if new_parent_id is None:
                review.append(
                    ReviewItem(
                        "summary-parent",
                        old_id,
                        f"parent_id {summary.parent_id!r} references an unknown summary",
                    )
                )
                continue
        summary_plans.append(
            SummaryMigration(
                old_id=old_id,
                new_id=summary_new_ids[old_id],
                entity_ids=summary_new_entity_ids[old_id],
                parent_id=new_parent_id,
            )
        )

    return MigrationPlan(
        entities=entity_plans,
        books=book_plans,
        chunks=chunk_plans,
        summaries=summary_plans,
        review=review,
    )


async def load_entities(driver: Any) -> list[Entity]:
    """Read every :Entity node into domain models."""
    query = """
    MATCH (n:Entity)
    RETURN n.id AS id, n.name AS name, n.type AS type,
           n.description AS description, n.source_page AS source_page,
           n.aliases AS aliases, n.canonical_name AS canonical_name
    ORDER BY n.id
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


async def load_book_ids(driver: Any) -> list[str]:
    """Read the distinct ``:Book.id`` values."""
    query = "MATCH (b:Book) RETURN collect(DISTINCT b.id) AS ids"
    async with driver.session() as session:
        result = await session.run(query)
        record = await result.single()
    if record is None:
        return []
    return [bid for bid in (record["ids"] or []) if bid]


async def load_chunks(driver: Any) -> list[tuple[int, str | None]]:
    """Read every ``:Chunk`` ``(chunk_index, book_id)`` pair."""
    query = """
    MATCH (c:Chunk)
    RETURN c.chunk_index AS chunk_index, c.book_id AS book_id
    ORDER BY c.chunk_index
    """
    chunks: list[tuple[int, str | None]] = []
    async with driver.session() as session:
        result = await session.run(query)
        async for record in result:
            chunks.append((record["chunk_index"], record["book_id"]))
    return chunks


async def load_summaries(driver: Any) -> list[CommunitySummary]:
    """Read every :CommunitySummary node."""
    query = """
    MATCH (c:CommunitySummary)
    RETURN c.id AS id, c.level AS level, c.summary AS summary,
           c.entity_ids AS entity_ids, c.parent_id AS parent_id
    """
    summaries: list[CommunitySummary] = []
    async with driver.session() as session:
        result = await session.run(query)
        async for record in result:
            summaries.append(
                CommunitySummary(
                    id=record["id"],
                    level=record["level"],
                    summary=record["summary"],
                    entity_ids=record["entity_ids"],
                    parent_id=record["parent_id"],
                )
            )
    return summaries


async def load_relationship_scope(driver: Any) -> tuple[set[str], dict[str, int]]:
    """Read relationship endpoints and edge counts for the report/coverage check."""
    endpoints_query = """
    OPTIONAL MATCH (src:Entity)-[:RELATED]->(dst:Entity)
    WITH collect(src.id) + collect(dst.id) AS related_ids
    OPTIONAL MATCH ()-[:MENTIONS]->(e:Entity)
    WITH related_ids + collect(e.id) AS all_ids
    UNWIND all_ids AS id
    RETURN collect(DISTINCT id) AS ids
    """
    counts_query = """
    OPTIONAL MATCH ()-[r:RELATED]->() RETURN 'RELATED' AS type, count(r) AS count
    UNION ALL
    OPTIONAL MATCH ()-[m:MENTIONS]->() RETURN 'MENTIONS' AS type, count(m) AS count
    UNION ALL
    OPTIONAL MATCH ()-[h]->() WHERE type(h) STARTS WITH 'HAS_'
    RETURN type(h) AS type, count(h) AS count
    """
    async with driver.session() as session:
        endpoint_result = await session.run(endpoints_query)
        endpoint_record = await endpoint_result.single()
        count_result = await session.run(counts_query)
        count_records = [record async for record in count_result]

    endpoints: set[str] = set()
    if endpoint_record is not None:
        endpoints.update(endpoint_record["ids"] or [])

    edge_counts: dict[str, int] = {}
    for record in count_records:
        edge_counts[record["type"]] = record["count"]

    return endpoints, edge_counts


async def load_snapshot(driver: Any) -> GraphSnapshot:
    """Load everything the migration needs from Neo4j."""
    entities = await load_entities(driver)
    book_ids = await load_book_ids(driver)
    chunks = await load_chunks(driver)
    summaries = await load_summaries(driver)
    relationship_entity_ids, edge_counts = await load_relationship_scope(driver)
    return GraphSnapshot(
        entities=entities,
        book_ids=book_ids,
        chunks=chunks,
        summaries=summaries,
        relationship_entity_ids=relationship_entity_ids,
        edge_counts=edge_counts,
    )


async def apply_migrations(driver: Any, plan: MigrationPlan) -> None:
    """Write the migration, one atomic transaction per node rewrite.

    Each rewrite mutates an existing node's id property in place, so a failure
    can never leave a partially-migrated node (the id either changed or did not).
    """
    if plan.is_noop:
        return

    async def _apply_one(session: Any, query: str, params: dict[str, Any]) -> None:
        tx = await session.begin_transaction()
        try:
            await tx.run(query, params)
            await tx.commit()
        except Exception:
            await tx.rollback()
            raise

    async with driver.session() as session:
        for entity in plan.entities:
            await _apply_one(
                session,
                _MIGRATE_ENTITY,
                {"old_id": entity.old_id, "new_id": entity.new_id, "aliases": entity.aliases},
            )
        for book in plan.books:
            await _apply_one(
                session, _MIGRATE_BOOK, {"old_id": book.old_id, "new_id": book.new_id}
            )
        for chunk in plan.chunks:
            await _apply_one(
                session,
                _MIGRATE_CHUNK,
                {
                    "chunk_index": chunk.chunk_index,
                    "old_book_id": chunk.old_book_id,
                    "new_book_id": chunk.new_book_id,
                },
            )
        for summary in plan.summaries:
            await _apply_one(
                session,
                _MIGRATE_SUMMARY,
                {
                    "old_id": summary.old_id,
                    "new_id": summary.new_id,
                    "entity_ids": summary.entity_ids,
                    "parent_id": summary.parent_id,
                },
            )


def _render_plan(snapshot: GraphSnapshot, plan: MigrationPlan) -> list[str]:
    """Render the re-identification plan as reviewable lines with counts and samples."""
    lines: list[str] = []
    lines.append(f"Entities: {len(snapshot.entities)} total, {len(plan.entities)} to migrate")
    for entity in plan.entities[:_SAMPLE_LIMIT]:
        lines.append(f"  {entity.old_id}  ->  {entity.new_id}")
    if len(plan.entities) > _SAMPLE_LIMIT:
        lines.append(f"  ... {len(plan.entities) - _SAMPLE_LIMIT} more")

    lines.append(f"Books: {len(plan.books)} to migrate")
    for book in plan.books[:_SAMPLE_LIMIT]:
        lines.append(f"  {book.old_id}  ->  {book.new_id}")

    lines.append(f"Chunks: {len(plan.chunks)} book_id values to migrate")
    lines.append(f"Community summaries: {len(plan.summaries)} to migrate")
    for summary in plan.summaries[:_SAMPLE_LIMIT]:
        lines.append(f"  {summary.old_id}  ->  {summary.new_id}")

    lines.append("Edge coverage (preserved in place, node references):")
    for rel_type, count in sorted(snapshot.edge_counts.items()):
        lines.append(f"  {rel_type}: {count}")

    if plan.review:
        lines.append(f"Review required: {len(plan.review)} case(s)")
        for item in plan.review[:_SAMPLE_LIMIT]:
            lines.append(f"  [{item.kind}] {item.identifier}: {item.reason}")
        if len(plan.review) > _SAMPLE_LIMIT:
            lines.append(f"  ... {len(plan.review) - _SAMPLE_LIMIT} more")
    return lines


def _make_driver(settings: Settings) -> Any:
    """Create an ephemeral bolt driver for the migration run."""
    return AsyncGraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value()),
    )


async def _run_main(
    dry_run: bool, assume_yes: bool, corpus: str, source: str
) -> MigrationReport:
    """Single-entry coroutine so the event loop stays open for cleanup."""
    settings = Settings.model_validate({})
    namespace = CatalogLoader(settings.catalog_path).load().resolve_source(corpus, source)
    driver = _make_driver(settings)
    try:
        snapshot = await load_snapshot(driver)
        plan = build_migration_plan(
            namespace,
            snapshot.entities,
            snapshot.book_ids,
            snapshot.chunks,
            snapshot.summaries,
            snapshot.relationship_entity_ids,
        )
        for line in _render_plan(snapshot, plan):
            click.echo(line)

        if dry_run:
            click.echo("[dry-run] No changes written.")
        else:
            if not assume_yes and not click.confirm(
                "Apply this migration to the graph?", default=False
            ):
                click.echo("Aborted. No changes written.")
            else:
                await apply_migrations(driver, plan)
                click.echo("[done] Migration applied.")
    finally:
        await driver.close()

    return MigrationReport(
        total_entities=len(snapshot.entities),
        migrated_entities=len(plan.entities),
        migrated_books=len(plan.books),
        migrated_chunks=len(plan.chunks),
        migrated_summaries=len(plan.summaries),
        review_count=len(plan.review),
        dry_run=dry_run,
    )


@click.command()
@click.option(
    "--dry-run/--apply",
    "dry_run",
    default=True,
    show_default=True,
    help="Preview the migration plan (default) or write it to Neo4j.",
)
@click.option(
    "--yes",
    is_flag=True,
    help="Skip the interactive confirmation when --apply is given.",
)
@click.option("--corpus", default="knowledge", show_default=True, help="Catalog corpus.")
@click.option(
    "--source",
    default="agentic-architectural-patterns",
    show_default=True,
    help="Catalog source slug.",
)
def main(dry_run: bool, yes: bool, corpus: str, source: str) -> None:
    """Migrate global entity/book ids to catalog namespaced ids."""
    asyncio.run(_run_main(dry_run, yes, corpus, source))


if __name__ == "__main__":
    main()
