"""MCP server entrypoint for book-graph-rag."""

from __future__ import annotations

import asyncio
import sys

import click

from book_graph_rag.config import Settings
from book_graph_rag.infrastructure.logging.json_query_logger_adapter import (
    JsonFileQueryLoggerAdapter,
)
from book_graph_rag.infrastructure.mcp.mcp_server_adapter import McpServerAdapter
from book_graph_rag.infrastructure.neo4j_query_adapter import Neo4jQueryAdapter


@click.group()
@click.version_option(prog_name="book-graph-rag-mcp")
def mcp_cli() -> None:
    """book-graph-rag-mcp: MCP server for knowledge graph queries."""


async def _run_server(settings: Settings) -> None:
    """Composition root: wire dependencies and run the MCP SSE server."""
    query_adapter: Neo4jQueryAdapter = Neo4jQueryAdapter(settings)
    try:
        query_logger: JsonFileQueryLoggerAdapter = JsonFileQueryLoggerAdapter(settings)
        try:
            server_adapter: McpServerAdapter = McpServerAdapter(query_adapter, query_logger)
            click.echo(f"MCP server starting on port {settings.mcp_port}")
            await server_adapter.run_sse(host="0.0.0.0", port=settings.mcp_port)
        finally:
            await query_logger.close()
    finally:
        await query_adapter.close()


@mcp_cli.command("serve")
def serve() -> None:
    """Start the MCP SSE server."""
    try:
        settings = Settings()  # type: ignore[call-arg]
    except Exception as exc:  # noqa: BLE001
        click.echo(f"Configuration error: {exc}", err=True)
        sys.exit(1)

    try:
        asyncio.run(_run_server(settings))
    except Exception as exc:  # noqa: BLE001
        click.echo(f"MCP server error: {exc}", err=True)
        sys.exit(2)


def main() -> None:
    """Script entrypoint for `book-graph-rag-mcp` console command."""
    mcp_cli()
