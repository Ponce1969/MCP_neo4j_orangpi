"""Pure domain models for resumable indexing checkpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from book_graph_rag.domain.models import PageRef


class CheckpointStatus(StrEnum):
    """Lifecycle states of a chunk checkpoint."""

    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    PROCESSED = "PROCESSED"
    FAILED = "FAILED"
    STALE = "STALE"


class VersionDimensions(BaseModel):
    """The four version dimensions that decide whether a checkpoint is stale."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_version: str = Field(description="sha256(pdf_bytes) first 16 hex chars")
    pipeline_version: str = Field(description="chunker + prompt version")
    model_version: str = Field(description="provider:model:yyyy-mm-dd")
    schema_version: str = Field(description="graph schema revision")


class Checkpoint(BaseModel):
    """A checkpoint row keyed by (source_id, chunk_index)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str = Field(description="corpus:source namespace")
    chunk_index: int = Field(ge=0)
    status: CheckpointStatus
    attempt: int = Field(default=0, ge=0)
    versions: VersionDimensions
    leased_at: datetime | None = None
    processed_at: datetime | None = None
    failed_at: datetime | None = None
    error_type: str | None = None
    error_message: str | None = None
    updated_at: datetime


class LeaseResult(BaseModel):
    """Result of acquiring or refreshing a checkpoint lease."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    checkpoint: Checkpoint
    is_new_lease: bool


class ReplayableChunk(BaseModel):
    """Minimal information needed to re-queue a failed chunk."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str
    chunk_index: int
    versions: VersionDimensions


class FailedChunkRecord(BaseModel):
    """Re-addressable dead-letter record for a failed chunk."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str
    chunk_index: int = Field(ge=0)
    page_ref: PageRef
    source_version: str
    pipeline_version: str
    model_version: str
    schema_version: str
    attempt: int = Field(ge=0)
    checkpoint_status: CheckpointStatus
    error_type: str
    error_message: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


def parse_failed_chunk(record: dict[str, Any]) -> ReplayableChunk:
    """Extract the replayable identity from a failed-chunk dead-letter record."""
    parsed = FailedChunkRecord.model_validate(record)
    return ReplayableChunk(
        source_id=parsed.source_id,
        chunk_index=parsed.chunk_index,
        versions=VersionDimensions(
            source_version=parsed.source_version,
            pipeline_version=parsed.pipeline_version,
            model_version=parsed.model_version,
            schema_version=parsed.schema_version,
        ),
    )
