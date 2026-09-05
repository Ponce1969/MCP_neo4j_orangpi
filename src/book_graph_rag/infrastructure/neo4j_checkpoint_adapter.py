"""Neo4j implementation of the CheckpointPort contract."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from neo4j import AsyncGraphDatabase

from book_graph_rag.config import Settings
from book_graph_rag.domain.checkpoint_models import (
    Checkpoint,
    CheckpointStatus,
    LeaseResult,
    VersionDimensions,
)
from book_graph_rag.ports.checkpoint_port import CheckpointPort


class Neo4jCheckpointAdapter(CheckpointPort):
    """Async Neo4j adapter for checkpoint lifecycle operations."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._driver: Any = AsyncGraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value()),
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

    @classmethod
    def _node_to_checkpoint(cls, node: Any) -> Checkpoint:
        """Map a Neo4j :Checkpoint node to the domain model."""
        return Checkpoint(
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
            leased_at=cls._to_datetime(node.get("leased_at")),
            processed_at=cls._to_datetime(node.get("processed_at")),
            failed_at=cls._to_datetime(node.get("failed_at")),
            error_type=node.get("error_type"),
            error_message=node.get("error_message"),
            updated_at=cls._to_datetime(node.get("updated_at")) or datetime.now(UTC),
        )

    async def acquire_lease(
        self, source_id: str, chunk_index: int, versions: VersionDimensions
    ) -> LeaseResult:
        """Move a chunk to PROCESSING and record the lease time."""
        async with self._driver.session() as session:
            result: LeaseResult = await session.execute_write(
                self._acquire_lease_tx,
                source_id=source_id,
                chunk_index=chunk_index,
                versions=versions,
            )
        return result

    @staticmethod
    async def _acquire_lease_tx(
        tx: Any,
        *,
        source_id: str,
        chunk_index: int,
        versions: VersionDimensions,
    ) -> LeaseResult:
        result = await tx.run(
            """
            MERGE (c:Checkpoint {source_id: $source_id, chunk_index: $chunk_index})
            ON CREATE SET c.created_at = datetime(),
                          c.status = 'PROCESSING',
                          c.attempt = 0,
                          c.leased_at = datetime(),
                          c.updated_at = datetime()
            ON MATCH SET c.leased_at = datetime(),
                         c.status = CASE
                             WHEN c.status IN ['PENDING', 'STALE', 'FAILED']
                             THEN 'PROCESSING'
                             ELSE c.status
                         END,
                         c.updated_at = datetime()
            SET c.source_version = $source_version,
                c.pipeline_version = $pipeline_version,
                c.model_version = $model_version,
                c.schema_version = $schema_version
            RETURN c
            """,
            {
                "source_id": source_id,
                "chunk_index": chunk_index,
                "source_version": versions.source_version,
                "pipeline_version": versions.pipeline_version,
                "model_version": versions.model_version,
                "schema_version": versions.schema_version,
            },
        )
        record = await result.single()
        if record is None:
            raise RuntimeError("acquire_lease did not return a checkpoint")
        node = record["c"]
        is_new_lease = node.get("created_at") is not None
        return LeaseResult(
            checkpoint=Neo4jCheckpointAdapter._node_to_checkpoint(node),
            is_new_lease=is_new_lease,
        )

    async def release_lease_to_failed(
        self,
        source_id: str,
        chunk_index: int,
        error_type: str,
        error_message: str,
        attempt: int,
    ) -> Checkpoint:
        """Transition a PROCESSING chunk to FAILED and store error metadata."""
        async with self._driver.session() as session:
            result: Checkpoint = await session.execute_write(
                self._release_lease_to_failed_tx,
                source_id=source_id,
                chunk_index=chunk_index,
                error_type=error_type,
                error_message=error_message,
                attempt=attempt,
            )
        return result

    @staticmethod
    async def _release_lease_to_failed_tx(
        tx: Any,
        *,
        source_id: str,
        chunk_index: int,
        error_type: str,
        error_message: str,
        attempt: int,
    ) -> Checkpoint:
        result = await tx.run(
            """
            MATCH (c:Checkpoint {source_id: $source_id, chunk_index: $chunk_index})
            SET c.status = 'FAILED',
                c.attempt = $attempt,
                c.error_type = $error_type,
                c.error_message = $error_message,
                c.failed_at = datetime(),
                c.leased_at = null,
                c.updated_at = datetime()
            RETURN c
            """,
            {
                "source_id": source_id,
                "chunk_index": chunk_index,
                "attempt": attempt,
                "error_type": error_type,
                "error_message": error_message,
            },
        )
        record = await result.single()
        if record is None:
            raise RuntimeError("release_lease_to_failed did not find the checkpoint")
        return Neo4jCheckpointAdapter._node_to_checkpoint(record["c"])

    async def mark_stale_and_reset(
        self, source_id: str, expected_versions: VersionDimensions
    ) -> int:
        """Mark non-PROCESSED checkpoints stale when versions mismatch."""
        async with self._driver.session() as session:
            result: int = await session.execute_write(
                self._mark_stale_and_reset_tx,
                source_id=source_id,
                expected_versions=expected_versions,
            )
        return result

    @staticmethod
    async def _mark_stale_and_reset_tx(
        tx: Any,
        *,
        source_id: str,
        expected_versions: VersionDimensions,
    ) -> int:
        result = await tx.run(
            """
            MATCH (c:Checkpoint {source_id: $source_id})
            WHERE c.status <> 'PROCESSED'
              AND (
                  c.source_version <> $source_version
                  OR c.pipeline_version <> $pipeline_version
                  OR c.model_version <> $model_version
                  OR c.schema_version <> $schema_version
              )
            SET c.status = 'STALE', c.updated_at = datetime()
            RETURN count(c) AS changed
            """,
            {
                "source_id": source_id,
                "source_version": expected_versions.source_version,
                "pipeline_version": expected_versions.pipeline_version,
                "model_version": expected_versions.model_version,
                "schema_version": expected_versions.schema_version,
            },
        )
        record = await result.single()
        if record is None:
            return 0
        return int(record["changed"])

    async def fetch_state(
        self, source_id: str, chunk_indices: list[int]
    ) -> dict[int, Checkpoint]:
        """Return existing checkpoints keyed by chunk_index."""
        if not chunk_indices:
            return {}
        async with self._driver.session() as session:
            result = await session.run(
                """
                MATCH (c:Checkpoint {source_id: $source_id})
                WHERE c.chunk_index IN $chunk_indices
                RETURN c
                """,
                {"source_id": source_id, "chunk_indices": chunk_indices},
            )
            checkpoints: dict[int, Checkpoint] = {}
            async for record in result:
                checkpoint = self._node_to_checkpoint(record["c"])
                checkpoints[checkpoint.chunk_index] = checkpoint
            return checkpoints

    async def reclaim_stale_leases(
        self, source_id: str, now: datetime, stale_seconds: int
    ) -> int:
        """Reset PROCESSING leases older than stale_seconds."""
        threshold = now - timedelta(seconds=stale_seconds)
        async with self._driver.session() as session:
            result = await session.run(
                """
                MATCH (c:Checkpoint {source_id: $source_id})
                WHERE c.status = 'PROCESSING'
                  AND (c.leased_at IS NULL OR c.leased_at < $threshold)
                SET c.status = 'PENDING',
                    c.leased_at = null,
                    c.updated_at = datetime()
                RETURN count(c) AS changed
                """,
                {"source_id": source_id, "threshold": threshold},
            )
            record = await result.single()
            if record is None:
                return 0
            return int(record["changed"])

    async def backfill_processed(
        self, source_id: str, chunk_indices: list[int], versions: VersionDimensions
    ) -> list[Checkpoint]:
        """Create PROCESSED checkpoints for legacy chunks without graph writes."""
        async with self._driver.session() as session:
            result: list[Checkpoint] = await session.execute_write(
                self._backfill_processed_tx,
                source_id=source_id,
                chunk_indices=chunk_indices,
                versions=versions,
            )
        return result

    @staticmethod
    async def _backfill_processed_tx(
        tx: Any,
        *,
        source_id: str,
        chunk_indices: list[int],
        versions: VersionDimensions,
    ) -> list[Checkpoint]:
        result = await tx.run(
            """
            UNWIND $chunk_indices AS chunk_index
            MERGE (c:Checkpoint {source_id: $source_id, chunk_index: chunk_index})
            ON CREATE SET c.created_at = datetime(), c.attempt = 1
            ON MATCH SET c.attempt = coalesce(c.attempt, 1)
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
                "chunk_indices": chunk_indices,
                "source_version": versions.source_version,
                "pipeline_version": versions.pipeline_version,
                "model_version": versions.model_version,
                "schema_version": versions.schema_version,
            },
        )
        checkpoints: list[Checkpoint] = []
        async for record in result:
            checkpoints.append(Neo4jCheckpointAdapter._node_to_checkpoint(record["c"]))
        return checkpoints
