"""Offline backfill for legacy graphs: alias/canonical defaults + fulltext index.

This script does NOT reconstruct ``(:Chunk)-[:MENTIONS]->(:Entity)`` edges,
because a legacy graph no longer has the chunk→entity mapping extracted from
the original text. Operators must run a full re-index for provenance.

Usage::

    uv run python scripts/backfill_resilience.py all
    uv run python scripts/backfill_resilience.py all --dry-run
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from typing import Any

import click
from neo4j import AsyncDriver, AsyncGraphDatabase

from book_graph_rag.config import Settings

logger = logging.getLogger(__name__)

_LEGACY_ENTITIES_CYPHER = """
MATCH (n:Entity)
RETURN n.id AS id, n.name AS name, n.type AS type
ORDER BY n.id
"""

_ENTITY_ID_COLLISION_GUARD_CYPHER = """
MATCH (existing:Entity {id: $new_id})
WHERE existing.id <> $old_id
RETURN count(existing) > 0 AS collision
"""

_MIGRATE_ENTITY_CYPHER = """
MATCH (old:Entity {id: $old_id})
MERGE (new:Entity {id: $new_id})
SET new += properties(old), new.id = $new_id
WITH old, new
CALL (old, new) {
    OPTIONAL MATCH (c:Chunk)-[m:MENTIONS]->(old)
    FOREACH (_ IN CASE WHEN m IS NULL THEN [] ELSE [1] END |
        MERGE (c)-[new_m:MENTIONS]->(new)
        SET new_m += properties(m)
        DELETE m
    )
}
CALL (old, new) {
    OPTIONAL MATCH (old)-[r:RELATED]->(target)
    FOREACH (_ IN CASE WHEN r IS NULL THEN [] ELSE [1] END |
        MERGE (new)-[new_r:RELATED {type: r.type}]->(target)
        SET new_r += properties(r)
        DELETE r
    )
}
CALL (old, new) {
    OPTIONAL MATCH (source)-[r:RELATED]->(old)
    FOREACH (_ IN CASE WHEN r IS NULL THEN [] ELSE [1] END |
        MERGE (source)-[new_r:RELATED {type: r.type}]->(new)
        SET new_r += properties(r)
        DELETE r
    )
}
DETACH DELETE old
RETURN $old_id AS old_id, $new_id AS new_id
"""

_BACKFILL_ALIASES_CYPHER = """
MATCH (n:Entity)
SET n.aliases = coalesce(n.aliases, []),
    n.canonical_name = coalesce(n.canonical_name, n.name)
RETURN count(n) AS updated
"""

_CREATE_FULLTEXT_INDEX_CYPHER = """
CREATE FULLTEXT INDEX entity_name_aliases_index IF NOT EXISTS
FOR (n:Entity) ON EACH [n.name, n.canonical_name, n.aliases]
"""


def _get_settings() -> Settings:
    """Load settings, failing fast if required Neo4j credentials are missing."""
    try:
        return Settings()  # type: ignore[call-arg]
    except Exception as exc:  # noqa: BLE001
        click.echo(f"Configuration error: {exc}", err=True)
        sys.exit(1)


def _build_driver(settings: Settings) -> AsyncDriver:
    """Build an async Neo4j driver from settings."""
    return AsyncGraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value()),
    )


async def _backfill_entities(session: Any, dry_run: bool) -> tuple[int, int]:
    """Migrate legacy ids and set alias/canonical defaults."""
    if dry_run:
        click.echo("[dry-run] Would migrate legacy entity ids:")
        click.echo(_LEGACY_ENTITIES_CYPHER)
        click.echo("[dry-run] Would execute the entity id collision guard:")
        click.echo(_ENTITY_ID_COLLISION_GUARD_CYPHER)
        click.echo(_MIGRATE_ENTITY_CYPHER)
        click.echo("[dry-run] Would execute entity alias/canonical backfill:")
        click.echo(_BACKFILL_ALIASES_CYPHER)
        return 0, 0

    result = await session.run(_LEGACY_ENTITIES_CYPHER)
    records = await result.data()
    migrated = 0
    collisions = 0
    for record in records:
        name = str(record["name"])
        entity_type = str(record["type"])
        new_id = f"{_slugify(name)}-{entity_type}"
        old_id = str(record["id"])
        if old_id == new_id:
            continue
        guard_result = await session.run(
            _ENTITY_ID_COLLISION_GUARD_CYPHER,
            {"old_id": old_id, "new_id": new_id},
        )
        guard_record = await guard_result.single()
        if guard_record is not None and bool(guard_record["collision"]):
            collisions += 1
            message = f"Entity id collision: {old_id} -> {new_id}; migration skipped"
            logger.warning(message)
            click.echo(f"WARNING: {message}", err=True)
            continue
        await session.run(
            _MIGRATE_ENTITY_CYPHER,
            {"old_id": old_id, "new_id": new_id},
        )
        migrated += 1

    result = await session.run(_BACKFILL_ALIASES_CYPHER)
    record = await result.single()
    updated = int(record["updated"]) if record is not None else 0
    return migrated + updated, collisions


def _slugify(text: str) -> str:
    """Match the adapter's deterministic slugification semantics."""
    normalized = re.sub(r"[^a-z0-9]+", "-", text.lower().strip())
    return normalized.strip("-")


async def _create_fulltext_index(session: Any, dry_run: bool) -> None:
    """Create the entity_name_aliases_index fulltext index if it does not exist."""
    if dry_run:
        click.echo("[dry-run] Would execute fulltext index creation:")
        click.echo(_CREATE_FULLTEXT_INDEX_CYPHER)
        return

    await session.run(_CREATE_FULLTEXT_INDEX_CYPHER)


async def run_backfill(driver: AsyncDriver, dry_run: bool) -> dict[str, Any]:
    """Run the backfill and return a summary.

    The function is async and driver-agnostic so tests can inject a fake
    Neo4j driver without touching the network.
    """
    updated = 0
    collisions = 0
    async with driver.session() as session:
        updated, collisions = await _backfill_entities(session, dry_run)
        await _create_fulltext_index(session, dry_run)

    return {
        "dry_run": dry_run,
        "entities_updated": updated,
        "entity_id_collisions": collisions,
        "mentions_reconstructible": False,
    }


@click.group()
def cli() -> None:
    """Offline backfill for the graphrag-ragas-resilience change."""


@cli.command("all")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print the Cypher that would run without writing to the database.",
)
def all_command(dry_run: bool) -> None:
    """Migrate legacy ids, backfill metadata, and create the fulltext index."""
    settings = _get_settings()
    driver = _build_driver(settings)

    async def _run() -> dict[str, Any]:
        try:
            return await run_backfill(driver, dry_run)
        finally:
            await driver.close()

    summary = asyncio.run(_run())

    click.echo("\nBackfill summary:")
    click.echo(f"  dry_run: {summary['dry_run']}")
    click.echo(f"  entities_updated: {summary['entities_updated']}")
    click.echo(f"  entity_id_collisions: {summary['entity_id_collisions']}")
    click.echo(
        "  :MENTIONS edges are NOT reconstructible from a legacy graph; "
        "run a full re-index for provenance."
    )


if __name__ == "__main__":
    cli()
