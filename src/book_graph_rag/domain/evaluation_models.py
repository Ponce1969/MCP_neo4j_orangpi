"""Domain models for resolution evaluation metrics and baseline report."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class EvaluationMetrics(BaseModel):
    """Metrics produced by a model evaluation run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str
    input_variant: Literal["A", "B"]
    retrieval_precision: float
    retrieval_recall: float
    retrieval_f1: float
    multilingual_under_merge_rate: float
    multilingual_f1: float
    hard_over_merge_rate: float
    auto_merge_count: int
    auto_merge_correct: int
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class BaselineReport(BaseModel):
    """Committed slug+token resolver baseline."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = "1.0.0"
    resolver: Literal["slug_token"] = "slug_token"
    retrieval_precision: float
    retrieval_recall: float
    retrieval_f1: float
    multilingual_under_merge_rate: float
    multilingual_f1: float
    hard_over_merge_rate: float
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    dataset_manifest_sha256: str
