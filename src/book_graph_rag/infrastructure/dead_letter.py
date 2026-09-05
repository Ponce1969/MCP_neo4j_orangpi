"""JSONL implementation of the dead-letter port."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from book_graph_rag.ports.dead_letter_port import DeadLetterPort


class JSONLDeadLetter(DeadLetterPort):
    """Append-only JSONL dead-letter writer.

    Writes are offloaded to the default thread-pool executor so the async
    event loop is not blocked by disk I/O.
    """

    def __init__(self, path: Path, failed_chunk_path: Path | None = None) -> None:
        self._path = path
        self._failed_chunk_path = failed_chunk_path if failed_chunk_path is not None else path

    async def write_orphan_relationship(self, record: dict[str, Any]) -> None:
        """Append the orphan record as a single JSONL line."""
        enriched = {
            **record,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        await asyncio.get_running_loop().run_in_executor(
            None, self._sync_append, enriched
        )

    async def write_failed_chunk(self, record: dict[str, Any]) -> None:
        """Append a re-addressable failed-chunk record to the chunks JSONL."""
        enriched = {
            **record,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        await asyncio.get_running_loop().run_in_executor(
            None, self._sync_append_to, self._failed_chunk_path, enriched
        )

    @staticmethod
    def _parse_line(line: str) -> dict[str, Any]:
        """Parse a single JSONL line."""
        return cast(dict[str, Any], json.loads(line))

    def _sync_append(self, record: dict[str, Any]) -> None:
        self._sync_append_to(self._path, record)

    @staticmethod
    def _sync_append_to(path: Path, record: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
