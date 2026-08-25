"""Read-only retrieval smoke port for deterministic pre-reindex checks."""
from __future__ import annotations

from abc import ABC, abstractmethod

from book_graph_rag.domain.validation_models import SmokeCase, SmokeResult


class RetrievalSmokePort(ABC):
    """Deterministic read-only retrieval boundary; no answer synthesis or evaluators."""

    @abstractmethod
    async def run_case(self, case: SmokeCase) -> SmokeResult:
        """Run one deterministic retrieval case and return its bounded result."""
