"""Port for loading committed evaluation baseline reports by layer."""

from __future__ import annotations

import abc
from typing import Literal

from book_graph_rag.domain.evaluation_models import EvaluationBaselineReport


class EvaluationBaselinePort(abc.ABC):
    """Load an EvaluationBaselineReport by layer (R9.1)."""

    @abc.abstractmethod
    def load(self, layer: Literal["resolution", "generation"]) -> EvaluationBaselineReport | None:
        """Return the committed baseline for ``layer`` or None if absent (R4.3 → INCOMPLETE)."""
