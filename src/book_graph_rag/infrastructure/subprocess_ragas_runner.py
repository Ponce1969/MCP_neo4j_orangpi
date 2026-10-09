"""RAGAS runner that shells out to scripts/run_ragas_evaluation.py (Slice B, D4)."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from book_graph_rag.domain.evaluation_models import RAGASQuestionScore, RAGASSecondaryMetrics
from book_graph_rag.ports.ragas_runner_port import RAGASRunnerPort

logger = logging.getLogger(__name__)


def _optional_float(value: Any) -> float | None:
    """Coerce a JSON/numpy value to float, or ``None`` when it is not a number."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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
        answers_dir: str | None = None,
    ) -> None:
        self._script_path = script_path or "scripts/run_ragas_evaluation.py"
        self._json_output_path = json_output_path
        #: Where the dataset sent to RAGAS is kept. It carries the answer and the contexts per
        #: question, which is what makes a failing score diagnosable; the temporary output JSON
        #: is still deleted. Defaults under ``data/`` (gitignored ops artifacts).
        self._answers_dir = (
            Path(answers_dir) if answers_dir is not None else Path("data/evaluation")
        )

    @staticmethod
    def _map_metric_name(name: str) -> str:
        if name == "llm_context_precision_without_reference":
            return "context_precision"
        return name

    def _parse_output(self, path: Path) -> RAGASSecondaryMetrics:
        text = path.read_text(encoding="utf-8")
        raw = json.loads(text)
        metrics = raw.get("metrics") or {}
        mapped: dict[str, Any] = {self._map_metric_name(k): v for k, v in metrics.items()}
        return RAGASSecondaryMetrics(
            faithfulness=mapped.get("faithfulness"),
            answer_relevancy=mapped.get("answer_relevancy"),
            context_precision=mapped.get("context_precision"),
            per_question=self._parse_per_question(raw),
            available=True,
            notes="subprocess ragas",
        )

    def _parse_per_question(self, raw: dict[str, Any]) -> tuple[RAGASQuestionScore, ...]:
        """Per-question rows, optional on purpose: older JSON outputs have none."""
        rows = raw.get("per_question") or []
        parsed: list[RAGASQuestionScore] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            mapped = {self._map_metric_name(str(key)): value for key, value in row.items()}
            question_id = mapped.get("question_id") or mapped.get("question")
            parsed.append(
                RAGASQuestionScore(
                    question_id=str(question_id or ""),
                    faithfulness=_optional_float(mapped.get("faithfulness")),
                    answer_relevancy=_optional_float(mapped.get("answer_relevancy")),
                    context_precision=_optional_float(mapped.get("context_precision")),
                )
            )
        return tuple(parsed)

    def _write_generation_jsonl(
        self,
        generation_results: tuple[tuple[str, str, tuple[str, ...]], ...],
    ) -> Path:
        self._answers_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        answers_path = self._answers_dir / f"ragas_input_{stamp}.jsonl"
        rows: list[dict[str, Any]] = []
        for question_id, answer, contexts in generation_results:
            rows.append(
                {
                    "question_id": question_id,
                    "question": question_id,
                    "type": "local",
                    "answer": answer,
                    "contexts": list(contexts),
                }
            )
        with answers_path.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return answers_path

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
        # mkstemp opens the file and hands the descriptor to the caller: on Windows an open file
        # cannot be unlinked (WinError 32), so close it now that only the path is needed.
        fd, output_name = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        output_path = Path(output_name)
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
            # Only the temporary metrics JSON is removed: the inputs are kept so a failing
            # per-question score can be traced back to the answer and contexts that produced it.
            output_path.unlink(missing_ok=True)
        logger.info("RAGAS inputs kept at %s", jsonl_path)
        return self._apply_drop_warning(metrics, previous_metrics)

    def _apply_drop_warning(
        self,
        metrics: RAGASSecondaryMetrics,
        previous_metrics: RAGASSecondaryMetrics | None,
    ) -> RAGASSecondaryMetrics:
        previous_faithfulness = previous_metrics.faithfulness if previous_metrics else None
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
