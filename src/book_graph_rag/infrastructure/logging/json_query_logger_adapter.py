"""JsonFileQueryLoggerAdapter implementation using stdlib logging rotation."""

from __future__ import annotations

import logging
import logging.handlers
from datetime import UTC, datetime, timedelta
from pathlib import Path

from book_graph_rag.config import Settings
from book_graph_rag.domain.models import QueryLogEntry
from book_graph_rag.ports.query_logger_port import QueryLoggerPort


class JsonFileQueryLoggerAdapter(QueryLoggerPort):
    """Logs ``QueryLogEntry`` instances as JSON lines with daily file rotation.

    Uses :class:`logging.handlers.TimedRotatingFileHandler` configured for
    midnight rollover. Each emitted record is exactly one JSON line produced by
    ``QueryLogEntry.model_dump_json()``. On construction the adapter removes
    rotated files older than ``mcp_log_retention_days`` while preserving the
    current base log file.
    """

    def __init__(self, settings: Settings) -> None:
        self._log_path: Path = settings.mcp_log_path
        self._retention_days: int = settings.mcp_log_retention_days
        self._closed: bool = False

        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._cleanup_old_logs()

        self._handler = logging.handlers.TimedRotatingFileHandler(
            filename=self._log_path,
            when="midnight",
            interval=1,
            encoding="utf-8",
        )
        self._handler.setFormatter(logging.Formatter("%(message)s"))

        self._logger = logging.getLogger(f"{__name__}.{id(self)}")
        self._logger.propagate = False
        self._logger.setLevel(logging.INFO)
        self._logger.addHandler(self._handler)

    def _cleanup_old_logs(self) -> None:
        """Delete rotated log files older than ``_retention_days``.

        The current base log file is preserved regardless of age so that the
        most recent active log is never removed during a routine restart.
        """
        cutoff = datetime.now(tz=UTC) - timedelta(days=self._retention_days)
        prefix = f"{self._log_path.name}."
        for path in self._log_path.parent.iterdir():
            if path == self._log_path:
                continue
            if not path.name.startswith(prefix):
                continue
            try:
                mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
                if mtime < cutoff:
                    path.unlink()
            except OSError:
                pass

    async def log_query(self, entry: QueryLogEntry) -> None:
        """Serialize ``entry`` to JSON and append it to the log file."""
        if self._closed:
            raise RuntimeError("JsonFileQueryLoggerAdapter is closed")
        self._logger.info(entry.model_dump_json())

    async def close(self) -> None:
        """Flush and close the underlying file handler."""
        if self._closed:
            return
        self._closed = True
        self._handler.flush()
        self._handler.close()
        self._logger.removeHandler(self._handler)
