"""Read-only port for pre-reindex graph validation evidence."""
from __future__ import annotations

from abc import ABC, abstractmethod

from book_graph_rag.domain.validation_models import CoverageEvidence, RuleEvidence


class GraphValidationReadPort(ABC):
    """Read-only graph validation boundary; no writes, mutations, or raw Cypher."""

    @abstractmethod
    async def collect_rule_evidence(
        self, book_id: str, sample_limit: int
    ) -> tuple[RuleEvidence, ...]:
        """Collect bounded, allowlisted audit rule evidence for the given book."""

    @abstractmethod
    async def collect_coverage(self, book_id: str) -> tuple[CoverageEvidence, ...]:
        """Collect provenance coverage evidence for the given book."""
