"""RAGAS runner that shells out to scripts/run_ragas_evaluation.py (Slice B, D4)."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from book_graph_rag.domain.evaluation_models import RAGASSecondaryMetrics
from book_graph_rag.ports.ragas_runner_port import RAGASRunnerPort

logger = logging.getLogger(__name__)


class SubprocessRAGASRunner(RAGASRunnerPort):
    """Compute RAGAS metrics by invoking the existing evaluation script.

    On import or execution failure returns ``RAGASSecondaryMetrics(available=False)``
    so the evaluation can continue without RAGAS (R5.4).
    """

    _DROP_THRESHOLD: float = 0.05

    def __init__(
        self,
        script_path: str | None = None,
        *,
        json_output_path: str | None = None,
    ) -> None:
        self._script_path = script_path or "scripts/run_ragas_evaluation.py"
        self._json_output_path = json_output_path

    @staticmethod
    def _map_metric_name(name: str) -> str:
        if name == "llm_context_precision_without_reference":
            return "context_precision"
        return name

    def _parse_output(self, path: Path) -> RAGASSecondaryMetrics:
        text = path.read_text(encoding="utf-8")
        raw = json.loads(text)
        metrics = raw.get("metrics") or {}
        mapped: dict[str, Any] = {
            self._map_metric_name(k): v
            for k, v in metrics.items()
        }
        return RAGASSecondaryMetrics(
            faithfulness=mapped.get("faithfulness"),
            answer_relevancy=mapped.get("answer_relevancy"),
            context_precision=mapped.get("context_precision"),
            available=True,
            notes="subprocess ragas",
        )

    def _write_generation_jsonl(
        self,
        generation_results: tuple[tuple[str, str, tuple[str, ...]], ...],
    ) -> Path:
        tmp = Path(tempfile.mkstemp(suffix=".jsonl")[1])
        rows: list[dict[str, Any]] = []
        for question_id, answer, contexts in generation_results:
            rows.append({
                "question_id": question_id,
                "question": question_id,
                "type": "local",
                "answer": answer,
                "contexts": list(contexts),
            })
        with tmp.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return tmp

    async def run(
        self,
        *,
        dataset_id: str,
        generation_results: tuple[tuple[str, str, tuple[str, ...]], ...],
        previous_metrics: RAGASSecondaryMetrics | None = None,
    ) -> RAGASSecondaryMetrics:
        """Run RAGAS via subprocess or parse a pre-generated JSON output file."""
        if self._json_output_path is not None:
            path = Path(self._json_output_path)
            if path.exists():
                try:
                    metrics = self._parse_output(path)
                except Exception as exc:  # noqa: BLE001
                    return RAGASSecondaryMetrics(
                        available=False,
                        notes=f"failed to parse json output: {exc}",
                    )
                return self._apply_drop_warning(metrics, previous_metrics)
            return RAGASSecondaryMetrics(
                available=False,
                notes=f"json output file not found: {self._json_output_path}",
            )

        jsonl_path = self._write_generation_jsonl(generation_results)
        output_path = Path(tempfile.mkstemp(suffix=".json")[1])
        try:
            subprocess.run(
                [
                    sys.executable,
                    self._script_path,
                    "--dataset",
                    str(jsonl_path),
                    "--no-compare",
                    "--json-output",
                    str(output_path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            metrics = self._parse_output(output_path)
        except Exception as exc:  # noqa: BLE001
            return RAGASSecondaryMetrics(
                available=False,
                notes=f"subprocess ragas failed: {exc}",
            )
        finally:
            jsonl_path.unlink(missing_ok=True)
            output_path.unlink(missing_ok=True)
        return self._apply_drop_warning(metrics, previous_metrics)

    def _apply_drop_warning(
        self,
        metrics: RAGASSecondaryMetrics,
        previous_metrics: RAGASSecondaryMetrics | None,
    ) -> RAGASSecondaryMetrics:
        previous_faithfulness = (
            previous_metrics.faithfulness if previous_metrics else None
        )
        drop_warning = False
        if (
            metrics.faithfulness is not None
            and previous_faithfulness is not None
            and (previous_faithfulness - metrics.faithfulness) > self._DROP_THRESHOLD
        ):
            drop_warning = True
        return metrics.model_copy(
            update={
                "previous_faithfulness": previous_faithfulness,
                "drop_warning": drop_warning,
            }
        )
