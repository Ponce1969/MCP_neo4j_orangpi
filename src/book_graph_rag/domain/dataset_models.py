"""Domain models for the labeled entity-resolution dataset."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from book_graph_rag.domain.models import EntityType
from book_graph_rag.domain.resolution_errors import DatasetManifestMismatch


class DatasetLabel(StrEnum):
    SAME = "same"
    DIFFERENT = "different"


class DatasetPairEntity(BaseModel):
    """One side of a labeled pair."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    type: EntityType
    aliases: tuple[str, ...] = ()
    description: str = ""
    namespace: str | None = None


class LabeledPair(BaseModel):
    """One labeled pair/row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = "1.0.0"
    id: str
    label: DatasetLabel
    hard: bool = False
    multilingual: bool = False
    entity_a: DatasetPairEntity
    entity_b: DatasetPairEntity
    note: str = ""


class DatasetManifest(BaseModel):
    """Generated drift-detection artifact for the labeled dataset."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = "1.0.0"
    pairs_file_sha256: str
    pair_count: int
    same_count: int
    different_count: int
    hard_count: int
    multilingual_count: int
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def _counts_consistent(self) -> DatasetManifest:
        if self.pair_count != self.same_count + self.different_count:
            raise ValueError("pair_count must equal same_count + different_count")
        if self.hard_count > self.pair_count:
            raise ValueError("hard_count cannot exceed pair_count")
        if self.multilingual_count > self.pair_count:
            raise ValueError("multilingual_count cannot exceed pair_count")
        return self

    def require_hash_match(self, data: bytes) -> None:
        """Raise DatasetManifestMismatch if data does not match the recorded hash."""
        actual = hashlib.sha256(data).hexdigest()
        if actual != self.pairs_file_sha256:
            raise DatasetManifestMismatch(
                f"pairs.yaml hash mismatch: expected {self.pairs_file_sha256}, got {actual}"
            )
