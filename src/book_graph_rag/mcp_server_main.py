"""MCP server entrypoint for book-graph-rag."""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import click
from neo4j import AsyncGraphDatabase

from book_graph_rag.application.global_query_use_case import GlobalQueryUseCase
from book_graph_rag.application.quality_gate_use_case import QualityGateUseCase
from book_graph_rag.config import Settings
from book_graph_rag.infrastructure.catalog_loader import CatalogLoader
from book_graph_rag.infrastructure.catalog_scope_resolver import CatalogScopeResolver
from book_graph_rag.infrastructure.community_adapter import Neo4jCommunityAdapter
from book_graph_rag.infrastructure.llm_adapter import LLMAdapter
from book_graph_rag.infrastructure.logging.json_query_logger_adapter import (
    JsonFileQueryLoggerAdapter,
)
from book_graph_rag.infrastructure.mcp.mcp_server_adapter import McpServerAdapter
from book_graph_rag.infrastructure.mcp_resource_budget_adapter import (
    InMemoryResourceBudgetAdapter,
)
from book_graph_rag.infrastructure.neo4j_query_adapter import Neo4jQueryAdapter
from book_graph_rag.infrastructure.neo4j_skill_registry_adapter import Neo4jSkillRegistryAdapter
from book_graph_rag.infrastructure.text2cypher_adapter import Text2CypherAdapter
from book_graph_rag.ports.skill_registry_port import SkillRegistryPort

#: ONE cheap read for the bookgraph://catalog resource: per-source chunk and
#: entity counts, executed as a single round trip at resource-read time. The
#: entity branch applies the same soft-delete predicate the query adapter uses,
#: so the resource can never report a different count than ``count_entities``
#: for the same graph.
_CATALOG_STATS_CYPHER = """
CALL {
  MATCH (c:Chunk)
  WHERE c.book_id IS NOT NULL
  WITH c.book_id AS source_id, count(*) AS chunks
  RETURN source_id, chunks, 0 AS entities
  UNION ALL
  MATCH (e:Entity)
  WHERE e.id IS NOT NULL AND e.id CONTAINS ':'
    AND (e.merged_into IS NULL OR e.merged_into = '')
  WITH split(e.id, ':') AS parts, count(*) AS entities
  WHERE size(parts) >= 2
  RETURN parts[0] + ':' + parts[1] AS source_id, 0 AS chunks, entities
}
RETURN source_id, sum(chunks) AS chunks, sum(entities) AS entities
"""


def _stats_from_rows(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Map ``execute_read`` rows to ``{source_id: {chunks, entities}}``."""
    stats: dict[str, dict[str, int]] = {}
    for row in rows:
        source_id = row.get("source_id")
        if isinstance(source_id, str) and source_id:
            chunks = row.get("chunks")
            entities = row.get("entities")
            stats[source_id] = {
                "chunks": chunks if isinstance(chunks, int) else 0,
                "entities": entities if isinstance(entities, int) else 0,
            }
    return stats


async def _resolve_tool_names(
    settings: Settings, registry: SkillRegistryPort
) -> frozenset[str] | None:
    """Return the tool set the skill quality gate selected, or ``None`` when it is off.

    ``None`` means "every registered tool", which is the historical behaviour. With the
    gate on, an empty registry yields an **empty** set on purpose: the gate is fail-closed,
    so an unseeded registry must expose nothing rather than falling back to everything.
    """
    if not settings.skill_gate_enabled:
        return None
    gate = QualityGateUseCase(registry, safety_cap=settings.skill_safety_high_tier_cap)
    result = await gate.execute(min_quality=settings.skill_min_quality, top_k=settings.skill_top_k)
    tool_names = frozenset(name for skill in result.selected for name in skill.tool_names)
    click.echo(f"Skill gate: {len(result.selected)} skill(s) selected -> {len(tool_names)} tool(s)")
    for line in result.rationale:
        click.echo(f"Skill gate: {line}")
    return tool_names


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
                    top_n=settings.ask_global_top_n,
                    score_batch_size=settings.ask_global_score_batch_size,
                )
                catalog_loader = CatalogLoader(settings.catalog_path)
                scope_resolver = CatalogScopeResolver(catalog_loader)
                catalog = catalog_loader.load()

                async def _catalog_stats() -> dict[str, dict[str, int]]:
                    """Single-read size counts for the catalog resource (as of read)."""
                    rows = await query_adapter.execute_read(_CATALOG_STATS_CYPHER)
                    return _stats_from_rows(rows)

                server_adapter: McpServerAdapter = McpServerAdapter(
                    query_adapter,
                    query_logger,
                    text2cypher_adapter,
                    global_query_use_case=global_query_use_case,
                    scope_resolver=scope_resolver,
                    enable_query_cypher=settings.mcp_enable_query_cypher,
                    require_scope=settings.mcp_require_scope,
                    budget_port=InMemoryResourceBudgetAdapter(),
                    hmac_key_id=settings.mcp_hmac_key_id,
                    hmac_key=settings.mcp_hmac_key,
                    app_env=settings.app_env,
                    raw_logging_enabled=settings.mcp_raw_logging_enabled,
                )
                # Self-description wiring: constructor kwargs are kept stable
                # for test doubles, so the catalog and its single-read size
                # stats are attached before the server starts.
                server_adapter.catalog = catalog
                server_adapter.catalog_stats_reader = _catalog_stats
                if settings.skill_gate_enabled:
                    registry_driver = AsyncGraphDatabase.driver(
                        settings.neo4j_uri,
                        auth=(
                            settings.neo4j_user,
                            settings.neo4j_password.get_secret_value(),
                        ),
                    )
                    try:
                        server_adapter.tool_names = await _resolve_tool_names(
                            settings, Neo4jSkillRegistryAdapter(registry_driver)
                        )
                    finally:
                        await registry_driver.close()
                click.echo(f"MCP server starting on port {settings.mcp_port}")
                await server_adapter.run_sse(
                    host=settings.mcp_bind_host,
                    port=settings.mcp_port,
                    access_token=settings.mcp_access_token,
                )
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
        settings = Settings.model_validate({})
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
