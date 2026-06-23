"""Port for structured MCP query logging."""

from __future__ import annotations

import abc

from book_graph_rag.domain.models import QueryLogEntry


class QueryLoggerPort(abc.ABC):
    """Contract for adapters that persist structured query log entries."""

    @abc.abstractmethod
    async def log_query(self, entry: QueryLogEntry) -> None:
        """Persist ``entry`` to the configured logging backend."""
        ...

    @abc.abstractmethod
    async def close(self) -> None:
        """Release any resources held by the logger."""
        ...
