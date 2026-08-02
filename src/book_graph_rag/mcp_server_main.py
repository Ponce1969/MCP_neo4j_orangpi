"""MCP server entrypoint for book-graph-rag."""

from __future__ import annotations

import asyncio
import sys

import click

from book_graph_rag.application.global_query_use_case import GlobalQueryUseCase
from book_graph_rag.config import Settings
from book_graph_rag.infrastructure.community_adapter import Neo4jCommunityAdapter
from book_graph_rag.infrastructure.llm_adapter import LLMAdapter
from book_graph_rag.infrastructure.logging.json_query_logger_adapter import (
    JsonFileQueryLoggerAdapter,
)
from book_graph_rag.infrastructure.mcp.mcp_server_adapter import McpServerAdapter
from book_graph_rag.infrastructure.neo4j_query_adapter import Neo4jQueryAdapter
from book_graph_rag.infrastructure.text2cypher_adapter import Text2CypherAdapter


@click.group()
@click.version_option(prog_name="book-graph-rag-mcp")
def mcp_cli() -> None:
    """book-graph-rag-mcp: MCP server for knowledge graph queries."""


async def _run_server(settings: Settings) -> None:
    """Composition root: wire dependencies and run the MCP SSE server."""
    query_adapter: Neo4jQueryAdapter = Neo4jQueryAdapter(settings)
    try:
        community_adapter: Neo4jCommunityAdapter = Neo4jCommunityAdapter(settings)
        try:
            query_logger: JsonFileQueryLoggerAdapter = JsonFileQueryLoggerAdapter(settings)
            try:
                llm_adapter: LLMAdapter = LLMAdapter(settings)
                text2cypher_adapter: Text2CypherAdapter = Text2CypherAdapter(
                    query_adapter, llm_adapter, settings
                )
                global_query_use_case = GlobalQueryUseCase(
                    read_port=community_adapter,
                    llm_port=llm_adapter,
                    max_concurrency=settings.summary_max_concurrency,
                )
                server_adapter: McpServerAdapter = McpServerAdapter(
                    query_adapter,
                    query_logger,
                    text2cypher_adapter,
                    global_query_use_case=global_query_use_case,
                )
                click.echo(f"MCP server starting on port {settings.mcp_port}")
                await server_adapter.run_sse(host="0.0.0.0", port=settings.mcp_port)
            finally:
                await query_logger.close()
        finally:
            await community_adapter.close()
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
