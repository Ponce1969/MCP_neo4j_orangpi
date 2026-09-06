"""IndexBookUseCase orchestrates PDF extraction, LLM graph extraction, and graph persistence.

The use case implements a streaming producer/consumer pipeline with bounded
concurrency (asyncio.Semaphore) and mini-batch persistence
(asyncio.Queue + sentinel). Failed chunks are written to a dead-letter log
(JSONL) and skipped without aborting the pipeline.

Phase 2 adds resumable indexing: when a ``CheckpointPort`` is supplied and
``checkpoint_enabled`` is true, the use case skips already-``PROCESSED`` chunks,
acquires per-chunk leases, and persists each chunk atomically via
``commit_chunk_atomic``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timezone
from pathlib import Path

from book_graph_rag.domain.checkpoint_models import (
    Checkpoint,
    CheckpointStatus,
    FailedChunkRecord,
    VersionDimensions,
)
from book_graph_rag.domain.models import Entity, KnowledgeGraphChunk, Relationship
from book_graph_rag.ports.checkpoint_port import CheckpointPort
from book_graph_rag.ports.dead_letter_port import DeadLetterPort
from book_graph_rag.ports.graph_db_port import GraphDatabasePort
from book_graph_rag.ports.llm_port import LLMProviderPort
from book_graph_rag.ports.pdf_port import PDFReaderPort


class IndexBookUseCase:
    """Orchestrate indexing a single PDF into the knowledge graph.

    The constructor receives **ports** (abstract interfaces) and primitive
    configuration values, never concrete adapters or ``Settings``. This keeps
    the application layer independent of infrastructure configuration
    mechanisms.
    """

    def __init__(
        self,
        pdf_port: PDFReaderPort,
        llm_port: LLMProviderPort,
        graph_db_port: GraphDatabasePort,
        max_concurrency: int,
        batch_size: int,
        dead_letter_path: Path,
        checkpoint_port: CheckpointPort | None = None,
        dead_letter_port: DeadLetterPort | None = None,
        versions: VersionDimensions | None = None,
        checkpoint_enabled: bool = False,
        resume: bool = True,
        max_attempts: int = 3,
        stale_lease_seconds: int = 300,
        force_reprocess: bool = False,
    ) -> None:
        self._pdf_port = pdf_port
        self._llm_port = llm_port
        self._graph_db_port = graph_db_port
        self._max_concurrency = max_concurrency
        self._batch_size = batch_size
        self._dead_letter_path = dead_letter_path
        self._checkpoint_port = checkpoint_port
        self._dead_letter_port = dead_letter_port
        self._versions = versions
        self._checkpoint_enabled = checkpoint_enabled
        self._resume = resume
        self._max_attempts = max_attempts
        self._stale_lease_seconds = stale_lease_seconds
        self._force_reprocess = force_reprocess
        self._logger = logging.getLogger(__name__)

    async def close(self) -> None:
        """Close any closable ports held by the use case."""
        for port in (self._graph_db_port, self._checkpoint_port, self._dead_letter_port):
            if port is not None and hasattr(port, "close"):
                with contextlib.suppress(Exception):
                    await port.close()

    async def execute(self, pdf_path: str) -> None:
        """Index ``pdf_path`` into the graph database.

        Steps:

        1. Pull all chunks from the synchronous PDF iterator into a list.
           This trades lazy iteration for a deterministic trigger point for
           ``upsert_book`` (the first chunk already carries the ``Book``).
        2. Upsert the book once, if any chunk references one.
        3. If checkpointing is enabled, run the Phase 2 lease-orchestration
           loop: mark stale rows, reclaim stale leases, short-circuit
           ``PROCESSED`` chunks, and atomically commit the rest.
        4. Otherwise fall back to the legacy batch-flush path.
        5. Failed chunks are written to the configured dead-letter sink and
           skipped.
        """
        chunk_iter: Iterator[KnowledgeGraphChunk] = self._pdf_port.extract_chunks(pdf_path)
        chunks: list[KnowledgeGraphChunk] = list(chunk_iter)

        if not chunks:
            return

        first_chunk = chunks[0]
        if first_chunk.book is not None:
            await self._graph_db_port.upsert_book(first_chunk.book)

        if self._checkpoint_enabled and self._checkpoint_port is not None:
            await self._execute_resumable(chunks)
        else:
            await self._execute_legacy(chunks)

    async def _execute_resumable(self, chunks: list[KnowledgeGraphChunk]) -> None:
        """Phase 2 per-chunk lease orchestration."""
        assert self._checkpoint_port is not None
        assert self._versions is not None

        first_chunk = chunks[0]
        if first_chunk.book is None:
            raise ValueError("Resumable indexing requires chunks that reference a Book")
        source_id = first_chunk.book.id

        if self._resume:
            await self._checkpoint_port.mark_stale_and_reset(source_id, self._versions)
            await self._checkpoint_port.reclaim_stale_leases(
                source_id, datetime.now(UTC), self._stale_lease_seconds
            )
            state = await self._checkpoint_port.fetch_state(
                source_id, [chunk.chunk_index for chunk in chunks]
            )
            state = await self._maybe_force_reprocess(source_id, state)
        else:
            state = {}

        semaphore = asyncio.Semaphore(self._max_concurrency)
        tasks: list[asyncio.Task[None]] = []
        for chunk in chunks:
            checkpoint = state.get(chunk.chunk_index)
            if checkpoint is not None and checkpoint.status == CheckpointStatus.PROCESSED:
                continue
            if checkpoint is not None and checkpoint.attempt >= self._max_attempts:
                self._logger.warning(
                    "Skipping chunk %s:%s because attempt count %s has reached "
                    "checkpoint_max_attempts (%s); recover via explicit dead-letter replay",
                    source_id,
                    chunk.chunk_index,
                    checkpoint.attempt,
                    self._max_attempts,
                )
                continue
            tasks.append(asyncio.create_task(self._process_chunk(chunk, source_id, semaphore)))

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    # Unhandled errors are logged but do not crash the pipeline.
                    continue

    async def _maybe_force_reprocess(
        self, source_id: str, state: dict[int, Checkpoint]
    ) -> dict[int, Checkpoint]:
        """Handle version-mismatched PROCESSED rows according to force_reprocess."""
        from book_graph_rag.domain.checkpoint_state_machine import is_stale

        assert self._versions is not None
        mismatched: list[int] = []
        for chunk_index, checkpoint in state.items():
            if checkpoint.status == CheckpointStatus.PROCESSED and is_stale(
                checkpoint, self._versions
            ):
                mismatched.append(chunk_index)

        if not mismatched:
            return state

        if not self._force_reprocess:
            self._logger.warning(
                "PROCESSED chunks %s for %s have version dimensions that differ from "
                "the current run; preserving them because --force-reprocess was not set",
                sorted(mismatched),
                source_id,
            )
            return state

        # Mark mismatched PROCESSED rows as STALE so the normal loop will re-process them.
        assert self._checkpoint_port is not None
        for chunk_index in mismatched:
            await self._checkpoint_port.acquire_lease(source_id, chunk_index, self._versions)
        return {
            chunk_index: checkpoint
            for chunk_index, checkpoint in state.items()
            if chunk_index not in mismatched
        }

    async def _process_chunk(
        self,
        chunk: KnowledgeGraphChunk,
        source_id: str,
        semaphore: asyncio.Semaphore,
    ) -> None:
        """Acquire lease, extract, commit; on any failure transition to FAILED."""
        assert self._checkpoint_port is not None
        assert self._versions is not None
        lease = await self._checkpoint_port.acquire_lease(
            source_id, chunk.chunk_index, self._versions
        )
        attempt = lease.checkpoint.attempt + 1

        async with semaphore:
            try:
                extracted = await self._llm_port.extract_graph(chunk)
            except Exception as error:  # noqa: BLE001 - chunk errors are recoverable per spec
                await self._fail_chunk(chunk, source_id, attempt, error)
                return

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
            await self._fail_chunk(chunk, source_id, attempt, error)

    async def _fail_chunk(
        self,
        chunk: KnowledgeGraphChunk,
        source_id: str,
        attempt: int,
        error: Exception,
    ) -> None:
        """Release the lease to FAILED and append a re-addressable dead-letter record."""
        assert self._checkpoint_port is not None
        assert self._versions is not None
        error_type = type(error).__name__
        error_message = str(error)
        with contextlib.suppress(Exception):
            await self._checkpoint_port.release_lease_to_failed(
                source_id, chunk.chunk_index, error_type, error_message, attempt
            )

        if self._dead_letter_port is not None:
            record = FailedChunkRecord(
                source_id=source_id,
                chunk_index=chunk.chunk_index,
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
            await self._dead_letter_port.write_failed_chunk(record.model_dump(mode="json"))

    async def _execute_legacy(self, chunks: list[KnowledgeGraphChunk]) -> None:
        """Pre-Phase 2 batch-flush path, preserved for ``--no-resume`` callers."""
        queue: asyncio.Queue[KnowledgeGraphChunk | None] = asyncio.Queue()
        semaphore = asyncio.Semaphore(self._max_concurrency)

        consumer_task = asyncio.create_task(self._consume(queue))

        producer_tasks = [
            asyncio.create_task(self._produce_chunk(chunk, semaphore, queue)) for chunk in chunks
        ]

        await asyncio.gather(*producer_tasks)
        await queue.put(None)
        await consumer_task

    async def _produce_chunk(
        self,
        chunk: KnowledgeGraphChunk,
        semaphore: asyncio.Semaphore,
        queue: asyncio.Queue[KnowledgeGraphChunk | None],
    ) -> None:
        async with semaphore:
            try:
                extracted = await self._llm_port.extract_graph(chunk)
            except Exception as error:  # noqa: BLE001 - chunk errors are recoverable per spec
                self._write_dead_letter(chunk, error)
                return
            await queue.put(extracted)

    async def _consume(self, queue: asyncio.Queue[KnowledgeGraphChunk | None]) -> None:
        batch: list[KnowledgeGraphChunk] = []
        seen_book_ids: set[str] = set()

        while True:
            item = await queue.get()
            if item is None:
                if batch:
                    await self._flush_batch(batch, seen_book_ids)
                break

            batch.append(item)
            if len(batch) >= self._batch_size:
                await self._flush_batch(batch, seen_book_ids)
                batch = []

    async def _flush_batch(self, batch: list[KnowledgeGraphChunk], seen_book_ids: set[str]) -> None:
        all_entities: list[Entity] = []
        all_relationships: list[Relationship] = []
        chunk_provenance: list[tuple[int, str | None, list[str]]] = []

        for chunk in batch:
            if chunk.book is not None and chunk.book.id not in seen_book_ids:
                await self._graph_db_port.upsert_book(chunk.book)
                seen_book_ids.add(chunk.book.id)

            if chunk.chapter is not None:
                sections = [*chunk.section_ancestors]
                if chunk.section is not None:
                    sections.append(chunk.section)
                await self._graph_db_port.upsert_editorial_structure(chunk.chapter, sections, chunk)

            book_id = chunk.book.id if chunk.book is not None else None
            entity_ids = [entity.id for entity in chunk.entities]
            chunk_provenance.append((chunk.chunk_index, book_id, entity_ids))

            all_entities.extend(chunk.entities)
            all_relationships.extend(
                rel.model_copy(
                    update={
                        "chunk_index": chunk.chunk_index,
                        "source_page": (
                            rel.source_page if rel.source_page is not None else chunk.page_ref.start
                        ),
                    }
                )
                for rel in chunk.relationships
            )

        await self._graph_db_port.upsert_entities(all_entities)
        await self._graph_db_port.upsert_relationships(all_relationships)

        for chunk_index, book_id, entity_ids in chunk_provenance:
            await self._graph_db_port.upsert_mentions(chunk_index, book_id, entity_ids)

    def _write_dead_letter(self, chunk: KnowledgeGraphChunk, error: Exception) -> None:
        record = {
            "chunk_index": chunk.chunk_index,
            "page_ref": {"start": chunk.page_ref.start, "end": chunk.page_ref.end},
            "error_type": type(error).__name__,
            "error_message": str(error),
            "timestamp": datetime.now(timezone.utc).isoformat(),  # noqa: UP017
        }
        with self._dead_letter_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
