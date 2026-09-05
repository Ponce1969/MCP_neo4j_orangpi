"""Thin CLI shim for the checkpoint backfill use case.

Usage::

    uv run python scripts/backfill_checkpoints.py \
        --source-id corpus:source \
        --pdf data/book.pdf \
        --dry-run

    uv run python scripts/backfill_checkpoints.py \
        --source-id corpus:source \
        --pdf data/book.pdf \
        --apply \
        --approval approval.txt
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from uuid import uuid4

import click

from book_graph_rag.application.backfill_checkpoints_use_case import (
    BackfillCheckpointsUseCase,
)
from book_graph_rag.config import Settings
from book_graph_rag.infrastructure.neo4j_checkpoint_adapter import Neo4jCheckpointAdapter
from book_graph_rag.infrastructure.neo4j_command_adapter import Neo4jCommandAdapter
from book_graph_rag.infrastructure.version_dimensions import compute_version_dimensions


@click.command()
@click.option(
    "--source-id",
    required=True,
    help="Source id (corpus:source) whose checkpoints will be backfilled.",
)
@click.option(
    "--pdf",
    "pdf_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to the PDF whose bytes define the source_version dimension.",
)
@click.option(
    "--dry-run/--apply",
    default=True,
    show_default=True,
    help="Dry-run reports candidates; --apply writes checkpoints.",
)
@click.option(
    "--approval",
    "approval_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Approval file required when --apply is used.",
)
def main(
    source_id: str,
    pdf_path: Path,
    dry_run: bool,
    approval_path: Path | None,
) -> None:
    """Backfill :Checkpoint rows for a legacy graph."""
    try:
        settings = Settings.model_validate({})
    except Exception as exc:  # noqa: BLE001
        click.echo(f"Configuration error: {exc}", err=True)
        sys.exit(1)

    versions = compute_version_dimensions(pdf_path.read_bytes(), settings)
    checkpoint_adapter = Neo4jCheckpointAdapter(settings)
    graph_db_adapter = Neo4jCommandAdapter(settings)
    use_case = BackfillCheckpointsUseCase(
        checkpoint_port=checkpoint_adapter,
        graph_db_port=graph_db_adapter,
        versions=versions,
        run_id=f"script-{uuid4().hex[:12]}",
    )

    async def _run() -> None:
        try:
            report = await use_case.execute(
                source_id,
                apply=not dry_run,
                approval_path=approval_path,
            )
        finally:
            await use_case.close()
        click.echo(report.model_dump_json(indent=2))

    try:
        asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        click.echo(f"Backfill error: {exc}", err=True)
        sys.exit(2)


if __name__ == "__main__":
    main()
