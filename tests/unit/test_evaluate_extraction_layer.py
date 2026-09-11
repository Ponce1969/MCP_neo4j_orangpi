"""Tests for EvaluateExtractionLayerUseCase stub (Slice B, T-B.13)."""

from __future__ import annotations

import asyncio

from book_graph_rag.application.evaluate_extraction_layer_use_case import (
    EvaluateExtractionLayerUseCase,
)
from book_graph_rag.domain.evaluation_models import LayerStatus


def test_extraction_layer_always_pending() -> None:
    """Layer 2 is deferred and always reports PENDING (R6.1)."""
    uc = EvaluateExtractionLayerUseCase()
    result = asyncio.run(uc.execute())
    assert result.status == LayerStatus.PENDING
    assert "deferred" in result.rationale.lower()
    assert result.source_dataset_id == ""
