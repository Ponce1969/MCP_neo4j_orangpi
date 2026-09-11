"""Stub pairwise judge for deterministic tests (Slice B, D8)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from book_graph_rag.domain.evaluation_models import PairwiseJudgment
from book_graph_rag.ports.pairwise_judge_port import PairwiseJudgePort


class StubPairwiseJudge(PairwiseJudgePort):
    """Reads pairwise verdicts from a JSON fixture keyed by ``question_id``."""

    def __init__(self, fixture_path: Path) -> None:
        self._fixture_path = fixture_path
        self._data: dict[str, dict[str, Any]] = {}
        if fixture_path.exists():
            self._data = json.loads(fixture_path.read_text(encoding="utf-8"))

    async def compare(
        self,
        *,
        question_id: str,
        question: str,
        graph_answer: str,
        baseline_answer: str,
        contexts: tuple[str, ...],
        judge_model_id: str,
    ) -> PairwiseJudgment:
        """Return the fixture verdict or a deterministic tie for unknown ids."""
        entry = self._data.get(question_id)
        if entry is None:
            return PairwiseJudgment(
                question_id=question_id,
                verdict="tie",
                rationale="no fixture entry for this question",
                judge_model_id=judge_model_id,
            )
        return PairwiseJudgment(
            question_id=question_id,
            verdict=entry.get("verdict", "tie"),
            rationale=entry.get("rationale", ""),
            judge_model_id=entry.get("judge_model_id", judge_model_id),
        )
