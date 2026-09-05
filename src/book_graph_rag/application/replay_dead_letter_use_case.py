"""Replay dead-letter records, re-processing failed chunks atomically."""

from __future__ import annotations

import contextlib
import json
from collections.abc import Awaitable, Callable
from pathlib import Path

from book_graph_rag.domain.checkpoint_models import (
    CheckpointStatus,
    FailedChunkRecord,
    ReplayableChunk,
    VersionDimensions,
    parse_failed_chunk,
)
from book_graph_rag.domain.models import KnowledgeGraphChunk
from book_graph_rag.ports.checkpoint_port import CheckpointPort
from book_graph_rag.ports.dead_letter_port import DeadLetterPort
from book_graph_rag.ports.graph_db_port import GraphDatabasePort
from book_graph_rag.ports.llm_port import LLMProviderPort


class ReplayDeadLetterUseCase:
    """Read re-addressable failed-chunk records and re-process them.

    The use case loads each record's original chunk (via the injected
    ``chunk_loader``), re-extracts entities with the current LLM, and commits
    the result through ``commit_chunk_atomic`` under the current run's version
    dimensions. Any replay failure transitions the chunk back to ``FAILED`` and
    appends a fresh dead-letter record.
    """

    def __init__(
        self,
        checkpoint_port: CheckpointPort,
        graph_db_port: GraphDatabasePort,
        llm_port: LLMProviderPort,
        dead_letter_port: DeadLetterPort,
        versions: VersionDimensions,
        chunk_loader: Callable[[str, int], Awaitable[KnowledgeGraphChunk]],
        dead_letter_path: Path,
        max_attempts: int = 3,
    ) -> None:
        self._checkpoint_port = checkpoint_port
        self._graph_db_port = graph_db_port
        self._llm_port = llm_port
        self._dead_letter_port = dead_letter_port
        self._versions = versions
        self._chunk_loader = chunk_loader
        self._dead_letter_path = dead_letter_path
        self._max_attempts = max_attempts

    async def execute(
        self,
        source_id: str | None = None,
        limit: int | None = None,
        force_reprocess: bool = False,
    ) -> int:
        """Replay failed chunks.

        Returns the number of chunks successfully re-processed.
        """
        records = self._read_records()
        if source_id is not None:
            records = [record for record in records if record.source_id == source_id]
        if limit is not None:
            records = records[:limit]
        if not records:
            return 0

        await self._guard_against_processed_records(records, force_reprocess)

        processed = 0
        for record in records:
            if await self._replay_one(record):
                processed += 1
        return processed

    async def close(self) -> None:
        """Close any closable ports passed to the use case."""
        for port in (self._checkpoint_port, self._graph_db_port):
            if hasattr(port, "close"):
                await port.close()

    def _read_records(self) -> list[ReplayableChunk]:
        """Parse every re-addressable record from the dead-letter JSONL."""
        if not self._dead_letter_path.exists():
            return []
        records: list[ReplayableChunk] = []
        with self._dead_letter_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                payload = json.loads(line)
                records.append(parse_failed_chunk(payload))
        return records

    async def _guard_against_processed_records(
        self, records: list[ReplayableChunk], force_reprocess: bool
    ) -> None:
        """Ensure we do not silently overwrite currently-PROCESSED chunks."""
        by_source: dict[str, list[int]] = {}
        for record in records:
            by_source.setdefault(record.source_id, []).append(record.chunk_index)

        for source, indices in by_source.items():
            state = await self._checkpoint_port.fetch_state(source, indices)
            for index in indices:
                checkpoint = state.get(index)
                if (
                    checkpoint is not None
                    and checkpoint.status == CheckpointStatus.PROCESSED
                    and not force_reprocess
                ):
                    raise ValueError(
                        f"Chunk {source}:{index} is already PROCESSED; "
                        "use --force-reprocess to replay it"
                    )

    async def _replay_one(self, record: ReplayableChunk) -> bool:
        """Re-process a single failed chunk.

        Returns ``True`` when the chunk reached ``PROCESSED``.
        """
        chunk = await self._chunk_loader(record.source_id, record.chunk_index)
        lease = await self._checkpoint_port.acquire_lease(
            record.source_id, record.chunk_index, self._versions
        )
        attempt = lease.checkpoint.attempt + 1

        try:
            extracted = await self._llm_port.extract_graph(chunk)
        except Exception as error:  # noqa: BLE001
            await self._fail_record(record, chunk, attempt, error)
            return False

        entity_ids = [entity.id for entity in extracted.entities]
        try:
            await self._graph_db_port.commit_chunk_atomic(
                extracted,
                entity_ids,
                self._versions,
                attempt=attempt,
                lease_owned=True,
            )
        except Exception as error:  # noqa: BLE001
            await self._fail_record(record, chunk, attempt, error)
            return False

        return True

    async def _fail_record(
        self,
        record: ReplayableChunk,
        chunk: KnowledgeGraphChunk,
        attempt: int,
        error: Exception,
    ) -> None:
        """Transition the chunk back to FAILED and append a fresh dead-letter record."""
        error_type = type(error).__name__
        error_message = str(error)
        with contextlib.suppress(Exception):
            await self._checkpoint_port.release_lease_to_failed(
                record.source_id,
                record.chunk_index,
                error_type,
                error_message,
                attempt,
            )

        dead_letter_record = FailedChunkRecord(
            source_id=record.source_id,
            chunk_index=record.chunk_index,
            page_ref=chunk.page_ref,
            source_version=self._versions.source_version,
            pipeline_version=self._versions.pipeline_version,
            model_version=self._versions.model_version,
            schema_version=self._versions.schema_version,
            attempt=attempt,
            checkpoint_status=CheckpointStatus.FAILED,
            error_type=error_type,
            error_message=error_message,
        )
        await self._dead_letter_port.write_failed_chunk(
            dead_letter_record.model_dump(mode="json")
        )
