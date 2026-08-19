"""CLI entrypoint for book-graph-rag."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import click

from book_graph_rag.application.audit_graph_use_case import AuditGraphUseCase, build_audit_target
from book_graph_rag.application.index_book_use_case import IndexBookUseCase
from book_graph_rag.application.query_knowledge_graph_use_case import (
    QueryKnowledgeGraphUseCase,
)
from book_graph_rag.config import Settings
from book_graph_rag.domain.models import (
    BatchEntityQuery,
    EntityQuery,
    GraphQueryUnion,
    PathQuery,
    RelationQuery,
)
from book_graph_rag.infrastructure.llm_adapter import LLMAdapter
from book_graph_rag.infrastructure.neo4j_audit_adapter import Neo4jAuditAdapter
from book_graph_rag.infrastructure.neo4j_command_adapter import Neo4jCommandAdapter
from book_graph_rag.infrastructure.neo4j_query_adapter import Neo4jQueryAdapter
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
        settings = Settings.model_validate({})
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


def _build_graph_query(query_type: str, params: dict[str, Any]) -> GraphQueryUnion:
    """Build a concrete GraphQuery from the CLI type and parsed JSON params."""
    match query_type:
        case "entity":
            return EntityQuery(
                name=params["name"],
                entity_type=params.get("entity_type"),
                limit=params.get("limit", 100),
            )
        case "relation":
            return RelationQuery(
                source_id=params["source_id"],
                rel_type=params.get("rel_type"),
                depth=params.get("depth", 1),
            )
        case "path":
            return PathQuery(
                start_id=params["start_id"],
                end_id=params["end_id"],
                max_depth=params.get("max_depth", 3),
            )
        case "batch_entity":
            return BatchEntityQuery(ids=params["ids"])
        case _:
            raise ValueError(f"Unsupported query type: {query_type}")


@cli.command("audit")
@click.option("--target", required=True, type=click.Choice(["bookgraph-neo4j"]))
@click.option("--sample-limit", default=50, type=click.IntRange(min=0), show_default=True)
@click.option("--output", type=click.Path(dir_okay=False, path_type=Path), default=None)
def audit(target: str, sample_limit: int, output: Path | None) -> None:
    """Run the configured project's static, read-only graph audit."""
    try:
        settings = Settings.model_validate({})
        audit_target = build_audit_target(target, settings.neo4j_uri, settings.neo4j_database)
        adapter = Neo4jAuditAdapter(settings)
    except Exception:
        payload = json.dumps(
            {
                "report_schema_version": "1.0",
                "state": "failed",
                "reason": "configuration_or_audit_failure",
            }
        )
        click.echo(payload)
        raise click.exceptions.Exit(13) from None

    async def _run_audit() -> Any:
        try:
            return await AuditGraphUseCase(adapter).execute(audit_target, sample_limit)
        finally:
            if hasattr(adapter, "close"):
                await adapter.close()

    try:
        report = asyncio.run(_run_audit())
    except Exception:
        payload = json.dumps(
            {
                "report_schema_version": "1.0",
                "state": "failed",
                "reason": "configuration_or_audit_failure",
            }
        )
        click.echo(payload)
        raise click.exceptions.Exit(13) from None

    payload = report.model_dump_json(indent=2)
    click.echo(payload)
    if output is not None:
        output.write_text(payload + "\n", encoding="utf-8")
    execution = getattr(report, "execution", None)
    code = (
        execution.exit_code
        if execution
        else {
            "passed": 0,
            "violations": 10,
            "incomplete": 11,
            "unreachable": 12,
            "failed": 13,
        }.get(str(report.state), 13)
    )
    raise click.exceptions.Exit(code)


@cli.command("query")
@click.option(
    "--type",
    "query_type",
    required=True,
    type=click.Choice(["entity", "relation", "path", "batch_entity"]),
)
@click.option("--query", "query_json", required=True, help="JSON query parameters")
def query(query_type: str, query_json: str) -> None:
    """Query the knowledge graph. Output is pure JSON."""
    try:
        settings = Settings.model_validate({})
    except Exception as exc:  # noqa: BLE001
        click.echo(f"Configuration error: {exc}", err=True)
        sys.exit(1)

    try:
        params = json.loads(query_json)
    except json.JSONDecodeError as exc:
        click.echo(f"Invalid JSON: {exc}", err=True)
        sys.exit(2)

    graph_query = _build_graph_query(query_type, params)

    query_adapter = Neo4jQueryAdapter(settings)
    use_case = QueryKnowledgeGraphUseCase(port=query_adapter)

    async def _run() -> Any:
        try:
            return await use_case.execute(graph_query)
        finally:
            await query_adapter.close()

    try:
        result = asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        click.echo(f"Query error: {exc}", err=True)
        sys.exit(3)

    click.echo(result.model_dump_json(indent=2))


def main() -> None:
    """Script entrypoint for `book-graph-rag` console command."""
    cli()


if __name__ == "__main__":
    main()
