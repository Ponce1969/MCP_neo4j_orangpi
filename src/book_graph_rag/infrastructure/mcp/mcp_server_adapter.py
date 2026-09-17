"""FastMCP adapter exposing GraphQueryPort operations as MCP tools."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent
from pydantic import SecretStr

from book_graph_rag.application.global_query_use_case import GlobalQueryUseCase
from book_graph_rag.domain.mcp_security import (
    InvalidScopeError,
    McpSecurityError,
    MissingScopeError,
    QueryFingerprint,
    QueryFingerprintError,
    ScopeContext,
)
from book_graph_rag.domain.models import (
    EntityType,
    QueryLogEntry,
    RelationshipType,
    redact_sensitive,
    redact_sensitive_metadata,
)
from book_graph_rag.domain.tool_tier_registry import tier_for
from book_graph_rag.infrastructure.mcp_resource_budget_adapter import (
    InMemoryResourceBudgetAdapter,
)
from book_graph_rag.ports.graph_query_port import GraphQueryPort
from book_graph_rag.ports.mcp_security_port import ResourceBudgetPort
from book_graph_rag.ports.query_logger_port import QueryLoggerPort
from book_graph_rag.ports.scope_resolver_port import ScopeResolverPort
from book_graph_rag.ports.text2cypher_port import Text2CypherPort


def _tool_content(payload: dict[str, Any]) -> list[TextContent]:
    """Serialize a dict result into MCP text content."""
    return [TextContent(type="text", text=json.dumps(payload, indent=2))]


#: Free-text prompt keys are never persisted or included in the structured query
#: fingerprint; they are routed to the keyed prompt fingerprint instead (R5).
_PROMPT_KEYS: frozenset[str] = frozenset({"query", "question"})

#: Logger name for the development-only raw query channel (R5). This channel is
#: opt-in AND development-only; it never feeds the persisted JSONL schema.
_RAW_QUERY_LOGGER_NAME = "book_graph_rag.mcp.raw_query_log"

#: Non-sensitive scalar configuration knobs that may be persisted as metadata.
_METADATA_SCALAR_KEYS: frozenset[str] = frozenset({
    "limit",
    "depth",
    "cursor",
    "page_size",
    "include_relations",
    "detail_level",
})


def _to_query_metadata(
    params: dict[str, Any],
) -> dict[str, str | int | float | bool | None]:
    """Reduce raw query inputs to non-sensitive metadata (R5).

    Free-text and identifier values are replaced by presence flags; only
    non-sensitive configuration scalars are persisted as values.
    """
    metadata: dict[str, str | int | float | bool | None] = {
        "param_count": len(params),
    }
    for key, value in params.items():
        if key in _METADATA_SCALAR_KEYS:
            metadata[key] = value
        else:
            metadata[f"{key}_set"] = value is not None
    return metadata


def _error_code_for(exc: BaseException) -> str:
    """Return a stable, non-secret error code for a raised exception."""
    if isinstance(exc, McpSecurityError):
        return exc.error_code
    return type(exc).__name__


class McpServerAdapter:
    """Wraps a ``GraphQueryPort`` and a ``Text2CypherPort`` as MCP tools.

    The adapter calls the ports directly (bypassing the application use case)
    because each MCP tool has a distinct input/output shape that does not fit
    the unified ``GraphQueryUnion`` dispatch.

    Scope-aware structured tools accept an optional ``source_id`` plus filter
    lists. When a ``source_id`` is supplied, the configured ``ScopeResolverPort``
    validates it and produces a frozen ``ScopeContext`` that is bound as
    parameters to the underlying graph queries. ``query_cypher`` is disabled by
    default and must be explicitly enabled; when disabled it returns a typed
    policy error without contacting the graph.
    """

    def __init__(
        self,
        graph_query_port: GraphQueryPort,
        query_logger: QueryLoggerPort,
        text2cypher_port: Text2CypherPort,
        global_query_use_case: GlobalQueryUseCase | None = None,
        scope_resolver: ScopeResolverPort | None = None,
        enable_query_cypher: bool = False,
        require_scope: bool = True,
        budget_port: ResourceBudgetPort | None = None,
        hmac_key_id: str = "mcp-log-v1",
        hmac_key: SecretStr | None = None,
        app_env: Literal["development", "production", "test"] = "production",
        raw_logging_enabled: bool = False,
        dev_raw_logger: logging.Logger | None = None,
    ) -> None:
        self._graph_query_port = graph_query_port
        self._query_logger = query_logger
        self._text2cypher_port = text2cypher_port
        self._global_query_use_case = global_query_use_case
        self._scope_resolver = scope_resolver
        self._enable_query_cypher = enable_query_cypher
        self._require_scope = require_scope
        # Fail-closed budget wiring: a misconfigured adapter must never disable
        # per-tier concurrency/rate limits, so the default is always-on.
        self._budget_port: ResourceBudgetPort = (
            budget_port if budget_port is not None else InMemoryResourceBudgetAdapter()
        )
        # Keyed log-fingerprint wiring (R5). A missing/empty key raises a typed
        # QueryFingerprintError at fingerprint time, never plaintext logging.
        self._hmac_key_id = hmac_key_id
        self._hmac_key = hmac_key
        # Development-only raw logging (R5). Raw text may only flow to the
        # separate dev channel when the flag AND app_env == "development" hold;
        # emission is refused (fail-closed) otherwise at _emit_raw_log time.
        self._app_env = app_env
        self._raw_logging_enabled = raw_logging_enabled
        self._dev_raw_logger: logging.Logger = (
            dev_raw_logger
            if dev_raw_logger is not None
            else logging.getLogger(_RAW_QUERY_LOGGER_NAME)
        )

    def _now(self) -> datetime:
        """Return the current UTC time (extracted for testability)."""
        return datetime.now(tz=UTC)

    def _resolve_scope(
        self,
        source_id: str | None,
        book_ids: list[str] | None,
        entity_types: list[str] | None,
        relationship_types: list[str] | None,
        *,
        tool_name: str | None = None,
    ) -> ScopeContext | None:
        """Resolve a validated ``ScopeContext``, failing closed without scope.

        When ``require_scope`` is enabled (the default) a missing ``source_id`` is
        rejected with ``MissingScopeError`` before any budget is acquired or any
        port is contacted. Legacy callers opt out explicitly via
        ``require_scope=False``.
        """
        if source_id is None:
            if self._require_scope:
                raise MissingScopeError(
                    f"scope source_id is required for tool {tool_name!r}"
                )
            return None
        if self._scope_resolver is None:
            raise InvalidScopeError(
                "Scope source_id provided but no ScopeResolverPort is configured",
                scope_id=source_id,
            )
        return self._scope_resolver.resolve(
            source_id,
            book_ids=tuple(book_ids or ()),
            entity_types=tuple(entity_types or ()),
            relationship_types=tuple(relationship_types or ()),
        )

    def _make_fingerprint(self, payload: dict[str, Any]) -> QueryFingerprint:
        """Compute a keyed query/prompt fingerprint, failing closed without a key."""
        key = self._hmac_key
        if key is None or not key.get_secret_value():
            raise QueryFingerprintError(
                "mcp_hmac_key is empty or missing; refusing to log without fingerprinting"
            )
        return QueryFingerprint.from_canonical(
            self._hmac_key_id, key.get_secret_value(), payload
        )

    def _emit_raw_log(
        self,
        *,
        tool_name: str,
        query_type: str,
        query_params: dict[str, Any],
        prompt: str | None = None,
        error_code: str | None = None,
    ) -> None:
        """Emit raw query text to the development-only raw channel (R5).

        Raw logging is opt-in (``raw_logging_enabled``) AND development-only
        (``app_env == "development"``). When the flag is set under any other
        environment this refuses (fail-closed) with a typed
        ``QueryFingerprintError``, mirroring the fingerprint fail-closed path, so
        raw text can never flow into a non-development log. The raw text never
        enters the persisted ``QueryLogEntry`` JSONL schema.
        """
        if not self._raw_logging_enabled:
            return
        if self._app_env != "development":
            raise QueryFingerprintError(
                "raw query logging is development-only; refusing to emit raw "
                f"query text under app_env={self._app_env!r}"
            )
        self._dev_raw_logger.info(
            json.dumps(
                {
                    "tool": tool_name,
                    "query_type": query_type,
                    "params": query_params,
                    "prompt": prompt,
                    "error_code": error_code,
                },
                default=str,
            )
        )

    async def _log(
        self,
        *,
        tool_name: str,
        query_type: str,
        query_params: dict[str, Any],
        result_count: int,
        entity_not_found: bool,
        duration_ms: float,
        error_code: str | None = None,
        prompt: str | None = None,
    ) -> None:
        """Build and persist a metadata-only ``QueryLogEntry`` (R5)."""
        # Raw query text may only flow to the development-only channel; it is
        # refused (fail-closed) under any other app_env. This runs before the
        # metadata entry is built so a misconfigured adapter persists nothing.
        self._emit_raw_log(
            tool_name=tool_name,
            query_type=query_type,
            query_params=query_params,
            prompt=prompt,
            error_code=error_code,
        )
        fingerprint_inputs = {
            key: value
            for key, value in query_params.items()
            if key not in _PROMPT_KEYS
        }
        entry = QueryLogEntry(
            timestamp=self._now(),
            tool_name=tool_name,
            query_type=query_type,
            query_metadata=redact_sensitive_metadata(_to_query_metadata(query_params)),
            result_count=result_count,
            zero_results=result_count == 0,
            entity_not_found=entity_not_found,
            duration_ms=duration_ms,
            error_code=redact_sensitive(error_code) if error_code is not None else None,
            query_fingerprint=self._make_fingerprint(
                {"tool": tool_name, "query_type": query_type, "inputs": fingerprint_inputs}
            ),
            prompt_fingerprint=(
                self._make_fingerprint({"tool": tool_name, "prompt": prompt})
                if prompt is not None
                else None
            ),
        )
        await self._query_logger.log_query(entry)

    async def find_entity(
        self,
        name: str,
        entity_type: EntityType | None = None,
        *,
        source_id: str | None = None,
        book_ids: list[str] | None = None,
        entity_types: list[str] | None = None,
        relationship_types: list[str] | None = None,
    ) -> dict[str, Any]:
        """Find entities by name and optional type."""
        scope = self._resolve_scope(
            source_id,
            book_ids,
            entity_types,
            relationship_types,
            tool_name="find_entity",
        )
        params: dict[str, Any] = {
            "name": name,
            "entity_type": entity_type,
        }
        if source_id is not None:
            params["source_id"] = source_id
        start = self._now()
        try:
            async with self._budget_port.budget(
                tier_for("find_entity"), key="find_entity"
            ):
                entities = await self._graph_query_port.find_entity(
                    name, entity_type, scope=scope
                )
        except Exception as exc:
            duration_ms = (self._now() - start).total_seconds() * 1000
            await self._log(
                tool_name="find_entity",
                query_type="entity",
                query_params=params,
                result_count=0,
                entity_not_found=True,
                duration_ms=duration_ms,
                error_code=_error_code_for(exc),
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
        *,
        scope_source_id: str | None = None,
        book_ids: list[str] | None = None,
        entity_types: list[str] | None = None,
        relationship_types: list[str] | None = None,
    ) -> dict[str, Any]:
        """Traverse outgoing relationships up to ``depth`` levels (clamped 0-3)."""
        scope = self._resolve_scope(
            scope_source_id,
            book_ids,
            entity_types,
            relationship_types,
            tool_name="traverse_relationships",
        )
        clamped_depth = max(0, min(depth, 3))
        params: dict[str, Any] = {
            "source_id": source_id,
            "rel_type": rel_type,
            "depth": clamped_depth,
        }
        if scope_source_id is not None:
            params["scope_source_id"] = scope_source_id
        start = self._now()
        try:
            async with self._budget_port.budget(
                tier_for("traverse_relationships"), key="traverse_relationships"
            ):
                entities, relationships = await self._graph_query_port.traverse_relationships(
                    source_id, rel_type, clamped_depth, scope=scope
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
                error_code=_error_code_for(exc),
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

    async def search_chunks(
        self,
        query: str,
        limit: int = 10,
        *,
        source_id: str | None = None,
        book_ids: list[str] | None = None,
        entity_types: list[str] | None = None,
        relationship_types: list[str] | None = None,
    ) -> dict[str, Any]:
        """Full-text search over chunk nodes."""
        scope = self._resolve_scope(
            source_id,
            book_ids,
            entity_types,
            relationship_types,
            tool_name="search_chunks",
        )
        params: dict[str, Any] = {"query": query, "limit": limit}
        if source_id is not None:
            params["source_id"] = source_id
        start = self._now()
        try:
            async with self._budget_port.budget(
                tier_for("search_chunks"), key="search_chunks"
            ):
                chunks = await self._graph_query_port.search_chunks(
                    query, limit, scope=scope
                )
        except Exception as exc:
            duration_ms = (self._now() - start).total_seconds() * 1000
            await self._log(
                tool_name="search_chunks",
                query_type="similarity",
                query_params=params,
                result_count=0,
                entity_not_found=False,
                duration_ms=duration_ms,
                error_code=_error_code_for(exc),
                prompt=query,
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
            prompt=query,
        )
        return {"chunks": chunks}

    async def list_entities(
        self,
        cursor: int = 0,
        page_size: int = 50,
        *,
        source_id: str | None = None,
        book_ids: list[str] | None = None,
        entity_types: list[str] | None = None,
        relationship_types: list[str] | None = None,
    ) -> dict[str, Any]:
        """Cursor-based pagination over entities."""
        scope = self._resolve_scope(
            source_id,
            book_ids,
            entity_types,
            relationship_types,
            tool_name="list_entities",
        )
        params: dict[str, Any] = {"cursor": cursor, "page_size": page_size}
        if source_id is not None:
            params["source_id"] = source_id
        start = self._now()
        try:
            async with self._budget_port.budget(
                tier_for("list_entities"), key="list_entities"
            ):
                entities, next_cursor = await self._graph_query_port.list_entities(
                    cursor, page_size, scope=scope
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
                error_code=_error_code_for(exc),
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

    async def count_entities(
        self,
        entity_type: str | None = None,
        *,
        source_id: str | None = None,
        book_ids: list[str] | None = None,
        entity_types: list[str] | None = None,
        relationship_types: list[str] | None = None,
    ) -> dict[str, Any]:
        """Return the number of entities, optionally filtered by type."""
        scope = self._resolve_scope(
            source_id,
            book_ids,
            entity_types,
            relationship_types,
            tool_name="count_entities",
        )
        params: dict[str, Any] = {"entity_type": entity_type}
        if source_id is not None:
            params["source_id"] = source_id
        start = self._now()
        try:
            async with self._budget_port.budget(
                tier_for("count_entities"), key="count_entities"
            ):
                count = await self._graph_query_port.count_entities(
                    entity_type, scope=scope
                )
        except Exception as exc:
            duration_ms = (self._now() - start).total_seconds() * 1000
            await self._log(
                tool_name="count_entities",
                query_type="count",
                query_params=params,
                result_count=0,
                entity_not_found=False,
                duration_ms=duration_ms,
                error_code=_error_code_for(exc),
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
        self,
        query: str,
        limit: int = 10,
        include_relations: bool = True,
        *,
        source_id: str | None = None,
        book_ids: list[str] | None = None,
        entity_types: list[str] | None = None,
        relationship_types: list[str] | None = None,
    ) -> dict[str, Any]:
        """Unified RAG search: chunks + entity + optional relationships."""
        scope = self._resolve_scope(
            source_id,
            book_ids,
            entity_types,
            relationship_types,
            tool_name="search_rag",
        )
        params: dict[str, Any] = {
            "query": query,
            "limit": limit,
            "include_relations": include_relations,
        }
        if source_id is not None:
            params["source_id"] = source_id
        start = self._now()

        errors: list[str] = []
        chunks: list[dict[str, Any]] = []
        entities: list[dict[str, Any]] = []
        entity_not_found = False
        relationships: list[dict[str, Any]] = []

        try:
            async with self._budget_port.budget(
                tier_for("search_rag"), key="search_rag"
            ):
                chunk_task = self._graph_query_port.search_chunks(
                    query, limit, scope=scope
                )
                entity_task = self._graph_query_port.find_entity(
                    query, None, scope=scope
                )
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
                    entities = [
                        entity.model_dump(mode="json") for entity in entity_result
                    ]
                    entity_not_found = len(entity_result) == 0
                    if entity_result and include_relations:
                        try:
                            _, rels = await self._graph_query_port.traverse_relationships(
                                entity_result[0].entity.id, None, 1, scope=scope
                            )
                            relationships = [
                                rel.model_dump(mode="json") for rel in rels
                            ]
                        except Exception as exc:  # pragma: no cover - defensive only
                            errors.append(str(exc))
        except Exception as exc:
            duration_ms = (self._now() - start).total_seconds() * 1000
            await self._log(
                tool_name="search_rag",
                query_type="rag",
                query_params=params,
                result_count=0,
                entity_not_found=False,
                duration_ms=duration_ms,
                error_code=_error_code_for(exc),
                prompt=query,
            )
            raise

        total_results = len(entities) + len(relationships) + len(chunks)
        duration_ms = (self._now() - start).total_seconds() * 1000
        await self._log(
            tool_name="search_rag",
            query_type="rag",
            query_params=params,
            result_count=total_results,
            entity_not_found=entity_not_found,
            duration_ms=duration_ms,
            prompt=query,
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

    async def query_cypher(self, question: str) -> dict[str, Any]:
        """Generate and execute a Cypher query from a natural-language question.

        This high-risk tool is disabled by default. When disabled it returns a
        typed policy error without contacting the graph or the LLM.
        """
        params = {"question": question}
        start = self._now()
        if not self._enable_query_cypher:
            duration_ms = (self._now() - start).total_seconds() * 1000
            error = "query_cypher is disabled by default"
            await self._log(
                tool_name="query_cypher",
                query_type="text2cypher",
                query_params=params,
                result_count=0,
                entity_not_found=False,
                duration_ms=duration_ms,
                error_code="policy_violation",
                prompt=question,
            )
            return {
                "question": question,
                "error": error,
                "error_code": "policy_violation",
                "cypher": None,
                "rows": [],
                "schema_source": None,
                "retries": 0,
            }

        try:
            async with self._budget_port.budget(
                tier_for("query_cypher"), key="query_cypher"
            ):
                result = await self._text2cypher_port.generate_and_run(question)
        except Exception as exc:
            duration_ms = (self._now() - start).total_seconds() * 1000
            await self._log(
                tool_name="query_cypher",
                query_type="text2cypher",
                query_params=params,
                result_count=0,
                entity_not_found=False,
                duration_ms=duration_ms,
                error_code=_error_code_for(exc),
                prompt=question,
            )
            raise

        duration_ms = (self._now() - start).total_seconds() * 1000
        await self._log(
            tool_name="query_cypher",
            query_type="text2cypher",
            query_params=params,
            result_count=len(result.rows),
            entity_not_found=False,
            duration_ms=duration_ms,
            prompt=question,
        )
        return {
            "question": result.question,
            "cypher": result.cypher,
            "rows": result.rows,
            "schema_source": result.schema_source,
            "retries": result.retries,
        }

    async def ask_global(
        self,
        question: str,
        detail_level: int = 1,
        *,
        source_id: str | None = None,
        book_ids: list[str] | None = None,
        entity_types: list[str] | None = None,
        relationship_types: list[str] | None = None,
    ) -> dict[str, Any]:
        """Answer a global question using community-summary map-reduce.

        ``detail_level`` must be in ``[0, 3]``; it is validated before any
        expensive LLM or Neo4j calls are made. Scope parameters are accepted for
        interface consistency but are not yet wired into the community read
        path.
        """
        if not 0 <= detail_level <= 3:
            raise ValueError(
                f"detail_level must be between 0 and 3, got {detail_level}"
            )

        # Enforce the fail-closed scope boundary before any LLM-mediated call.
        # The community read path is not scope-aware in this slice, but the
        # request must still carry a validated scope.
        self._resolve_scope(
            source_id,
            book_ids,
            entity_types,
            relationship_types,
            tool_name="ask_global",
        )

        if self._global_query_use_case is None:
            raise RuntimeError("GlobalQueryUseCase is not configured")

        start = self._now()
        try:
            async with self._budget_port.budget(
                tier_for("ask_global"), key="ask_global"
            ):
                return await self._global_query_use_case.ask(question, detail_level)
        except Exception as exc:
            duration_ms = (self._now() - start).total_seconds() * 1000
            await self._log(
                tool_name="ask_global",
                query_type="global",
                query_params={"question": question, "detail_level": detail_level},
                result_count=0,
                entity_not_found=False,
                duration_ms=duration_ms,
                error_code=_error_code_for(exc),
                prompt=question,
            )
            raise

    def create_server(self, host: str = "0.0.0.0", port: int = 8003) -> FastMCP:
        """Return a configured FastMCP instance with the 8 tools registered."""
        mcp = FastMCP("book-graph-rag", host=host, port=port)

        @mcp.tool()
        async def find_entity(
            name: str,
            entity_type: EntityType | None = None,
            source_id: str | None = None,
            book_ids: list[str] | None = None,
            entity_types: list[str] | None = None,
            relationship_types: list[str] | None = None,
        ) -> dict[str, Any]:
            return await self.find_entity(
                name,
                entity_type,
                source_id=source_id,
                book_ids=book_ids,
                entity_types=entity_types,
                relationship_types=relationship_types,
            )

        @mcp.tool()
        async def traverse_relationships(
            source_id: str,
            rel_type: RelationshipType | None = None,
            depth: int = 1,
            scope_source_id: str | None = None,
            book_ids: list[str] | None = None,
            entity_types: list[str] | None = None,
            relationship_types: list[str] | None = None,
        ) -> dict[str, Any]:
            return await self.traverse_relationships(
                source_id,
                rel_type,
                depth,
                scope_source_id=scope_source_id,
                book_ids=book_ids,
                entity_types=entity_types,
                relationship_types=relationship_types,
            )

        @mcp.tool()
        async def search_chunks(
            query: str,
            limit: int = 10,
            source_id: str | None = None,
            book_ids: list[str] | None = None,
            entity_types: list[str] | None = None,
            relationship_types: list[str] | None = None,
        ) -> dict[str, Any]:
            return await self.search_chunks(
                query,
                limit,
                source_id=source_id,
                book_ids=book_ids,
                entity_types=entity_types,
                relationship_types=relationship_types,
            )

        @mcp.tool()
        async def list_entities(
            cursor: int = 0,
            page_size: int = 50,
            source_id: str | None = None,
            book_ids: list[str] | None = None,
            entity_types: list[str] | None = None,
            relationship_types: list[str] | None = None,
        ) -> dict[str, Any]:
            return await self.list_entities(
                cursor,
                page_size,
                source_id=source_id,
                book_ids=book_ids,
                entity_types=entity_types,
                relationship_types=relationship_types,
            )

        @mcp.tool()
        async def count_entities(
            entity_type: str | None = None,
            source_id: str | None = None,
            book_ids: list[str] | None = None,
            entity_types: list[str] | None = None,
            relationship_types: list[str] | None = None,
        ) -> dict[str, Any]:
            return await self.count_entities(
                entity_type,
                source_id=source_id,
                book_ids=book_ids,
                entity_types=entity_types,
                relationship_types=relationship_types,
            )

        @mcp.tool()
        async def search_rag(
            query: str,
            limit: int = 10,
            include_relations: bool = True,
            source_id: str | None = None,
            book_ids: list[str] | None = None,
            entity_types: list[str] | None = None,
            relationship_types: list[str] | None = None,
        ) -> dict[str, Any]:
            return await self.search_rag(
                query,
                limit,
                include_relations,
                source_id=source_id,
                book_ids=book_ids,
                entity_types=entity_types,
                relationship_types=relationship_types,
            )

        @mcp.tool()
        async def query_cypher(question: str) -> dict[str, Any]:
            return await self.query_cypher(question)

        @mcp.tool()
        async def ask_global(
            question: str,
            detail_level: int = 1,
            source_id: str | None = None,
            book_ids: list[str] | None = None,
            entity_types: list[str] | None = None,
            relationship_types: list[str] | None = None,
        ) -> list[TextContent]:
            return _tool_content(
                await self.ask_global(
                    question,
                    detail_level,
                    source_id=source_id,
                    book_ids=book_ids,
                    entity_types=entity_types,
                    relationship_types=relationship_types,
                )
            )

        return mcp

    async def run_sse(self, host: str = "0.0.0.0", port: int = 8003) -> None:
        """Start the SSE server on the configured host and port."""
        server = self.create_server(host=host, port=port)
        await server.run_sse_async()
