"""Manual runner for the indexing pipeline.

Usage: uv run python scripts/run_indexer.py --pdf data/book.pdf
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import click

from book_graph_rag.application.index_book_use_case import IndexBookUseCase
from book_graph_rag.config import Settings
from book_graph_rag.infrastructure.llm_adapter import LLMAdapter
from book_graph_rag.infrastructure.neo4j_command_adapter import Neo4jCommandAdapter
from book_graph_rag.infrastructure.pdf_adapter import PDFAdapter


@click.command()
@click.option(
    "--pdf",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to the PDF to index.",
)
@click.option(
    "--clear",
    is_flag=True,
    help="Clear the existing index and exit.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show current graph counts and the PDF chunk count without writing.",
)
@click.option(
    "--fresh",
    is_flag=True,
    help="Clear the index before running the pipeline.",
)
def main(pdf: Path, clear: bool, dry_run: bool, fresh: bool) -> None:
    """Index a PDF book into the knowledge graph."""
    try:
        settings = Settings()  # type: ignore[call-arg]
    except Exception as exc:  # noqa: BLE003
        click.echo(f"Configuration error: {exc}", err=True)
        sys.exit(1)

    pdf_adapter = PDFAdapter(settings)
    llm_adapter = LLMAdapter(settings)
    neo4j_adapter = Neo4jCommandAdapter(settings)

    async def _show_counts(label: str, extra: str = "") -> None:
        chunks = await neo4j_adapter.count_chunks()
        entities = await neo4j_adapter.count_entities()
        mentions = await neo4j_adapter.count_mentions()
        msg = f"{label}: chunks={chunks} entities={entities} mentions={mentions}"
        if extra:
            msg += f" {extra}"
        click.echo(msg)

    async def _run() -> None:
        if clear:
            await neo4j_adapter.clear_index()
            click.echo("Index cleared.")
            return

        if dry_run:
            pdf_chunks = list(pdf_adapter.extract_chunks(str(pdf)))
            await _show_counts("dry-run", extra=f"pdf_chunks={len(pdf_chunks)}")
            return

        await _show_counts("pre")

        if fresh:
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
        await use_case.execute(str(pdf))

        await _show_counts("post")

    try:
        asyncio.run(_run())
    finally:
        asyncio.run(neo4j_adapter.close())


if __name__ == "__main__":
    main()
