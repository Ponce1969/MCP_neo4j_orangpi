"""IndexBookUseCase orchestrates PDF extraction, LLM graph extraction, and graph persistence.

The use case implements a streaming producer/consumer pipeline with bounded
concurrency (asyncio.Semaphore) and mini-batch persistence
(asyncio.Queue + sentinel). Failed chunks are written to a dead-letter log
(JSONL) and skipped without aborting the pipeline.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from book_graph_rag.domain.models import Entity, KnowledgeGraphChunk, Relationship
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
    ) -> None:
        self._pdf_port = pdf_port
        self._llm_port = llm_port
        self._graph_db_port = graph_db_port
        self._max_concurrency = max_concurrency
        self._batch_size = batch_size
        self._dead_letter_path = dead_letter_path

    async def execute(self, pdf_path: str) -> None:
        """Index ``pdf_path`` into the graph database.

        Steps:

        1. Pull all chunks from the synchronous PDF iterator into a list.
           This trades lazy iteration for a deterministic trigger point for
           ``upsert_book`` (the first chunk already carries the ``Book``).
        2. Upsert the book once, if any chunk references one.
        3. Spawn producer tasks that call the LLM port under a semaphore.
        4. A single consumer coroutine drains an ``asyncio.Queue``, batches
           ``batch_size`` extracted chunks, and flushes them to the graph DB.
        5. Failed chunks are written to ``dead_letter_path`` and skipped.
        """
        chunk_iter: Iterator[KnowledgeGraphChunk] = self._pdf_port.extract_chunks(pdf_path)
        # We materialise the iterator so that:
        #   (a) the consumer works with an async-safe list, and
        #   (b) ``upsert_book`` can be triggered before any LLM work begins.
        chunks: list[KnowledgeGraphChunk] = list(chunk_iter)

        if not chunks:
            return

        first_chunk = chunks[0]
        if first_chunk.book is not None:
            await self._graph_db_port.upsert_book(first_chunk.book)

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

        for chunk in batch:
            if chunk.book is not None and chunk.book.id not in seen_book_ids:
                await self._graph_db_port.upsert_book(chunk.book)
                seen_book_ids.add(chunk.book.id)

            if chunk.chapter is not None:
                sections = [chunk.section] if chunk.section is not None else []
                await self._graph_db_port.upsert_editorial_structure(
                    chunk.chapter, sections, chunk
                )

            all_entities.extend(chunk.entities)
            all_relationships.extend(chunk.relationships)

        await self._graph_db_port.upsert_entities(all_entities)
        await self._graph_db_port.upsert_relationships(all_relationships)

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
