"""FastMCP adapter exposing GraphQueryPort operations as MCP tools."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from mcp.server.fastmcp import FastMCP

from book_graph_rag.domain.models import (
    EntityType,
    QueryLogEntry,
    RelationshipType,
)
from book_graph_rag.ports.graph_query_port import GraphQueryPort
from book_graph_rag.ports.query_logger_port import QueryLoggerPort


class McpServerAdapter:
    """Wraps a ``GraphQueryPort`` as a set of MCP tools served by FastMCP.

    The adapter calls the port directly (bypassing the application use case)
    because each MCP tool has a distinct input/output shape that does not fit
    the unified ``GraphQueryUnion`` dispatch.
    """

    def __init__(
        self,
        graph_query_port: GraphQueryPort,
        query_logger: QueryLoggerPort,
    ) -> None:
        self._graph_query_port = graph_query_port
        self._query_logger = query_logger

    def _now(self) -> datetime:
        """Return the current UTC time (extracted for testability)."""
        return datetime.now(tz=UTC)

    async def _log(
        self,
        *,
        tool_name: str,
        query_type: str,
        query_params: dict[str, Any],
        result_count: int,
        entity_not_found: bool,
        duration_ms: float,
        error: str | None = None,
    ) -> None:
        """Build and persist a ``QueryLogEntry``."""
        entry = QueryLogEntry(
            timestamp=self._now(),
            tool_name=tool_name,
            query_type=query_type,
            query_params=query_params,
            result_count=result_count,
            zero_results=result_count == 0,
            entity_not_found=entity_not_found,
            duration_ms=duration_ms,
            error=error,
        )
        await self._query_logger.log_query(entry)

    async def find_entity(
        self, name: str, entity_type: EntityType | None = None
    ) -> dict[str, Any]:
        """Find entities by name and optional type."""
        params = {"name": name, "entity_type": entity_type}
        start = self._now()
        try:
            entities = await self._graph_query_port.find_entity(name, entity_type)
        except Exception as exc:
            duration_ms = (self._now() - start).total_seconds() * 1000
            await self._log(
                tool_name="find_entity",
                query_type="entity",
                query_params=params,
                result_count=0,
                entity_not_found=True,
                duration_ms=duration_ms,
                error=str(exc),
            )
            raise

        duration_ms = (self._now() - start).total_seconds() * 1000
        entity_not_found = len(entities) == 0
        await self._log(
            tool_name="find_entity",
            query_type="entity",
            query_params=params,
            result_count=len(entities),
            entity_not_found=entity_not_found,
            duration_ms=duration_ms,
        )
        return {
            "entities": [e.model_dump(mode="json") for e in entities],
            "entity_not_found": entity_not_found,
        }

    async def traverse_relationships(
        self,
        source_id: str,
        rel_type: RelationshipType | None = None,
        depth: int = 1,
    ) -> dict[str, Any]:
        """Traverse outgoing relationships up to ``depth`` levels (clamped 0-3)."""
        clamped_depth = max(0, min(depth, 3))
        params = {"source_id": source_id, "rel_type": rel_type, "depth": clamped_depth}
        start = self._now()
        try:
            entities, relationships = await self._graph_query_port.traverse_relationships(
                source_id, rel_type, clamped_depth
            )
        except Exception as exc:
            duration_ms = (self._now() - start).total_seconds() * 1000
            await self._log(
                tool_name="traverse_relationships",
                query_type="relation",
                query_params=params,
                result_count=0,
                entity_not_found=False,
                duration_ms=duration_ms,
                error=str(exc),
            )
            raise

        duration_ms = (self._now() - start).total_seconds() * 1000
        await self._log(
            tool_name="traverse_relationships",
            query_type="relation",
            query_params=params,
            result_count=len(entities),
            entity_not_found=False,
            duration_ms=duration_ms,
        )
        return {
            "entities": [e.model_dump(mode="json") for e in entities],
            "relationships": [r.model_dump(mode="json") for r in relationships],
        }

    async def search_chunks(self, query: str, limit: int = 10) -> dict[str, Any]:
        """Full-text search over chunk nodes."""
        params = {"query": query, "limit": limit}
        start = self._now()
        try:
            chunks = await self._graph_query_port.search_chunks(query, limit)
        except Exception as exc:
            duration_ms = (self._now() - start).total_seconds() * 1000
            await self._log(
                tool_name="search_chunks",
                query_type="similarity",
                query_params=params,
                result_count=0,
                entity_not_found=False,
                duration_ms=duration_ms,
                error=str(exc),
            )
            raise

        duration_ms = (self._now() - start).total_seconds() * 1000
        await self._log(
            tool_name="search_chunks",
            query_type="similarity",
            query_params=params,
            result_count=len(chunks),
            entity_not_found=False,
            duration_ms=duration_ms,
        )
        return {"chunks": chunks}

    async def list_entities(self, cursor: int = 0, page_size: int = 50) -> dict[str, Any]:
        """Cursor-based pagination over entities."""
        params = {"cursor": cursor, "page_size": page_size}
        start = self._now()
        try:
            entities, next_cursor = await self._graph_query_port.list_entities(
                cursor, page_size
            )
        except Exception as exc:
            duration_ms = (self._now() - start).total_seconds() * 1000
            await self._log(
                tool_name="list_entities",
                query_type="list",
                query_params=params,
                result_count=0,
                entity_not_found=False,
                duration_ms=duration_ms,
                error=str(exc),
            )
            raise

        duration_ms = (self._now() - start).total_seconds() * 1000
        await self._log(
            tool_name="list_entities",
            query_type="list",
            query_params=params,
            result_count=len(entities),
            entity_not_found=False,
            duration_ms=duration_ms,
        )
        return {
            "entities": [e.model_dump(mode="json") for e in entities],
            "next_cursor": next_cursor,
        }

    async def count_entities(self, entity_type: str | None = None) -> dict[str, Any]:
        """Return the number of entities, optionally filtered by type."""
        params = {"entity_type": entity_type}
        start = self._now()
        try:
            count = await self._graph_query_port.count_entities(entity_type)
        except Exception as exc:
            duration_ms = (self._now() - start).total_seconds() * 1000
            await self._log(
                tool_name="count_entities",
                query_type="count",
                query_params=params,
                result_count=0,
                entity_not_found=False,
                duration_ms=duration_ms,
                error=str(exc),
            )
            raise

        duration_ms = (self._now() - start).total_seconds() * 1000
        await self._log(
            tool_name="count_entities",
            query_type="count",
            query_params=params,
            result_count=count,
            entity_not_found=False,
            duration_ms=duration_ms,
        )
        return {"count": count}

    async def search_rag(
        self, query: str, limit: int = 10, include_relations: bool = True
    ) -> dict[str, Any]:
        """Unified RAG search: chunks + entity + optional relationships."""
        params = {"query": query, "limit": limit, "include_relations": include_relations}
        start = self._now()

        errors: list[str] = []
        chunks: list[dict[str, Any]] = []
        entities: list[dict[str, Any]] = []
        entity_not_found = False
        relationships: list[dict[str, Any]] = []

        chunk_task = self._graph_query_port.search_chunks(query, limit)
        entity_task = self._graph_query_port.find_entity(query, None)
        chunk_result, entity_result = await asyncio.gather(
            chunk_task, entity_task, return_exceptions=True
        )

        if isinstance(chunk_result, Exception):
            errors.append(str(chunk_result))
        else:
            chunks = chunk_result

        if isinstance(entity_result, Exception):
            errors.append(str(entity_result))
        else:
            entities = [entity.model_dump(mode="json") for entity in entity_result]
            entity_not_found = len(entity_result) == 0
            if entity_result and include_relations:
                try:
                    _, rels = await self._graph_query_port.traverse_relationships(
                        entity_result[0].entity.id, None, 1
                    )
                    relationships = [rel.model_dump(mode="json") for rel in rels]
                except Exception as exc:  # pragma: no cover - defensive only
                    errors.append(str(exc))

        total_results = len(entities) + len(relationships) + len(chunks)
        duration_ms = (self._now() - start).total_seconds() * 1000
        await self._log(
            tool_name="search_rag",
            query_type="rag",
            query_params=params,
            result_count=total_results,
            entity_not_found=entity_not_found,
            duration_ms=duration_ms,
        )
        return {
            "query": query,
            "entities": entities,
            "relationships": relationships,
            "chunks": chunks,
            "entity_not_found": entity_not_found,
            "total_results": total_results,
            "errors": errors,
        }

    def create_server(self, host: str = "0.0.0.0", port: int = 8003) -> FastMCP:
        """Return a configured FastMCP instance with the 6 tools registered."""
        mcp = FastMCP("book-graph-rag", host=host, port=port)

        @mcp.tool()
        async def find_entity(name: str, entity_type: EntityType | None = None) -> dict[str, Any]:
            return await self.find_entity(name, entity_type)

        @mcp.tool()
        async def traverse_relationships(
            source_id: str,
            rel_type: RelationshipType | None = None,
            depth: int = 1,
        ) -> dict[str, Any]:
            return await self.traverse_relationships(source_id, rel_type, depth)

        @mcp.tool()
        async def search_chunks(query: str, limit: int = 10) -> dict[str, Any]:
            return await self.search_chunks(query, limit)

        @mcp.tool()
        async def list_entities(cursor: int = 0, page_size: int = 50) -> dict[str, Any]:
            return await self.list_entities(cursor, page_size)

        @mcp.tool()
        async def count_entities(entity_type: str | None = None) -> dict[str, Any]:
            return await self.count_entities(entity_type)

        @mcp.tool()
        async def search_rag(
            query: str, limit: int = 10, include_relations: bool = True
        ) -> dict[str, Any]:
            return await self.search_rag(query, limit, include_relations)

        return mcp

    async def run_sse(self, host: str = "0.0.0.0", port: int = 8003) -> None:
        """Start the SSE server on the configured host and port."""
        server = self.create_server(host=host, port=port)
        await server.run_sse_async()
