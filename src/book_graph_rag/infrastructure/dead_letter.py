"""JSONL implementation of the dead-letter port."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from book_graph_rag.ports.dead_letter_port import DeadLetterPort


class JSONLDeadLetter(DeadLetterPort):
    """Append-only JSONL dead-letter writer.

    Writes are offloaded to the default thread-pool executor so the async
    event loop is not blocked by disk I/O.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    async def write_orphan_relationship(self, record: dict[str, Any]) -> None:
        """Append the orphan record as a single JSONL line."""
        enriched = {
            **record,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        await asyncio.get_running_loop().run_in_executor(
            None, self._sync_append, enriched
        )

    def _sync_append(self, record: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
