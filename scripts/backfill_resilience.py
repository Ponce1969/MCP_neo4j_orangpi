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
import sys
from typing import Any

import click
from neo4j import AsyncDriver, AsyncGraphDatabase

from book_graph_rag.config import Settings

logger = logging.getLogger(__name__)

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


async def _backfill_entities(session: Any, dry_run: bool) -> int:
    """Set aliases=[] and canonical_name=name defaults on all Entity nodes."""
    if dry_run:
        click.echo("[dry-run] Would execute entity alias/canonical backfill:")
        click.echo(_BACKFILL_ALIASES_CYPHER)
        return 0

    result = await session.run(_BACKFILL_ALIASES_CYPHER)
    record = await result.single()
    return int(record["updated"]) if record is not None else 0


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
    async with driver.session() as session:
        updated = await _backfill_entities(session, dry_run)
        await _create_fulltext_index(session, dry_run)

    return {
        "dry_run": dry_run,
        "entities_updated": updated,
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
    """Backfill alias/canonical defaults and create the fulltext index."""
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
    click.echo(
        "  :MENTIONS edges are NOT reconstructible from a legacy graph; "
        "run a full re-index for provenance."
    )


if __name__ == "__main__":
    cli()
