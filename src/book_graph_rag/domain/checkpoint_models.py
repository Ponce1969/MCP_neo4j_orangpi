"""Pure domain models for resumable indexing checkpoints."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


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
