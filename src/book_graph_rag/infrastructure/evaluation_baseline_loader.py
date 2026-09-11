"""Infrastructure adapter for loading evaluation baseline reports."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from book_graph_rag.domain.evaluation_models import EvaluationBaselineReport
from book_graph_rag.ports.evaluation_baseline_port import EvaluationBaselinePort


class JsonEvaluationBaselineLoader(EvaluationBaselinePort):
    """Load EvaluationBaselineReport JSON files from a directory."""

    _FILE_NAMES: dict[str, str] = {
        "resolution": "resolution_baseline.json",
        "generation": "generation_baseline.json",
        "retrieval": "retrieval_baseline.json",
    }

    def __init__(self, baseline_dir: Path) -> None:
        self._baseline_dir = baseline_dir

    def load(self, layer: Literal["resolution", "generation"]) -> EvaluationBaselineReport | None:
        filename = self._FILE_NAMES.get(layer)
        if filename is None:
            return None
        path = self._baseline_dir / filename
        if not path.exists():
            return None
        text = path.read_text(encoding="utf-8")
        return EvaluationBaselineReport.model_validate_json(text)
