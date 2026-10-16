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
from book_graph_rag.infrastructure.neo4j_read_credentials import (
    has_readonly_credentials,
    readonly_settings,
)
from book_graph_rag.infrastructure.neo4j_skill_registry_adapter import Neo4jSkillRegistryAdapter
from book_graph_rag.infrastructure.sentence_transformer_reranker_adapter import (
    reranker_for,
)
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


def _has_access_token(settings: Settings) -> bool:
    """Whether a real bearer token is configured.

    One rule decides what counts, and it lives in ``domain.mcp_security`` so this layer and the
    adapter can never drift apart. Spaces, tabs and invisible non-whitespace characters all
    read as "no token", which is what makes the fail-closed default actually closed.
    """
    from book_graph_rag.domain.mcp_security import effective_token

    if settings.mcp_access_token is None:
        return False
    return bool(effective_token(settings.mcp_access_token.get_secret_value()))


async def _run_server(settings: Settings) -> None:
    """Composition root: wire dependencies and run the MCP SSE server."""
    readonly_configured = has_readonly_credentials(settings)
    if settings.app_env == "production" and not readonly_configured:
        raise RuntimeError(
            "neo4j_readonly_user and neo4j_readonly_password must both be set when "
            "app_env=production: the MCP read paths require a server-side read-only credential"
        )
    if not readonly_configured:
        click.echo(
            "Warning: serving without a read-only Neo4j credential; MCP read paths use the "
            "main Neo4j credential outside production",
            err=True,
        )
    read_settings = readonly_settings(settings)

    # The bearer middleware is installed only when a token is configured, so a production
    # deployment without one would serve every tool to anyone who can reach the port.
    # docs/ops/mcp-service.md section 7 already says every path requires
    # ``Authorization: Bearer <MCP_ACCESS_TOKEN>``; this makes the code agree with it.
    # Scoped to production for the same reason the wildcard-bind gate is: development and
    # test may run token-less on purpose, and they are told so.
    if settings.app_env == "production" and not _has_access_token(settings):
        raise RuntimeError(
            "mcp_access_token must be set when app_env=production: without it the MCP "
            "serves every tool unauthenticated, and docs/ops/mcp-service.md requires "
            "Authorization: Bearer <MCP_ACCESS_TOKEN> on every path"
        )
    if not _has_access_token(settings):
        click.echo(
            "Warning: serving without authentication (no mcp_access_token); acceptable "
            "only on a loopback bind outside production",
            err=True,
        )
    # The same rule governs the fingerprint key, and it is checked here rather than at log
    # time: without it a weak key starts cleanly and then breaks every tool call, after the
    # graph has already been called.
    from book_graph_rag.domain.mcp_security import effective_token

    if settings.app_env == "production" and not effective_token(
        settings.mcp_hmac_key.get_secret_value()
    ):
        raise RuntimeError(
            "mcp_hmac_key must be a real key in production: the MCP refuses to log without "
            "a keyed fingerprint, so a weak key would start cleanly and then break every "
            "tool call after it had already called the graph"
        )
    query_adapter: Neo4jQueryAdapter = Neo4jQueryAdapter(read_settings)
    try:
        community_adapter: Neo4jCommunityAdapter = Neo4jCommunityAdapter(read_settings)
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
                    reranker=reranker_for(settings),
                    rerank_overfetch_n=settings.retrieval_overfetch_n,
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
                            read_settings.neo4j_user,
                            read_settings.neo4j_password.get_secret_value(),
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
