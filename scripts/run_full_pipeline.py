"""Full indexing pipeline runner.

Usage:
    uv run python scripts/run_full_pipeline.py data/book.pdf
    uv run python scripts/run_full_pipeline.py --dry-run data/book.pdf
    uv run python scripts/run_full_pipeline.py --fresh --with-communities data/book.pdf
    uv run python scripts/run_full_pipeline.py restore ~/backups_neo4j/bookgraph_backup_<ts>.json
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click
from neo4j import AsyncGraphDatabase

from book_graph_rag.application.index_book_use_case import IndexBookUseCase
from book_graph_rag.config import Settings
from book_graph_rag.infrastructure.llm_adapter import LLMAdapter
from book_graph_rag.infrastructure.neo4j_command_adapter import Neo4jCommandAdapter
from book_graph_rag.infrastructure.pdf_adapter import PDFAdapter
from book_graph_rag.ports.graph_db_port import CountTolerancePolicy, GraphDatabasePort
from scripts import run_communities

_BACKUP_DIR = Path.home() / "backups_neo4j"
_INDEX_NODE_LABELS = (
    "Chunk",
    "Entity",
    "CommunitySummary",
    "Section",
    "Chapter",
    "Book",
)
_INDEX_EDGE_TYPES = (
    "MENTIONS",
    "RELATED",
    "HAS_SUMMARY",
    "CONTAINS",
    "HAS_SECTION",
    "HAS_SUBSECTION",
    "HAS_CHUNK",
)


def _make_driver(settings: Settings) -> Any:
    """Create an ephemeral bolt driver for operator-level backup/restore."""
    return AsyncGraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value()),
    )


async def _backup(driver: Any, path: Path | None = None) -> Path:
    """Dump all index-created nodes and edges to a JSON file."""
    _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_path = path or _BACKUP_DIR / f"bookgraph_backup_{timestamp}.json"

    async with driver.session() as session:
        label_filter = " OR ".join(f"n:{label}" for label in _INDEX_NODE_LABELS)
        node_result = await session.run(
            f"MATCH (n) WHERE {label_filter} "
            "RETURN labels(n) AS labels, properties(n) AS properties"
        )
        nodes: list[dict[str, Any]] = []
        async for record in node_result:
            nodes.append(
                {"labels": record["labels"], "properties": record["properties"]}
            )

        rel_result = await session.run(
            "MATCH (a)-[r]->(b) WHERE type(r) IN $types "
            "RETURN type(r) AS type, properties(r) AS properties, "
            "labels(a) AS start_labels, properties(a) AS start_props, "
            "labels(b) AS end_labels, properties(b) AS end_props",
            {"types": list(_INDEX_EDGE_TYPES)},
        )
        relationships: list[dict[str, Any]] = []
        async for record in rel_result:
            relationships.append(
                {
                    "type": record["type"],
                    "properties": record["properties"],
                    "start_labels": record["start_labels"],
                    "start_props": record["start_props"],
                    "end_labels": record["end_labels"],
                    "end_props": record["end_props"],
                }
            )

    payload = {
        "captured_at_utc": datetime.now(UTC).isoformat(),
        "nodes": nodes,
        "relationships": relationships,
    }
    backup_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return backup_path


def _node_key(label: str, props: dict[str, Any]) -> dict[str, Any]:
    """Return the natural key properties used to MERGE a node idempotently."""
    if label in ("Book", "Entity", "CommunitySummary"):
        return {"id": props["id"]}
    if label == "Chapter":
        return {"number": props["number"], "title": props["title"]}
    if label == "Section":
        return {
            "chapter_number": props["chapter_number"],
            "title": props["title"],
        }
    if label == "Chunk":
        return {
            "chunk_index": props["chunk_index"],
            "book_id": props.get("book_id"),
        }
    raise ValueError(f"Unsupported node label for restore: {label}")


async def _restore(driver: Any, path: Path) -> None:
    """Replay a JSON backup idempotently via MERGE/MATCH…MERGE."""
    data = json.loads(path.read_text(encoding="utf-8"))
    async with driver.session() as session:
        for node in data.get("nodes", []):
            labels = node["labels"]
            if not labels:
                continue
            label = labels[0]
            props = node["properties"]
            key = _node_key(label, props)
            key_clause = ", ".join(f"{k}: ${k}" for k in key)
            set_clause = ", ".join(
                f"n.{k} = ${k}" for k in props if k not in key
            )
            query = f"MERGE (n:{label} {{{key_clause}}})"
            if set_clause:
                query += f" SET {set_clause}"
            parameters: dict[str, Any] = {**key, **props}
            await session.run(query, parameters)

        for rel in data.get("relationships", []):
            rel_type = rel["type"]
            start_label = rel["start_labels"][0]
            start_key = _node_key(start_label, rel["start_props"])
            end_label = rel["end_labels"][0]
            end_key = _node_key(end_label, rel["end_props"])
            start_clause = ", ".join(f"{k}: ${k}" for k in start_key)
            end_clause = ", ".join(f"{k}: ${k}" for k in end_key)
            set_clause = ", ".join(
                f"r.{k} = ${k}" for k in rel.get("properties", {}) if k not in ("type",)
            )
            query = (
                f"MATCH (a:{start_label} {{{start_clause}}}) "
                f"MATCH (b:{end_label} {{{end_clause}}}) "
                f"MERGE (a)-[r:{rel_type}]->(b)"
            )
            if set_clause:
                query += f" SET {set_clause}"
            parameters = {**start_key, **end_key, **rel.get("properties", {})}
            await session.run(query, parameters)


def _run_communities(fresh: bool = False) -> None:
    """Chain the community-summary pipeline with a fresh event loop."""
    asyncio.run(run_communities._run_main(fresh=fresh))


async def _has_community_summaries(driver: Any) -> bool:
    """Return True if any :CommunitySummary nodes exist in the graph."""
    async with driver.session() as session:
        result = await session.run(
            "MATCH (c:CommunitySummary) RETURN count(c) AS count"
        )
        record = await result.single()
        if record is None:
            return False
        return int(record["count"]) > 0


async def _verify_counts(
    port: GraphDatabasePort,
    expected_chunk_count: int,
    pre_entity_count: int,
    dry_run: bool,
) -> tuple[int, int, int]:
    """Compare post-run counts against expectations and emit warnings."""
    if dry_run:
        return 0, 0, 0

    chunks = await port.count_chunks()
    entities = await port.count_entities()
    mentions = await port.count_mentions()

    policy = CountTolerancePolicy()
    tolerance = max(
        expected_chunk_count * policy.chunk_tolerance_pct / 100.0,
        float(policy.chunk_tolerance_abs),
    )
    if abs(chunks - expected_chunk_count) > tolerance:
        click.echo(
            f"WARNING: chunk count drift: expected {expected_chunk_count}, got {chunks}",
            err=True,
        )
    if policy.entity_must_not_decrease and entities < pre_entity_count:
        click.echo(
            f"WARNING: entity count decreased from {pre_entity_count} to {entities}",
            err=True,
        )
    if mentions == 0:
        click.echo("WARNING: zero :MENTIONS edges found", err=True)

    return chunks, entities, mentions


async def _count_dead_letters(path: Path) -> int:
    """Return the number of records already in the dead-letter log."""
    if not path.exists():
        return 0
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return 0
    return len(text.splitlines())


async def _run_pipeline(
    pdf_path: Path,
    dry_run: bool,
    fresh: bool,
    with_communities: bool,
) -> None:
    """Core pipeline: extract, optionally clear/backup, index, verify."""
    try:
        settings = Settings()  # type: ignore[call-arg]
    except Exception as exc:  # noqa: BLE003
        click.echo(f"Configuration error: {exc}", err=True)
        sys.exit(1)

    pdf_adapter = PDFAdapter(settings)
    llm_adapter = LLMAdapter(settings)
    neo4j_adapter = Neo4jCommandAdapter(settings)
    dead_letter_start = await _count_dead_letters(settings.dead_letter_path)

    chunks = list(pdf_adapter.extract_chunks(str(pdf_path)))
    expected_chunk_count = len(chunks)

    if dry_run:
        section_titles = {c.section.title for c in chunks if c.section}
        click.echo(
            f"dry-run: pdf_chunks={expected_chunk_count} sections={len(section_titles)}"
        )
        return

    pre_entity_count = await neo4j_adapter.count_entities()

    if fresh:
        backup_driver = _make_driver(settings)
        try:
            backup_path = await _backup(backup_driver)
            click.echo(f"Backup written to {backup_path}")
        except Exception as exc:  # noqa: BLE003
            click.echo(f"Backup failed: {exc}", err=True)
            sys.exit(2)
        finally:
            await backup_driver.close()
        await neo4j_adapter.clear_index()
        click.echo("Index cleared for fresh run.")

    use_case = IndexBookUseCase(
        pdf_port=pdf_adapter,
        llm_port=llm_adapter,
        graph_db_port=neo4j_adapter,
        max_concurrency=settings.llm_max_concurrency,
        batch_size=settings.processing_batch_size,
        dead_letter_path=settings.dead_letter_path,
    )
    await use_case.execute(str(pdf_path))

    if with_communities:
        _run_communities(fresh=True)
    else:
        backup_driver = _make_driver(settings)
        try:
            if await _has_community_summaries(backup_driver):
                click.echo(
                    "WARNING: community summaries may be stale; "
                    "re-run with --with-communities to regenerate.",
                    err=True,
                )
        finally:
            await backup_driver.close()

    chunks_count, entities_count, mentions_count = await _verify_counts(
        neo4j_adapter,
        expected_chunk_count,
        pre_entity_count,
        dry_run=False,
    )
    dead_lettered = await _count_dead_letters(settings.dead_letter_path)
    dead_letter_delta = dead_lettered - dead_letter_start
    click.echo(
        f"[done] chunks={chunks_count} entities={entities_count} "
        f"mentions={mentions_count} dead_lettered={dead_letter_delta}"
    )


@click.command()
@click.argument(
    "pdf_path",
    required=False,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Preview the planned work without writing to Neo4j.",
)
@click.option(
    "--fresh",
    is_flag=True,
    help="Clear the index before re-indexing (auto-backup first).",
)
@click.option(
    "--with-communities",
    is_flag=True,
    help="Regenerate community summaries after indexing.",
)
@click.option(
    "--restore",
    "restore_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Restore the graph from a JSON backup instead of indexing.",
)
def cli(
    pdf_path: Path | None,
    dry_run: bool,
    fresh: bool,
    with_communities: bool,
    restore_path: Path | None,
) -> None:
    """Run the full indexing pipeline for a PDF or restore from a backup."""
    if restore_path is not None:
        _run_restore(restore_path)
        return
    if pdf_path is None:
        raise click.UsageError("Missing argument 'PDF_PATH'.")
    asyncio.run(_run_pipeline(pdf_path, dry_run, fresh, with_communities))


def _run_restore(backup_path: Path) -> None:
    """Restore the graph from a JSON backup produced by --fresh."""
    try:
        settings = Settings()  # type: ignore[call-arg]
    except Exception as exc:  # noqa: BLE003
        click.echo(f"Configuration error: {exc}", err=True)
        sys.exit(1)

    driver = _make_driver(settings)
    try:
        asyncio.run(_restore(driver, backup_path))
        click.echo(f"Restored from {backup_path}")
    finally:
        asyncio.run(driver.close())


def main() -> None:
    """Script entrypoint for the full pipeline."""
    cli()


if __name__ == "__main__":
    main()
