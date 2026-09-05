"""GraphDatabasePort implementation using the async Neo4j driver.

All write operations use ``MERGE`` and parameters for idempotent,
 injection-safe persistence.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from neo4j import AsyncGraphDatabase

from book_graph_rag.config import Settings
from book_graph_rag.domain.checkpoint_models import Checkpoint, CheckpointStatus, VersionDimensions
from book_graph_rag.domain.models import (
    Book,
    Chapter,
    Entity,
    KnowledgeGraphChunk,
    Relationship,
    Section,
)
from book_graph_rag.infrastructure.dead_letter import JSONLDeadLetter
from book_graph_rag.ports.dead_letter_port import DeadLetterPort
from book_graph_rag.ports.graph_db_port import GraphDatabasePort


class Neo4jCommandAdapter(GraphDatabasePort):
    """Async Neo4j implementation of ``GraphDatabasePort`` for write commands."""

    def __init__(self, settings: Settings, dead_letter_port: DeadLetterPort | None = None) -> None:
        self._settings = settings
        # Deserialize the SecretStr once at construction time. The password is
        # passed to the driver and never logged or printed by this adapter.
        self._driver: Any = AsyncGraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value()),
        )
        self._orphan_policy: str = settings.relationship_orphan_policy
        self._dead_letter: DeadLetterPort = (
            dead_letter_port
            if dead_letter_port is not None
            else JSONLDeadLetter(settings.dead_letter_path_orphans)
        )

    async def close(self) -> None:
        """Close the underlying Neo4j driver."""
        await self._driver.close()

    @staticmethod
    def _to_datetime(value: Any) -> datetime | None:
        """Convert Neo4j DateTime values to Python datetimes."""
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        return value.to_native()  # type: ignore[no-any-return]

    async def upsert_book(self, book: Book) -> None:
        """Idempotently persist the book root node (MERGE by id)."""
        async with self._driver.session() as session:
            await session.run(
                """
                MERGE (b:Book {id: $id})
                SET b.title = $title,
                    b.author = $author,
                    b.pdf_path = $pdf_path,
                    b.page_count = $page_count
                """,
                {
                    "id": book.id,
                    "title": book.title,
                    "author": book.author,
                    "pdf_path": book.pdf_path,
                    "page_count": book.page_count,
                },
            )

    async def upsert_entities(self, entities: list[Entity]) -> None:
        """Idempotently persist entities (MERGE by id)."""
        async with self._driver.session() as session:
            await session.run(
                """
                UNWIND $entities AS e
                MERGE (n:Entity {id: e.id})
                SET n.name = e.name,
                    n.type = e.type,
                    n.description = e.description,
                    n.source_page = e.source_page,
                    n.aliases = e.aliases,
                    n.canonical_name = e.canonical_name
                """,
                {"entities": [entity.model_dump() for entity in entities]},
            )

    async def upsert_relationships(self, relationships: list[Relationship]) -> None:
        """Idempotently persist relationships with endpoint validation.

        Runs one batched set-membership query to detect missing endpoints, then
        either raises (``fail_loud``) or writes orphans to the dead-letter log
        and persists the valid subset (``log_orphan``). The zero-silent-drop
        invariant is ``input == persisted + dead_lettered_orphans``.
        """
        if not relationships:
            return

        source_ids = [rel.source_entity_id for rel in relationships]
        target_ids = [rel.target_entity_id for rel in relationships]

        async with self._driver.session() as session:
            result = await session.run(
                """
                WITH $source_ids AS src_ids, $target_ids AS dst_ids
                UNWIND (src_ids + dst_ids) AS id
                WITH DISTINCT id
                OPTIONAL MATCH (n:Entity {id: id})
                RETURN collect(DISTINCT id) AS requested,
                       collect(DISTINCT n.id) AS found_ids
                """,
                {"source_ids": source_ids, "target_ids": target_ids},
            )
            record = await result.single()

        missing_ids: set[str] = set()
        if record is not None:
            requested: set[str] = set(record["requested"])
            found_ids: set[str] = set(record["found_ids"])
            missing_ids = requested - found_ids

        valid_relationships: list[Relationship] = []
        orphans: list[dict[str, Any]] = []
        for rel in relationships:
            src_missing = rel.source_entity_id in missing_ids
            dst_missing = rel.target_entity_id in missing_ids
            if src_missing or dst_missing:
                missing_endpoint = (
                    "both"
                    if src_missing and dst_missing
                    else ("source" if src_missing else "target")
                )
                orphans.append(
                    {
                        "reason": "orphan_endpoint",
                        "type": rel.type,
                        "source_entity_id": rel.source_entity_id,
                        "target_entity_id": rel.target_entity_id,
                        "description": rel.description,
                        "source_page": rel.source_page,
                        "chunk_index": rel.chunk_index,
                        "missing_endpoint": missing_endpoint,
                    }
                )
            else:
                valid_relationships.append(rel)

        if orphans and self._orphan_policy == "fail_loud":
            missing = sorted(missing_ids)
            raise ValueError(f"Relationship batch aborted: missing endpoints {missing}")

        for orphan in orphans:
            await self._dead_letter.write_orphan_relationship(orphan)

        if valid_relationships:
            async with self._driver.session() as session:
                await session.run(
                    """
                    UNWIND $rels AS r
                    MATCH (src:Entity {id: r.source_entity_id}),
                          (dst:Entity {id: r.target_entity_id})
                    MERGE (src)-[rel:RELATED {type: r.type}]->(dst)
                    SET rel.description = r.description,
                        rel.source_page = r.source_page,
                        rel.chunk_index = r.chunk_index
                    """,
                    {"rels": [rel.model_dump() for rel in valid_relationships]},
                )

    async def upsert_mentions(
        self, chunk_index: int, book_id: str | None, entity_ids: list[str]
    ) -> None:
        """Idempotently persist (:Chunk)-[:MENTIONS]->(:Entity) edges.

        The ``WHERE`` guard is required because Cypher equality with ``NULL``
        evaluates to ``UNKNOWN``; a direct ``{book_id: $book_id}`` matcher would
        fail to match chunks whose ``book_id`` is null (TOC-less PDFs).
        """
        async with self._driver.session() as session:
            await session.run(
                """
                UNWIND $entity_ids AS eid
                MATCH (c:Chunk {chunk_index: $chunk_index})
                WHERE ($book_id IS NULL AND c.book_id IS NULL) OR c.book_id = $book_id
                MATCH (e:Entity {id: eid})
                MERGE (c)-[m:MENTIONS]->(e)
                SET m.source_page = coalesce(m.source_page, e.source_page)
                """,
                {
                    "chunk_index": chunk_index,
                    "book_id": book_id,
                    "entity_ids": entity_ids,
                },
            )

    async def upsert_editorial_structure(
        self, chapter: Chapter, sections: list[Section], chunk: KnowledgeGraphChunk
    ) -> None:
        """Persist the book's editorial hierarchy (chapter → section → chunk).

        Idempotent via ``MERGE``. If ``chunk.book`` is ``None`` (fallback for
        PDFs without TOC), the chunk node is created without a book link.
        """
        async with self._driver.session() as session:
            if chunk.book is not None:
                await session.run(
                    """
                    MERGE (b:Book {id: $book_id})
                    MERGE (ch:Chapter {number: $chapter_number, title: $chapter_title})
                    SET ch.page_start = $chapter_page_start
                    MERGE (b)-[:CONTAINS]->(ch)
                    """,
                    {
                        "book_id": chunk.book.id,
                        "chapter_number": chapter.number,
                        "chapter_title": chapter.title,
                        "chapter_page_start": chapter.page_start,
                    },
                )

            for section in sections:
                if section.parent_section_title is None:
                    await session.run(
                        """
                        MATCH (ch:Chapter {number: $chapter_number, title: $chapter_title})
                        MERGE (sec:Section {title: $section_title, chapter_number: $chapter_number})
                        SET sec.level = $section_level,
                            sec.page_start = $section_page_start
                        MERGE (ch)-[:HAS_SECTION]->(sec)
                        """,
                        {
                            "chapter_number": chapter.number,
                            "chapter_title": chapter.title,
                            "section_title": section.title,
                            "section_level": section.level,
                            "section_page_start": section.page_start,
                        },
                    )
                else:
                    await session.run(
                        """
                        MATCH (parent:Section {
                            title: $parent_section_title,
                            chapter_number: $chapter_number
                        })
                        MERGE (sec:Section {title: $section_title, chapter_number: $chapter_number})
                        SET sec.level = $section_level,
                            sec.page_start = $section_page_start
                        MERGE (parent)-[:HAS_SUBSECTION]->(sec)
                        """,
                        {
                            "chapter_number": chapter.number,
                            "section_title": section.title,
                            "section_level": section.level,
                            "section_page_start": section.page_start,
                            "parent_section_title": section.parent_section_title,
                        },
                    )

            book_id = chunk.book.id if chunk.book is not None else None
            await session.run(
                """
                MERGE (k:Chunk {chunk_index: $chunk_index, book_id: $book_id})
                SET k.text = $text,
                    k.page_start = $page_start,
                    k.page_end = $page_end
                """,
                {
                    "chunk_index": chunk.chunk_index,
                    "book_id": book_id,
                    "text": chunk.text,
                    "page_start": chunk.page_ref.start,
                    "page_end": chunk.page_ref.end,
                },
            )

            if sections:
                await session.run(
                    """
                    MATCH (sec:Section {title: $section_title, chapter_number: $chapter_number}),
                          (k:Chunk {chunk_index: $chunk_index, book_id: $book_id})
                    MERGE (sec)-[:HAS_CHUNK]->(k)
                    """,
                    {
                        "section_title": sections[-1].title,
                        "chapter_number": chapter.number,
                        "chunk_index": chunk.chunk_index,
                        "book_id": book_id,
                    },
                )
            elif chunk.book is not None:
                await session.run(
                    """
                    MATCH (ch:Chapter {number: $chapter_number, title: $chapter_title}),
                          (k:Chunk {chunk_index: $chunk_index, book_id: $book_id})
                    MERGE (ch)-[:HAS_CHUNK]->(k)
                    """,
                    {
                        "chapter_number": chapter.number,
                        "chapter_title": chapter.title,
                        "chunk_index": chunk.chunk_index,
                        "book_id": book_id,
                    },
                )

    async def commit_chunk_atomic(
        self,
        chunk: KnowledgeGraphChunk,
        entity_ids: list[str],
        versions: VersionDimensions,
        *,
        attempt: int = 1,
        lease_owned: bool = True,
    ) -> Checkpoint:
        """Persist one chunk's writes + checkpoint row in a single transaction."""
        if chunk.book is None:
            raise ValueError("commit_chunk_atomic requires a chunk with a Book")
        book = chunk.book
        source_id = book.id

        async def _tx(tx: Any) -> tuple[Checkpoint, list[dict[str, Any]]]:
            assert chunk.book is not None
            # 1. Chunk node (durable identity is source_id + chunk_index).
            await tx.run(
                """
                MERGE (k:Chunk {source_id: $source_id, chunk_index: $chunk_index})
                SET k.text = $text,
                    k.page_start = $page_start,
                    k.page_end = $page_end
                """,
                {
                    "source_id": source_id,
                    "chunk_index": chunk.chunk_index,
                    "text": chunk.text,
                    "page_start": chunk.page_ref.start,
                    "page_end": chunk.page_ref.end,
                },
            )

            # 2. Editorial structure (only when the chunk belongs to a book).
            if chunk.chapter is not None:
                await tx.run(
                    """
                    MERGE (b:Book {id: $book_id})
                    SET b.title = $title,
                        b.author = $author,
                        b.pdf_path = $pdf_path,
                        b.page_count = $page_count
                    MERGE (ch:Chapter {number: $chapter_number, title: $chapter_title})
                    SET ch.page_start = $chapter_page_start
                    MERGE (b)-[:CONTAINS]->(ch)
                    """,
                    {
                        "book_id": chunk.book.id,
                        "title": chunk.book.title,
                        "author": chunk.book.author,
                        "pdf_path": chunk.book.pdf_path,
                        "page_count": chunk.book.page_count,
                        "chapter_number": chunk.chapter.number,
                        "chapter_title": chunk.chapter.title,
                        "chapter_page_start": chunk.chapter.page_start,
                    },
                )

                if chunk.section is not None:
                    await tx.run(
                        """
                        MERGE (sec:Section {title: $section_title, chapter_number: $chapter_number})
                        SET sec.level = $section_level,
                            sec.page_start = $section_page_start
                        WITH sec
                        MATCH (ch:Chapter {number: $chapter_number, title: $chapter_title})
                        MERGE (ch)-[:HAS_SECTION]->(sec)
                        WITH sec
                        MATCH (k:Chunk {source_id: $source_id, chunk_index: $chunk_index})
                        MERGE (sec)-[:HAS_CHUNK]->(k)
                        """,
                        {
                            "section_title": chunk.section.title,
                            "chapter_number": chunk.section.chapter_number,
                            "section_level": chunk.section.level,
                            "section_page_start": chunk.section.page_start,
                            "chapter_title": chunk.chapter.title,
                            "source_id": source_id,
                            "chunk_index": chunk.chunk_index,
                        },
                    )
                else:
                    await tx.run(
                        """
                        MATCH (ch:Chapter {number: $chapter_number, title: $chapter_title}),
                              (k:Chunk {source_id: $source_id, chunk_index: $chunk_index})
                        MERGE (ch)-[:HAS_CHUNK]->(k)
                        """,
                        {
                            "chapter_number": chunk.chapter.number,
                            "chapter_title": chunk.chapter.title,
                            "source_id": source_id,
                            "chunk_index": chunk.chunk_index,
                        },
                    )

            # 3. Entities.
            if chunk.entities:
                await tx.run(
                    """
                    UNWIND $entities AS e
                    MERGE (n:Entity {id: e.id})
                    SET n.name = e.name,
                        n.type = e.type,
                        n.description = e.description,
                        n.source_page = e.source_page,
                        n.aliases = e.aliases,
                        n.canonical_name = e.canonical_name
                    """,
                    {"entities": [entity.model_dump() for entity in chunk.entities]},
                )

            # 4. Relationships with orphan handling inside the same transaction.
            orphans: list[dict[str, Any]] = []
            if chunk.relationships:
                source_ids = [rel.source_entity_id for rel in chunk.relationships]
                target_ids = [rel.target_entity_id for rel in chunk.relationships]
                result = await tx.run(
                    """
                    WITH $source_ids AS src_ids, $target_ids AS dst_ids
                    UNWIND (src_ids + dst_ids) AS id
                    WITH DISTINCT id
                    OPTIONAL MATCH (n:Entity {id: id})
                    RETURN collect(DISTINCT id) AS requested,
                           collect(DISTINCT n.id) AS found_ids
                    """,
                    {"source_ids": source_ids, "target_ids": target_ids},
                )
                record = await result.single()
                requested: set[str] = set()
                found_ids: set[str] = set()
                if record is not None:
                    requested = set(record["requested"])
                    found_ids = set(record["found_ids"])
                missing_ids = requested - found_ids

                valid_relationships: list[Relationship] = []
                for rel in chunk.relationships:
                    src_missing = rel.source_entity_id in missing_ids
                    dst_missing = rel.target_entity_id in missing_ids
                    if src_missing or dst_missing:
                        missing_endpoint = (
                            "both"
                            if src_missing and dst_missing
                            else ("source" if src_missing else "target")
                        )
                        orphans.append(
                            {
                                "reason": "orphan_endpoint",
                                "type": rel.type,
                                "source_entity_id": rel.source_entity_id,
                                "target_entity_id": rel.target_entity_id,
                                "description": rel.description,
                                "source_page": rel.source_page,
                                "chunk_index": rel.chunk_index,
                                "missing_endpoint": missing_endpoint,
                            }
                        )
                    else:
                        valid_relationships.append(rel)

                if orphans and self._orphan_policy == "fail_loud":
                    raise ValueError(
                        f"Relationship batch aborted: missing endpoints {sorted(missing_ids)}"
                    )

                if valid_relationships:
                    await tx.run(
                        """
                        UNWIND $rels AS r
                        MATCH (src:Entity {id: r.source_entity_id}),
                              (dst:Entity {id: r.target_entity_id})
                        MERGE (src)-[rel:RELATED {type: r.type}]->(dst)
                        SET rel.description = r.description,
                            rel.source_page = r.source_page,
                            rel.chunk_index = r.chunk_index
                        """,
                        {"rels": [rel.model_dump() for rel in valid_relationships]},
                    )

            # 5. Mentions edges.
            await tx.run(
                """
                UNWIND $entity_ids AS eid
                MATCH (k:Chunk {source_id: $source_id, chunk_index: $chunk_index})
                MATCH (e:Entity {id: eid})
                MERGE (k)-[m:MENTIONS]->(e)
                SET m.source_page = coalesce(m.source_page, e.source_page)
                """,
                {
                    "entity_ids": entity_ids,
                    "source_id": source_id,
                    "chunk_index": chunk.chunk_index,
                },
            )

            # 6. Checkpoint row — PROCESSED only after all graph writes succeeded.
            # Version dimensions are stored as flat primitive properties because
            # Neo4j nodes do not accept Map values as properties.
            result = await tx.run(
                """
                MERGE (c:Checkpoint {source_id: $source_id, chunk_index: $chunk_index})
                ON CREATE SET c.created_at = datetime(), c.attempt = $attempt
                ON MATCH SET c.attempt = $attempt
                SET c.status = 'PROCESSED',
                    c.source_version = $source_version,
                    c.pipeline_version = $pipeline_version,
                    c.model_version = $model_version,
                    c.schema_version = $schema_version,
                    c.processed_at = datetime(),
                    c.leased_at = null,
                    c.error_type = null,
                    c.error_message = null,
                    c.updated_at = datetime()
                RETURN c
                """,
                {
                    "source_id": source_id,
                    "chunk_index": chunk.chunk_index,
                    "attempt": attempt,
                    "source_version": versions.source_version,
                    "pipeline_version": versions.pipeline_version,
                    "model_version": versions.model_version,
                    "schema_version": versions.schema_version,
                },
            )
            record = await result.single()
            if record is None:
                raise RuntimeError("commit_chunk_atomic did not return a checkpoint")
            node = record["c"]
            checkpoint = Checkpoint(
                source_id=node["source_id"],
                chunk_index=node["chunk_index"],
                status=CheckpointStatus(node["status"]),
                attempt=node["attempt"],
                versions=VersionDimensions(
                    source_version=node["source_version"],
                    pipeline_version=node["pipeline_version"],
                    model_version=node["model_version"],
                    schema_version=node["schema_version"],
                ),
                leased_at=self._to_datetime(node.get("leased_at")),
                processed_at=self._to_datetime(node.get("processed_at")),
                failed_at=self._to_datetime(node.get("failed_at")),
                error_type=node.get("error_type"),
                error_message=node.get("error_message"),
                updated_at=self._to_datetime(node["updated_at"]) or datetime.now(UTC),
            )
            return checkpoint, orphans

        async with self._driver.session() as session:
            checkpoint, orphans = await session.execute_write(_tx)

        for orphan in orphans:
            await self._dead_letter.write_orphan_relationship(orphan)

        return checkpoint  # type: ignore[no-any-return]

    async def clear_index(self) -> None:
        """Delete every index-created node and edge while preserving :User/:Config.

        Edges are deleted first to avoid leaving orphaned relationships, then
        nodes are detached and removed in a fixed order.
        """
        edge_types = (
            "MENTIONS",
            "RELATED",
            "HAS_SUMMARY",
            "CONTAINS",
            "HAS_SECTION",
            "HAS_SUBSECTION",
            "HAS_CHUNK",
        )
        node_labels = (
            "Chunk",
            "Entity",
            "CommunitySummary",
            "Section",
            "Chapter",
            "Book",
        )
        async with self._driver.session() as session:
            for edge_type in edge_types:
                await session.run(
                    f"""
                    MATCH ()-[r:{edge_type}]->()
                    DELETE r
                    """
                )
            for label in node_labels:
                await session.run(
                    f"""
                    MATCH (n:{label})
                    DETACH DELETE n
                    """
                )

    async def _count_label(self, label: str) -> int:
        """Return the number of nodes with the given label."""
        async with self._driver.session() as session:
            result = await session.run(
                f"""
                MATCH (n:{label})
                RETURN count(n) AS c
                """
            )
            record = await result.single()
            if record is None:
                return 0
            return int(record["c"])

    async def count_chunks(self) -> int:
        """Return the number of :Chunk nodes."""
        return await self._count_label("Chunk")

    async def count_entities(self) -> int:
        """Return the number of :Entity nodes."""
        return await self._count_label("Entity")

    async def count_mentions(self) -> int:
        """Return the number of (:Chunk)-[:MENTIONS]->(:Entity) edges."""
        async with self._driver.session() as session:
            result = await session.run(
                """
                MATCH ()-[r:MENTIONS]->()
                RETURN count(r) AS c
                """
            )
            record = await result.single()
            if record is None:
                return 0
            return int(record["c"])
