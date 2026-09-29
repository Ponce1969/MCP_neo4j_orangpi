"""Pure-domain models for namespace routing evaluation.

Routing quality is measured separately from retrieval and generation quality.
The labeled dataset records expected routing behavior, not answer correctness.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class RoutingLabel(BaseModel):
    """One labeled routing expectation from the committed dataset."""

    model_config = ConfigDict(frozen=True)

    question_id: str
    question: str
    language: str
    route_kind: str
    expected_namespaces: tuple[str, ...]
    ambiguity: str = ""
    provenance: str = ""


class EvaluationPrediction(BaseModel):
    """Router output normalized for metric computation."""

    model_config = ConfigDict(frozen=True)

    question_id: str
    validated_namespace: str | None = None
    fanout_namespaces: tuple[str, ...] = ()


class RoutingMetrics(BaseModel):
    """Deterministic routing metrics over the labeled dataset."""

    model_config = ConfigDict(frozen=True)

    total: int
    single_count: int = 0
    multi_count: int = 0
    abstained_count: int = 0
    single_accuracy: float = Field(default=0.0, ge=0.0, le=1.0)
    wrong_namespace_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    abstention_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    multi_containment: float = Field(default=0.0, ge=0.0, le=1.0)
