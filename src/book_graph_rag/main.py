"""CLI entrypoint for book-graph-rag."""

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


@click.group()
@click.version_option(prog_name="book-graph-rag")
def cli() -> None:
    """book-graph-rag: Knowledge-graph RAG indexer for Agentic Architectural Patterns."""


@cli.command("index")
@click.argument("pdf_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def index(pdf_path: Path) -> None:
    """Index a PDF book into the knowledge graph.

    PDF_PATH is the book PDF to process.
    """
    try:
        settings = Settings()  # type: ignore[call-arg]
    except Exception as exc:  # noqa: BLE003
        click.echo(f"Configuration error: {exc}", err=True)
        sys.exit(1)

    pdf_adapter = PDFAdapter(settings)
    llm_adapter = LLMAdapter(settings)
    neo4j_command_adapter = Neo4jCommandAdapter(settings)

    use_case = IndexBookUseCase(
        pdf_port=pdf_adapter,
        llm_port=llm_adapter,
        graph_db_port=neo4j_command_adapter,
        max_concurrency=settings.llm_max_concurrency,
        batch_size=settings.processing_batch_size,
        dead_letter_path=settings.dead_letter_path,
    )

    asyncio.run(use_case.execute(str(pdf_path)))


def main() -> None:
    """Script entrypoint for `book-graph-rag` console command."""
    cli()


if __name__ == "__main__":
    main()
