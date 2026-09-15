"""GraphQueryPort implementation using the async Neo4j driver.

All queries are MATCH-only (read-side). Write operations live in
``Neo4jCommandAdapter``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, NoReturn, cast

from neo4j import READ_ACCESS, AsyncGraphDatabase
from neo4j.exceptions import Neo4jError

from book_graph_rag.config import Settings
from book_graph_rag.domain.mcp_security import (
    ResourceExhaustedError,
    ScopeContext,
    UnsupportedQueryError,
)
from book_graph_rag.domain.models import (
    Entity,
    EntityType,
    EntityWithContext,
    GraphPath,
    QueryTimeoutError,
    Relationship,
    RelationshipType,
)
from book_graph_rag.ports.graph_query_port import GraphQueryPort

logger = logging.getLogger(__name__)

# Server-side transaction termination codes mapped to QueryTimeoutError.
_NEO4J_TRANSACTION_TIMEOUT_CODES = frozenset({
    "Neo.ClientError.Transaction.TransactionTimedOut",
    "Neo.ClientError.Transaction.TransactionTimedOutClientConfiguration",
    "Neo.TransientError.Transaction.Terminated",
})

# Write vectors the read-only path must never execute; mapped to a typed
# UnsupportedQueryError so callers can distinguish a rejected write from a
# transient driver failure.
_NEO4J_WRITE_REJECTION_CODES = frozenset({
    "Neo.ClientError.Statement.AccessMode",
    "Neo.ClientError.General.ForbiddenOnReadOnlyDatabase",
})


class Neo4jQueryAdapter(GraphQueryPort):
    """Async Neo4j implementation of ``GraphQueryPort`` for read queries."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # Deserialize the SecretStr once at construction time. The password is
        # passed to the driver and never logged or printed by this adapter.
        self._driver: Any = AsyncGraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value()),
        )

    async def close(self) -> None:
        """Close the underlying Neo4j driver."""
        await self._driver.close()

    def _read_session(self) -> Any:
        """Open a session routed to the read database in READ_ACCESS mode.

        Read-only authority is enforced by the managed ``execute_read``
        transaction; READ_ACCESS routing is a supplemental signal that the
        session must never run write operations.
        """
        return self._driver.session(
            database=self._settings.neo4j_read_database,
            default_access_mode=READ_ACCESS,
        )

    async def _fetch_records(
        self, tx: Any, query: str, params: dict[str, Any], max_rows: int
    ) -> list[Any]:
        """Materialize at most ``max_rows`` records inside a managed read transaction.

        Materialization is bounded so a query that ignores its own ``LIMIT`` can
        never exhaust the read session; exceeding the cap raises a typed
        ``ResourceExhaustedError``.
        """
        result = await tx.run(query, params)
        records: list[Any] = []
        async for record in result:
            if len(records) >= max_rows:
                raise ResourceExhaustedError(
                    f"Read query exceeded the {max_rows}-row materialization cap"
                )
            records.append(record)
        return records

    async def _read_records(
        self, session: Any, query: str, params: dict[str, Any]
    ) -> list[Any]:
        """Run a read query through a managed ``execute_read`` transaction.

        A server-side transaction timeout is applied by attaching the configured
        timeout (seconds) to the unit-of-work callable; the neo4j driver reads
        that attribute when it opens the managed transaction. Server-side
        timeouts and write-vector rejections are mapped to typed domain errors.
        """
        max_rows = self._settings.neo4j_read_max_rows

        async def unit_of_work(tx: Any) -> list[Any]:
            return await self._fetch_records(tx, query, params, max_rows)

        # The neo4j driver reads the ``timeout`` attribute off the unit-of-work
        # callable and applies it as a server-side transaction timeout.
        unit_of_work.timeout = self._settings.neo4j_read_timeout_seconds  # type: ignore[attr-defined]

        try:
            return cast(list[Any], await session.execute_read(unit_of_work))
        except Neo4jError as exc:
            self._raise_typed_driver_error(exc)

    @staticmethod
    def _raise_typed_driver_error(exc: Neo4jError) -> NoReturn:
        """Map a Neo4j driver error to a typed domain error, or re-raise it."""
        code = getattr(exc, "code", None)
        if code in _NEO4J_TRANSACTION_TIMEOUT_CODES:
            raise QueryTimeoutError(f"Neo4j transaction timed out: {code}") from exc
        if code in _NEO4J_WRITE_REJECTION_CODES:
            raise UnsupportedQueryError(
                f"Write rejected on the read-only query path: {code}"
            ) from exc
        raise exc

    async def _run_with_timeout(self, coro: Any, timeout: float = 3.0) -> Any:
        """Wrap a coroutine with a hard timeout and map to a domain error."""
        try:
            return await asyncio.wait_for(coro, timeout=timeout)
        except asyncio.CancelledError as exc:
            raise QueryTimeoutError(
                f"Query cancelled before the {timeout}s budget elapsed"
            ) from exc
        except TimeoutError:
            raise QueryTimeoutError(f"Query exceeded {timeout}s timeout") from None

    def _node_to_entity(self, node: Any) -> EntityWithContext:
        """Map a Neo4j Node to an ``EntityWithContext``."""
        entity = Entity(
            id=node.get("id", ""),
            name=node.get("name", ""),
            type=node.get("type", ""),
            description=node.get("description", ""),
            source_page=node.get("source_page"),
            aliases=node.get("aliases") or [],
            canonical_name=node.get("canonical_name"),
        )
        return EntityWithContext(entity=entity)

    def _record_to_entity_with_context(self, record: Any) -> EntityWithContext:
        """Map a Neo4j result record to an ``EntityWithContext`` with provenance."""
        entity_with_context = self._node_to_entity(record["n"])
        entity_with_context.confidence = record.get("score")
        entity_with_context.source = self._format_source(
            record.get("chunk_index"), record.get("book_id")
        )
        return entity_with_context

    @staticmethod
    def _format_source(chunk_index: Any, book_id: Any) -> str | None:
        """Format chunk provenance for ``EntityWithContext.source``."""
        if chunk_index is None:
            return None
        if book_id is not None:
            return f"book_id={book_id},chunk_index={chunk_index}"
        return f"chunk_index={chunk_index}"
    @staticmethod
    def _build_scope_clause(
        scope: ScopeContext | None,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Return parameterized scope predicates keyed by variable context.

        The returned predicate strings are composed of fixed fragments; all
        user-provided scope values are returned in the parameter map so the
        driver binds them safely. Predicates are grouped by the variable they
        reference (``entity`` -> ``n``, ``path`` -> ``p``, ``chunk`` ->
        ``node``) so each query shape binds only the variables it defines.

        The validated ``scope.source`` namespace is bound as an ``Entity.id``
        prefix predicate (``n.id STARTS WITH $scope_prefix``) and a chunk book
        id predicate (``node.book_id = $scope_source_id``).
        """
        if scope is None:
            return {}, {"entity": "", "path": "", "chunk": ""}
        params: dict[str, Any] = {}
        clauses: dict[str, list[str]] = {"entity": [], "path": [], "chunk": []}
        params["scope_prefix"] = f"{scope.source.source_id}:"
        params["scope_source_id"] = scope.source.source_id
        clauses["entity"].append("n.id STARTS WITH $scope_prefix")
        clauses["chunk"].append("node.book_id = $scope_source_id")
        if scope.entity_types:
            params["scope_entity_types"] = list(scope.entity_types)
            clauses["entity"].append("n.type IN $scope_entity_types")
        if scope.relationship_types:
            params["scope_rel_types"] = list(scope.relationship_types)
            clauses["path"].append(
                "ALL(r IN relationships(p) WHERE r.type IN $scope_rel_types)"
            )
        if scope.book_ids:
            params["scope_book_ids"] = list(scope.book_ids)
            clauses["chunk"].append("node.book_id IN $scope_book_ids")
        return params, {
            name: " AND ".join(parts) for name, parts in clauses.items()
        }

    async def find_entity(
        self,
        name: str,
        entity_type: EntityType | None,
        *,
        scope: ScopeContext | None = None,
    ) -> list[EntityWithContext]:
        """Return entities matching ``name`` and optional ``entity_type``.

        Cascades through exact, case-insensitive, substring and fulltext tiers,
        stopping at the first non-empty tier. The fulltext tier is wrapped in a
        try/except so a missing index degrades gracefully to Tiers 1-3.

        When ``scope`` is provided, entity-type predicates are bound as
        parameters and appended to each tier's WHERE clause.
        """
        limit = 100
        scope_params, scope_clauses = self._build_scope_clause(scope)
        params: dict[str, Any] = {
            "name": name,
            "entity_type": entity_type,
            "limit": limit,
            **scope_params,
        }

        base_predicates = [
            "($entity_type IS NULL OR n.type = $entity_type)",
        ]
        entity_clause = scope_clauses["entity"]
        if entity_clause:
            base_predicates.append(entity_clause)
        where_fragment = " AND ".join(base_predicates)

        tier_queries: list[tuple[float, str]] = [
            (
                1.0,
                f"""
                MATCH (n:Entity {{name: $name}})
                WHERE {where_fragment}
                OPTIONAL MATCH (c:Chunk)-[:MENTIONS]->(n)
                RETURN n, 1.0 AS score, c.chunk_index AS chunk_index, c.book_id AS book_id
                LIMIT $limit
                """,
            ),
            (
                0.8,
                f"""
                MATCH (n:Entity)
                WHERE toLower(n.name) = toLower($name)
                  AND {where_fragment}
                OPTIONAL MATCH (c:Chunk)-[:MENTIONS]->(n)
                RETURN n, 0.8 AS score, c.chunk_index AS chunk_index, c.book_id AS book_id
                LIMIT $limit
                """,
            ),
            (
                0.6,
                f"""
                MATCH (n:Entity)
                WHERE n.name CONTAINS $name
                  AND {where_fragment}
                OPTIONAL MATCH (c:Chunk)-[:MENTIONS]->(n)
                RETURN n, 0.6 AS score, c.chunk_index AS chunk_index, c.book_id AS book_id
                ORDER BY size(n.name) ASC
                LIMIT $limit
                """,
            ),
        ]

        async with self._read_session() as session:
            results_by_id: dict[str, EntityWithContext] = {}
            for _score, query in tier_queries:
                records = await self._run_with_timeout(
                    self._read_records(session, query, params)
                )
                for record in records:
                    entity = self._record_to_entity_with_context(record)
                    if entity.entity.id not in results_by_id:
                        results_by_id[entity.entity.id] = entity
                if results_by_id:
                    break

            if not results_by_id:
                try:
                    records = await self._run_with_timeout(
                        self._read_records(
                            session,
                            f"""
                            CALL db.index.fulltext.queryNodes(
                                "entity_name_aliases_index", $name
                            )
                            YIELD node AS n, score AS ft_score
                            WHERE {where_fragment}
                            OPTIONAL MATCH (c:Chunk)-[:MENTIONS]->(n)
                            RETURN
                                n,
                                ft_score * 0.4 AS score,
                                c.chunk_index AS chunk_index,
                                c.book_id AS book_id
                            ORDER BY score DESC
                            LIMIT $limit
                            """,
                            params,
                        )
                    )
                    for record in records:
                        entity = self._record_to_entity_with_context(record)
                        if entity.entity.id not in results_by_id:
                            results_by_id[entity.entity.id] = entity
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Fulltext index entity_name_aliases_index unavailable; "
                        "falling back to Tiers 1-3 for find_entity(%r): %s",
                        name,
                        exc,
                    )

            return list(results_by_id.values())

    async def find_entities_batch(self, ids: list[str]) -> list[EntityWithContext]:
        """Return entities for the given list of ids (max 200)."""
        async with self._read_session() as session:
            records = await self._run_with_timeout(
                self._read_records(
                    session,
                    """
                    UNWIND $ids AS id
                    MATCH (n:Entity {id: id})
                    OPTIONAL MATCH (c:Chunk)-[:MENTIONS]->(n)
                    RETURN n, c.chunk_index AS chunk_index, c.book_id AS book_id
                    """,
                    {"ids": ids},
                )
            )
            seen_ids: set[str] = set()
            entities: list[EntityWithContext] = []
            for record in records:
                entity = self._record_to_entity_with_context(record)
                if entity.entity.id not in seen_ids:
                    seen_ids.add(entity.entity.id)
                    entities.append(entity)
            return entities

    def _relationship_to_domain(self, rel: Any) -> Relationship:
        """Map a Neo4j Relationship to a domain ``Relationship``.

        Neo4j stores all edges as native type ``:RELATED`` with the semantic
        type (requires, enables, etc.) in a ``type`` property.  We must read
        the property, not the native edge type.
        """
        return Relationship(
            source_entity_id=rel.start_node["id"],
            target_entity_id=rel.end_node["id"],
            type=rel["type"],
            description=rel.get("description", ""),
            source_page=rel.get("source_page"),
            chunk_index=rel.get("chunk_index"),
        )

    async def traverse_relationships(
        self,
        source_id: str,
        rel_type: RelationshipType | None,
        depth: int,
        *,
        scope: ScopeContext | None = None,
    ) -> tuple[list[EntityWithContext], list[Relationship]]:
        """Traverse outgoing relationships up to ``depth`` levels.

        When ``scope`` is provided, relationship-type predicates are bound as
        parameters and appended to the path WHERE clause.
        """
        clamped_depth = max(0, min(depth, 3))

        if clamped_depth == 0:
            async with self._read_session() as session:
                records = await self._run_with_timeout(
                    self._read_records(
                        session,
                        "MATCH (start:Entity {id: $source_id}) RETURN start",
                        {"source_id": source_id},
                    )
                )
                if not records:
                    return [], []
                return [self._node_to_entity(records[0]["start"])], []

        scope_params, scope_clauses = self._build_scope_clause(scope)
        params: dict[str, Any] = {
            "source_id": source_id,
            "rel_type": rel_type,
            **scope_params,
        }

        predicates = [
            "($rel_type IS NULL OR ALL(r IN relationships(p) WHERE r.type = $rel_type))",
        ]
        path_clause = scope_clauses["path"]
        if path_clause:
            predicates.append(path_clause)
        where_fragment = " AND ".join(predicates)

        query = f"""
            MATCH p = (start:Entity {{id: $source_id}})-[:RELATED*1..{clamped_depth}]->(end:Entity)
            WHERE {where_fragment}
            RETURN start, end, relationships(p) AS rels
            LIMIT 100
        """
        async with self._read_session() as session:
            records = await self._run_with_timeout(
                self._read_records(
                    session,
                    query,
                    params,
                )
            )
            entity_by_id: dict[str, EntityWithContext] = {}
            relationships: list[Relationship] = []
            seen_rel_keys: set[tuple[str, str, str]] = set()
            for record in records:
                start_entity = self._node_to_entity(record["start"])
                entity_by_id[start_entity.entity.id] = start_entity
                end_entity = self._node_to_entity(record["end"])
                entity_by_id[end_entity.entity.id] = end_entity

                for rel in record["rels"]:
                    for node in (rel.start_node, rel.end_node):
                        entity = self._node_to_entity(node)
                        entity_by_id[entity.entity.id] = entity

                    domain_rel = self._relationship_to_domain(rel)
                    rel_key = (
                        domain_rel.source_entity_id,
                        domain_rel.target_entity_id,
                        domain_rel.type,
                    )
                    if rel_key not in seen_rel_keys:
                        seen_rel_keys.add(rel_key)
                        relationships.append(domain_rel)

            return list(entity_by_id.values()), relationships

    async def find_path(self, start_id: str, end_id: str, max_depth: int) -> list[GraphPath]:
        """Return shortest paths between two entities within ``max_depth``."""
        clamped_depth = max(1, min(max_depth, 3))
        query = f"""
            MATCH p = shortestPath(
                (a:Entity {{id: $start_id}})
                -[:RELATED*..{clamped_depth}]->
                (b:Entity {{id: $end_id}})
            )
            RETURN p
        """
        async with self._read_session() as session:
            records = await self._run_with_timeout(
                self._read_records(
                    session,
                    query,
                    {"start_id": start_id, "end_id": end_id},
                )
            )
            record: Any | None = records[0] if records else None
            if record is None:
                return []
            path = record["p"]
            nodes = [self._node_to_entity(node).entity for node in path.nodes]
            relationships = [self._relationship_to_domain(rel) for rel in path.relationships]
            return [GraphPath(nodes=nodes, relationships=relationships)]

    async def search_chunks(
        self, query: str, limit: int, *, scope: ScopeContext | None = None
    ) -> list[dict[str, Any]]:
        """Full-text search over chunk nodes with identity and provenance."""
        scope_params, scope_clauses = self._build_scope_clause(scope)
        params: dict[str, Any] = {"query": query, "limit": limit, **scope_params}

        chunk_clause = scope_clauses["chunk"]
        if chunk_clause:
            query_text = f"""
                CALL db.index.fulltext.queryNodes("chunk_text_index", $query)
                YIELD node, score
                WHERE {chunk_clause}
                OPTIONAL MATCH (parent)-[:HAS_CHUNK]->(node)
                WHERE parent:Chapter OR parent:Section
                WITH node, score, parent,
                     CASE WHEN parent:Chapter THEN parent ELSE null END AS chapter,
                     CASE WHEN parent:Section THEN parent ELSE null END AS section
                OPTIONAL MATCH (chapterAncestor:Chapter)
                    -[:HAS_SECTION|HAS_SUBSECTION*1..]->(section)
                RETURN node, score, chapter, section, chapterAncestor
                ORDER BY score DESC
                LIMIT $limit
                """
        else:
            query_text = """
                CALL db.index.fulltext.queryNodes("chunk_text_index", $query)
                YIELD node, score
                OPTIONAL MATCH (parent)-[:HAS_CHUNK]->(node)
                WHERE parent:Chapter OR parent:Section
                WITH node, score, parent,
                     CASE WHEN parent:Chapter THEN parent ELSE null END AS chapter,
                     CASE WHEN parent:Section THEN parent ELSE null END AS section
                OPTIONAL MATCH (chapterAncestor:Chapter)
                    -[:HAS_SECTION|HAS_SUBSECTION*1..]->(section)
                RETURN node, score, chapter, section, chapterAncestor
                ORDER BY score DESC
                LIMIT $limit
                """

        async with self._read_session() as session:
            records = await self._run_with_timeout(
                self._read_records(session, query_text, params)
            )
            return [self._chunk_payload(record) for record in records]

    @staticmethod
    def _chunk_payload(record: Any) -> dict[str, Any]:
        node = record["node"]
        chunk_index = node.get("chunk_index")
        book_id = node.get("book_id")
        chapter = record.get("chapter")
        section = record.get("section")
        chapter_ancestor = record.get("chapterAncestor")
        effective_chapter = chapter if chapter is not None else chapter_ancestor
        return {
            "chunk_id": (
                f"{book_id}:{chunk_index}"
                if book_id is not None and chunk_index is not None
                else None
            ),
            "chunk_index": chunk_index,
            "book_id": book_id,
            "chapter_id": Neo4jQueryAdapter._editorial_id(
                effective_chapter, "number", "title"
            ),
            "section_id": Neo4jQueryAdapter._editorial_id(
                section, "chapter_number", "title"
            ),
            "page_start": node.get("page_start"),
            "page_end": node.get("page_end"),
            "text": node.get("text", ""),
            "score": record["score"],
        }

    @staticmethod
    def _editorial_id(node: Any, number_field: str, title_field: str) -> str | None:
        if node is None:
            return None
        number = node.get(number_field)
        title = node.get(title_field)
        if number is None or title is None:
            return None
        return f"{number}:{title}"

    async def count_entities(
        self, entity_type: str | None, *, scope: ScopeContext | None = None
    ) -> int:
        """Return the number of entities, optionally filtered by type."""
        scope_params, scope_clauses = self._build_scope_clause(scope)
        params: dict[str, Any] = {"type": entity_type, **scope_params}

        predicates = ["($type IS NULL OR n.type = $type)"]
        entity_clause = scope_clauses["entity"]
        if entity_clause:
            predicates.append(entity_clause)
        where_fragment = " AND ".join(predicates)

        async with self._read_session() as session:
            records = await self._run_with_timeout(
                self._read_records(
                    session,
                    f"""
                    MATCH (n:Entity)
                    WHERE {where_fragment}
                    RETURN count(n) AS count
                    """,
                    params,
                )
            )
            return records[0]["count"] if records else 0

    async def list_entities(
        self,
        cursor: int,
        page_size: int,
        *,
        scope: ScopeContext | None = None,
    ) -> tuple[list[EntityWithContext], int]:
        """Cursor-based pagination over entities."""
        scope_params, scope_clauses = self._build_scope_clause(scope)
        params: dict[str, Any] = {
            "cursor": cursor,
            "page_size": page_size,
            **scope_params,
        }

        predicates = ["id(n) > $cursor"]
        entity_clause = scope_clauses["entity"]
        if entity_clause:
            predicates.append(entity_clause)
        where_fragment = " AND ".join(predicates)

        async with self._read_session() as session:
            records = await self._run_with_timeout(
                self._read_records(
                    session,
                    f"""
                    MATCH (n:Entity)
                    WHERE {where_fragment}
                    RETURN n, id(n) AS internal_id
                    ORDER BY id(n)
                    LIMIT $page_size
                    """,
                    params,
                )
            )
            entities = [self._node_to_entity(record["n"]) for record in records]
            next_cursor = records[-1]["internal_id"] if records else cursor
            return entities, next_cursor

    async def explain(self, cypher: str, parameters: dict[str, Any] | None = None) -> None:
        """Run ``EXPLAIN`` on ``cypher`` to validate it without executing it.

        Args:
            cypher: The Cypher statement to explain.
            parameters: Optional bound parameters for the statement.

        Raises:
            QueryTimeoutError: If the EXPLAIN exceeds the 3-second internal limit.
        """
        async with self._read_session() as session:
            await self._run_with_timeout(
                self._read_records(session, f"EXPLAIN {cypher}", parameters or {}),
                timeout=3.0,
            )

    async def execute_read(self, cypher: str) -> list[dict[str, Any]]:
        """Execute ``cypher`` and return raw record data as dictionaries.

        Raises:
            QueryTimeoutError: If the query exceeds the 3-second internal limit.
        """
        async with self._read_session() as session:
            records = await self._run_with_timeout(
                self._read_records(session, cypher, {}), timeout=3.0
            )
            return [record.data() for record in records]

    async def ensure_indexes(self) -> None:
        """Create read-side indexes idempotently."""
        index_statements = [
            "CREATE INDEX entity_name IF NOT EXISTS FOR (n:Entity) ON (n.name)",
            "CREATE INDEX entity_type IF NOT EXISTS FOR (n:Entity) ON (n.type)",
            "CREATE INDEX entity_id IF NOT EXISTS FOR (n:Entity) ON (n.id)",
            "CREATE INDEX rel_type IF NOT EXISTS FOR ()-[r:RELATED]-() ON (r.type)",
            "CREATE FULLTEXT INDEX chunk_text_index IF NOT EXISTS FOR (n:Chunk) ON EACH [n.text]",
            (
                "CREATE FULLTEXT INDEX entity_name_aliases_index "
                "IF NOT EXISTS FOR (n:Entity) "
                "ON EACH [n.name, n.canonical_name, n.aliases]"
            ),
        ]
        async with self._driver.session() as session:
            for statement in index_statements:
                await self._run_with_timeout(session.run(statement))
