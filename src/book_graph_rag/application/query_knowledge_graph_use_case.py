"""Query use case: dispatch read-side graph queries to the query port."""

from __future__ import annotations

import time

from book_graph_rag.domain.models import (
    BatchSizeExceededError,
    GraphQueryResult,
    GraphQueryUnion,
    QueryMetadata,
    UnsupportedQueryTypeError,
)
from book_graph_rag.ports.graph_query_port import GraphQueryPort


class QueryKnowledgeGraphUseCase:
    """Execute a read-side graph query through a single discriminated entry point."""

    def __init__(
        self,
        port: GraphQueryPort,
        max_batch_size: int = 200,
        max_depth: int = 3,
    ) -> None:
        self._port = port
        self._max_batch_size = max_batch_size
        self._max_depth = max_depth

    async def execute(self, query: GraphQueryUnion) -> GraphQueryResult:
        """Dispatch ``query`` by ``type`` and return a unified result."""
        start = time.monotonic()

        match query.type:
            case "entity":
                entities = await self._port.find_entity(
                    query.name, query.entity_type
                )
                return GraphQueryResult(
                    entities=entities,
                    metadata=QueryMetadata(
                        total_count=len(entities),
                        query_ms=(time.monotonic() - start) * 1000,
                    ),
                )

            case "batch_entity":
                if len(query.ids) > self._max_batch_size:
                    raise BatchSizeExceededError(
                        limit=self._max_batch_size,
                        received=len(query.ids),
                    )
                entities = await self._port.find_entities_batch(query.ids)
                return GraphQueryResult(
                    entities=entities,
                    metadata=QueryMetadata(
                        total_count=len(entities),
                        query_ms=(time.monotonic() - start) * 1000,
                    ),
                )

            case "relation":
                depth = max(0, min(query.depth, self._max_depth))
                entities, relationships = await self._port.traverse_relationships(
                    query.source_id,
                    query.rel_type,
                    depth,
                )
                return GraphQueryResult(
                    entities=entities,
                    relationships=relationships,
                    metadata=QueryMetadata(
                        total_count=len(entities),
                        depth=depth,
                        query_ms=(time.monotonic() - start) * 1000,
                    ),
                )

            case "path":
                paths = await self._port.find_path(
                    query.start_id,
                    query.end_id,
                    query.max_depth,
                )
                return GraphQueryResult(
                    paths=paths,
                    metadata=QueryMetadata(
                        total_count=len(paths),
                        depth=query.max_depth,
                        query_ms=(time.monotonic() - start) * 1000,
                    ),
                )

            case "similarity":
                raise UnsupportedQueryTypeError("similarity")

            case unknown_type:
                raise UnsupportedQueryTypeError(str(unknown_type))
