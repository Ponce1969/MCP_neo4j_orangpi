"""RAGAS runner that shells out to scripts/run_ragas_evaluation.py (Slice B, D4)."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from book_graph_rag.config import Settings
from book_graph_rag.domain.checkpoint_models import format_redacted_error
from book_graph_rag.domain.evaluation_models import (
    RAGASQuestionScore,
    RagasSample,
    RAGASSecondaryMetrics,
)
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
        settings: Settings | None = None,
        json_output_path: str | None = None,
        answers_dir: str | None = None,
        hmac_key_id: str = "",
        hmac_secret: str = "",
    ) -> None:
        self._script_path = script_path or "scripts/run_ragas_evaluation.py"
        self._json_output_path = json_output_path
        self._settings = settings
        self._hmac_key_id = hmac_key_id
        self._hmac_secret = hmac_secret
        #: Where the dataset sent to RAGAS is kept. It carries the answer and the contexts per
        #: question, which is what makes a failing score diagnosable; the temporary output JSON
        #: is still deleted. Defaults under ``data/`` (gitignored ops artifacts).
        self._answers_dir = (
            Path(answers_dir) if answers_dir is not None else Path("data/evaluation")
        )

    def _child_environment(self) -> dict[str, str]:
        """The environment the child script runs with: this process's, plus the resolved settings.

        ``scripts/run_ragas_evaluation.py`` builds its own ``Settings``, and pydantic-settings
        resolves a dotenv **relative to the working directory** while this runner deliberately
        runs the script in a temporary one. A child left to resolve the configuration itself
        therefore dies on the required Neo4j fields: the 438-byte redacted note of the
        2026-10-18 run was exactly that ``ValidationError``, and it is what kept the secondary
        metric unmeasurable through this path ("RAGAS did not measure", spec 07).

        The child inherits **values**, derived from the settings model's own fields so a setting
        added later cannot be forgotten, and never the path to a file: the parent has already
        resolved the configuration, and a path would hand the child a second source of truth to
        re-read in a directory that exists to be disposable. The explicit HMAC arguments are
        applied last so they stay authoritative over any copy the settings carry.
        """
        env = os.environ.copy()
        settings = self._settings
        if settings is not None:
            for name in type(settings).model_fields:
                value = getattr(settings, name)
                if value is None:
                    continue
                if isinstance(value, SecretStr):
                    rendered = value.get_secret_value()
                elif isinstance(value, dict):
                    rendered = json.dumps(value)
                elif isinstance(value, (list, tuple, set)):
                    rendered = json.dumps(list(value))
                else:
                    rendered = str(value)
                env[name.upper()] = rendered
        # Environment avoids exposing the HMAC secret in argv, which is visible in process lists.
        env["MCP_HMAC_KEY_ID"] = self._hmac_key_id
        env["MCP_HMAC_KEY"] = self._hmac_secret
        return env

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
        generation_results: Sequence[RagasSample],
    ) -> Path:
        self._answers_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        answers_path = self._answers_dir / f"ragas_input_{stamp}.jsonl"
        rows: list[dict[str, Any]] = []
        for sample in generation_results:
            rows.append(
                {
                    "question_id": sample.question_id,
                    # The real query text, never the id: the script scores what it
                    # receives (E3b), so a question written as an id would silently
                    # become a slug the metric cannot judge.
                    "question": sample.question,
                    "type": "local",
                    "answer": sample.answer,
                    "contexts": list(sample.contexts),
                    # Evidence for human traceability and auditing (S6): the scorer
                    # never reads a reference — the metric is reference-free by design.
                    "ground_truth": sample.ground_truth,
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
        generation_results: Sequence[RagasSample],
        previous_metrics: RAGASSecondaryMetrics | None = None,
    ) -> RAGASSecondaryMetrics:
        """Run RAGAS via subprocess or parse a pre-generated JSON output file."""
        if self._json_output_path is not None:
            path = Path(self._json_output_path)
            if path.exists():
                try:
                    metrics = self._parse_output(path)
                except Exception as exc:  # noqa: BLE001
                    redacted = format_redacted_error(
                        exc,
                        key_id=self._hmac_key_id,
                        secret=self._hmac_secret,
                    )
                    return RAGASSecondaryMetrics(
                        available=False,
                        notes=f"failed to parse json output: {redacted}",
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
        # The script's own default for --after-output is docs/benchmarks/gr3_after.json: an
        # untracked artifact that is NOT gitignored, so leaving it to the script made every gate
        # run dirty the repo and break test_gr3_after_removed_or_distinct until someone deleted
        # the file by hand (odd/backlog.md, E1). It is scratch, so it gets a scratch path.
        fd, after_name = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        after_path = Path(after_name)
        script_path = Path(self._script_path)
        if not script_path.is_absolute():
            script_path = Path.cwd() / script_path
        dataset_path = jsonl_path.resolve()
        # The child cannot resolve its own configuration: see _child_environment.
        child_env = self._child_environment()
        try:
            with tempfile.TemporaryDirectory(prefix="ragas-subprocess-") as working_dir:
                subprocess.run(
                    [
                        sys.executable,
                        str(script_path),
                        "--dataset",
                        str(dataset_path),
                        "--no-compare",
                        "--json-output",
                        str(output_path),
                        "--after-output",
                        str(after_path),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    cwd=working_dir,
                    env=child_env,
                )
                metrics = self._parse_output(output_path)
        except Exception as exc:  # noqa: BLE001
            redacted = format_redacted_error(
                exc,
                key_id=self._hmac_key_id,
                secret=self._hmac_secret,
            )
            return RAGASSecondaryMetrics(
                available=False,
                notes=f"subprocess ragas failed: {redacted}",
            )
        finally:
            # Both scratch artifacts are removed: the inputs are kept so a failing per-question
            # score can be traced back to the answer and contexts that produced it.
            output_path.unlink(missing_ok=True)
            after_path.unlink(missing_ok=True)
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
