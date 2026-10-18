"""Score a RAGAS evaluation dataset the evaluated pipeline already answered.

Pure scoring, no fabrication (E3b): the script receives one JSON row per
question — the real benchmark query text, the answer the generation layer
produced in the evaluated pass, and the contexts that fed its synthesis — and
computes Faithfulness, AnswerRelevancy and LLMContextPrecisionWithoutReference
over exactly those payloads. It never answers or re-answers a question: a
number computed here over self-fabricated answers would audit something that
never touched the evaluated pipeline (the phantom dataset E3b closed, whose
recorded ``0.7143`` came from re-answered ids).

Contract of each input row, as scored::

    {"question": <real query text>, "answer": <produced answer>,
     "contexts": [<retrieved texts>], "ground_truth": <reference or "">}

Rules the row handling enforces:

* A row whose ``answer`` or ``contexts`` are empty is skipped (E3a
  skip-when-empty): a missing input is not a failure and must not cry.
* A run in which every row is skipped fails loudly instead of emitting null
  metrics that read like a clean measurement.
* A malformed row is persisted as a redacted error row (R5): loud, never a
  crash, never a fabricated answer.

Produces ``docs/benchmarks/gr3_baseline.json`` with raw metrics and
per-question results. The received rows are preserved in
``evaluation_results.jsonl`` for reproducibility.

Usage::

    uv sync --group dev          # install ragas + langchain-openai once
    uv run python scripts/run_ragas_evaluation.py
    uv run python scripts/run_ragas_evaluation.py --no-ragas  # skip metric calc
"""

from __future__ import annotations

import json
import os
import sys
import warnings
from pathlib import Path
from typing import Any, cast

import click

from book_graph_rag.config import Settings, validate_llm_provider_settings
from book_graph_rag.domain.checkpoint_models import format_redacted_error
from book_graph_rag.infrastructure.llm_adapter import _escape_json_string_control_chars

# ── Constants ──────────────────────────────────────────────────────────────

_DEFAULT_DATASET = "evaluation_dataset.jsonl"
_RESULTS_OUTPUT = "evaluation_results.jsonl"
_BENCHMARK_DIR = Path("docs") / "benchmarks"
_BASELINE_OUTPUT = _BENCHMARK_DIR / "gr3_baseline.json"
_AFTER_OUTPUT = _BENCHMARK_DIR / "gr3_after.json"

# Local embedding model for AnswerRelevancy: DeepSeek offers no embeddings
# API, and a remote embedder would add rate-limit flakiness to a baseline we
# re-run after every change. all-MiniLM-L6-v2 is small, CPU-only, and stable.
_EMBEDDINGS_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


# ── Helpers ────────────────────────────────────────────────────────────────


def _load_dataset(path: str) -> list[dict[str, str]]:
    """Load the input dataset: one JSON row per question, already answered (E3b)."""
    samples: list[dict[str, str]] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                samples.append(json.loads(line))
    return samples


def _load_baseline(path: Path) -> dict[str, Any]:
    """Load the GR.3 baseline metrics from a previous run."""
    if not path.exists():
        raise FileNotFoundError(
            f"Baseline not found: {path}. Run with --no-compare to skip comparison, "
            "or generate a baseline first with scripts/run_ragas_evaluation.py."
        )
    with open(path, encoding="utf-8") as f:
        return cast(dict[str, Any], json.load(f))


def _compute_deltas(current: dict[str, Any], baseline: dict[str, Any]) -> dict[str, float | None]:
    """Compute per-metric deltas between current and baseline scores.

    RAGAS metric values are floats in [0, 1]. Returns None for metrics that
    exist only in one of the two runs.
    """
    current_metrics = current.get("metrics", {}) or {}
    baseline_metrics = baseline.get("metrics", {}) or {}
    deltas: dict[str, float | None] = {}
    for metric_name in current_metrics:
        current_value = current_metrics.get(metric_name)
        baseline_value = baseline_metrics.get(metric_name)
        if isinstance(current_value, (int, float)) and isinstance(baseline_value, (int, float)):
            deltas[metric_name] = round(float(current_value) - float(baseline_value), 4)
        else:
            deltas[metric_name] = None
    return deltas


def _prepare_row(sample: dict[str, Any]) -> dict[str, Any]:
    """Validate one received row and normalize it to the scoring contract.

    The script scores what it receives (E3b) and fabricates nothing: a row must
    carry the real benchmark question text, and its answer/contexts — when
    present — must be a string / list of strings. An EMPTY answer or context
    list is legal here: the skip rule at scoring time owns that case (E3a), not
    this validator. Anything else is a malformed row: ``main`` persists the
    failure as a redacted error row (R5) — loud, never a crash and never a
    fabricated answer.
    """
    question = sample["question"]
    if not isinstance(question, str) or not question.strip():
        raise ValueError("row has no benchmark question text")
    answer = sample.get("answer", "")
    if not isinstance(answer, str):
        raise TypeError(f"answer must be a string, got {type(answer).__name__}")
    contexts = sample.get("contexts", [])
    if not isinstance(contexts, list) or any(not isinstance(c, str) for c in contexts):
        raise TypeError("contexts must be a list of strings")
    return {
        **sample,
        "answer": answer,
        "contexts": list(contexts),
        "context_count": len(contexts),
    }


# ── RAGAS integration ──────────────────────────────────────────────────────


def _serialize_ragas_result(raw_result: Any) -> dict[str, float]:
    """Convert RAGAS EvaluationResult/dict into a JSON-safe metric mapping.

    ragas 0.4+ returns ``EvaluationResult`` objects instead of plain dicts.
    We normalize both shapes so downstream code always sees ``{metric: score}``.
    """
    if hasattr(raw_result, "items"):
        return {str(k): float(v) for k, v in raw_result.items()}
    if hasattr(raw_result, "to_pandas"):
        df = raw_result.to_pandas()
        return {str(col): float(df[col].mean()) for col in df.columns if df[col].dtype.kind in "fc"}
    if hasattr(raw_result, "scores"):
        scores = raw_result.scores
        if scores:
            keys = scores[0].keys()
            return {str(k): float(sum(row[k] for row in scores) / len(scores)) for k in keys}
    raise TypeError(f"Unsupported RAGAS result type: {type(raw_result)}")


def _extract_ragas_rows(raw_result: Any) -> list[dict[str, Any]]:
    """Per-question RAGAS scores, which ``_serialize_ragas_result`` averages away.

    The aggregates cannot tell WHICH question failed, which is what a quality
    diagnosis needs. Values are kept as native floats so the JSON stays portable.
    """
    if hasattr(raw_result, "to_pandas"):
        df = raw_result.to_pandas()
        rows: list[dict[str, Any]] = []
        for index, record in df.iterrows():
            row: dict[str, Any] = {"question_id": str(index)}
            for column in df.columns:
                if df[column].dtype.kind not in "fc":
                    continue
                value = record[column]
                if value is not None:
                    row[str(column)] = float(value)
            rows.append(row)
        return rows
    scores = getattr(raw_result, "scores", None)
    if scores:
        return [
            {"question_id": str(index), **{str(k): float(v) for k, v in row.items()}}
            for index, row in enumerate(scores)
        ]
    return []


def _ensure_questions(total_questions: int) -> None:
    """Fail loudly on an empty dataset: null metrics must not look like a clean run."""
    if total_questions == 0:
        raise click.ClickException(
            "no questions to evaluate: check the dataset path and contents, otherwise the "
            "metrics are null and the run looks clean"
        )


def _carries_something_to_score(row: dict[str, Any]) -> bool:
    """Both halves of the scoring contract must be present and non-blank.

    E3a's skip is about having nothing to score, and a string of spaces is nothing:
    Python truthiness would let it through and the metric would be computed over a
    blank answer. Types are checked here rather than coerced, so a non-string never
    slips in as text (``_prepare_row`` rejects those earlier; this is the guard that
    keeps a hand-fed dataset honest too).
    """
    answer = row.get("answer")
    contexts = row.get("contexts")
    return (
        isinstance(answer, str)
        and bool(answer.strip())
        and isinstance(contexts, list)
        and any(isinstance(context, str) and context.strip() for context in contexts)
    )


def _run_ragas(
    results: list[dict[str, Any]],
    settings: Settings,
) -> tuple[dict[str, float], list[dict[str, Any]]]:
    """Compute Faithfulness, AnswerRelevancy and ContextPrecision over what was received.

    E3b: scoring only — every row already carries the answer and the contexts
    the evaluated pipeline produced; nothing here touches the graph. Rows whose
    answer or contexts are empty or blank are skipped (E3a skip-when-empty), and if
    that leaves nothing the run fails loudly instead of reporting null metrics that
    read like a clean measurement (S9). The check runs before any ragas import
    or model construction, so the loud failure needs no dependency and no
    network.

    ragas 0.4.3 note: ``evaluate()`` only accepts v1 ``Metric`` objects — the
    v2 ``collections`` metrics fail its isinstance gate — so we import the v1
    classes (the same ones behind the deprecated public re-exports, hence the
    targeted warning filter) and pair them with a langchain-compatible local
    embedding model (v1 AnswerRelevancy calls embed_query/embed_documents).

    Requires ``ragas``, ``langchain-openai`` and ``langchain-huggingface`` in
    the dev group.
    """
    metric_rows = [row for row in results if _carries_something_to_score(row)]
    if not metric_rows:
        raise click.ClickException(
            f"nothing to score: {len(results)} row(s) received and none carried both "
            "an answer and contexts; skipping everything must not look like a clean "
            "measurement"
        )
    try:
        from datasets import Dataset
        from langchain_huggingface import HuggingFaceEmbeddings as LangchainHFEmbeddings
        from langchain_openai import ChatOpenAI
        from ragas import evaluate
        from ragas.llms import LangchainLLMWrapper
        from ragas.run_config import RunConfig
    except ImportError as exc:
        click.echo(
            f"RAGAS not available ({exc}).  Run `uv sync --group dev` and "
            "try again, or pass --no-ragas to skip metric computation.",
            err=True,
        )
        sys.exit(1)

    api_key: str = (
        settings.query_llm_api_key.get_secret_value()
        if settings.query_llm_api_key is not None
        else ""
    )

    class _SanitizingChatOpenAI(ChatOpenAI):
        """ChatOpenAI whose outputs escape raw control chars before ragas parses them.

        RAGAS metrics parse the LLM's JSON with strict parsers; the same
        NIM/model defect that broke instructor calls (raw newlines inside JSON
        string values) would break metric scoring. Escaping before ragas sees
        the text makes the metric LLM as robust as the retrieval one.
        """

        def _generate(
            self,
            messages: list[Any],
            stop: list[str] | None = None,
            run_manager: Any = None,
            **kwargs: Any,
        ) -> Any:
            result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
            for generation_list in result.generations:
                for generation in generation_list:
                    # langchain types generations loosely; .text is the payload
                    raw_text = generation.text  # type: ignore[attr-defined]
                    generation.text = _escape_json_string_control_chars(  # type: ignore[attr-defined]
                        raw_text
                    )
            return result

    with warnings.catch_warnings():
        # The public ragas.metrics names are the v1 classes evaluate() accepts;
        # they are re-exported with a deprecation warning we do not need.
        warnings.filterwarnings(
            "ignore",
            message=r"Importing .* from 'ragas\.metrics' is deprecated.*",
        )
        from ragas.metrics import (  # noqa: F811 — re-import under the filter
            AnswerRelevancy as _AnswerRelevancy,
        )
        from ragas.metrics import (
            Faithfulness as _Faithfulness,
        )
        from ragas.metrics import (
            LLMContextPrecisionWithoutReference as _LLMContextPrecisionWithoutReference,
        )

    eval_llm = LangchainLLMWrapper(
        _SanitizingChatOpenAI(
            model=settings.query_llm_model_name,
            base_url=settings.query_llm_base_url,
            api_key=api_key,  # type: ignore[arg-type]
            temperature=0,
            timeout=300.0,
            max_retries=2,
            model_kwargs={"max_tokens": 4096},
        )
    )

    try:
        embeddings = LangchainHFEmbeddings(model_name=_EMBEDDINGS_MODEL)
    except ImportError as exc:
        click.echo(
            f"Local embeddings backend not available ({exc}).  Run "
            "`uv sync --group dev` to install langchain-huggingface and "
            "try again.",
            err=True,
        )
        sys.exit(1)

    dataset = Dataset.from_list(
        [
            {
                "user_input": r["question"],
                "response": r["answer"],
                "retrieved_contexts": r["contexts"],
            }
            for r in metric_rows
        ]
    )

    # ContextPrecision (with reference) demands a ``reference`` ground-truth
    # column, which the curated dataset does not carry. Synthesizing references
    # from the same retrieval would be circular and inflate precision; the
    # without-reference variant judges context relevance against the question
    # alone and is the honest baseline while no human reference set exists.
    metrics = [
        _Faithfulness(llm=eval_llm),
        _AnswerRelevancy(llm=eval_llm, embeddings=embeddings, strictness=1),
        _LLMContextPrecisionWithoutReference(llm=eval_llm),
    ]

    raw_result: Any = evaluate(
        dataset,
        metrics=metrics,
        # Slow-but-successful deepseek responses must not be killed by the
        # executor's default 180s timeout, and 16 parallel n=3 calls would
        # saturate the API from a home connection.
        run_config=RunConfig(timeout=600, max_retries=3, max_wait=90, max_workers=4),
    )
    # ragas 0.4+ returns EvaluationResult/Result objects instead of a plain
    # dict, and metric values may be numpy floats. Convert to a JSON-safe
    # mapping of native floats so callers can serialize the score directly.
    return _serialize_ragas_result(raw_result), _extract_ragas_rows(raw_result)


# ── Main ───────────────────────────────────────────────────────────────────


@click.command()
@click.option(
    "--dataset",
    "-d",
    default=_DEFAULT_DATASET,
    help="Path to the scored input dataset (JSONL, one answered row per question)",
)
@click.option(
    "--detail-level",
    "-l",
    default=1,
    type=click.IntRange(0, 3),
    help="Community summary level for global questions (0-3)",
)
@click.option(
    "--no-ragas",
    is_flag=True,
    help="Skip RAGAS metric computation (only generate result tuples)",
)
@click.option(
    "--no-compare",
    is_flag=True,
    help="Skip baseline comparison and do not emit gr3_after.json deltas",
)
@click.option(
    "--no-baseline",
    is_flag=True,
    help="Alias for --no-compare",
)
@click.option(
    "--json-output",
    type=click.Path(path_type=Path),
    help="Also write the after-metrics JSON to this path",
)
@click.option(
    "--after-output",
    type=click.Path(path_type=Path),
    default=None,
    help="Redirect the default gr3_after.json write (tests/sandboxed runs)",
)
def main(
    dataset: str,
    detail_level: int,
    no_ragas: bool,
    no_compare: bool,
    no_baseline: bool,
    json_output: Path | None,
    after_output: Path | None,
) -> None:
    """Score the received dataset with RAGAS (E3b: pure scoring, no fabrication)."""
    settings = Settings.model_validate({})
    try:
        validate_llm_provider_settings(settings)
    except ValueError as exc:
        raise click.ClickException(f"Configuration error: {exc}") from exc

    click.echo(f"Loading dataset: {dataset}")
    samples = _load_dataset(dataset)
    click.echo(f"  {len(samples)} questions")

    # ── Row preparation (E3b: score what is received; fabricate nothing) ──
    results: list[dict[str, Any]] = []
    total = len(samples)
    for index, sample in enumerate(samples, 1):
        question = str(sample.get("question", ""))
        qtype = str(sample.get("type", "?"))
        click.echo(f"[{index}/{total}] {qtype}: {question[:80]}{'…' if len(question) > 80 else ''}")
        try:
            results.append(_prepare_row(sample))
        except Exception as exc:  # noqa: BLE001 — one bad row must not kill the batch
            # R5: a genuine per-row failure is persisted loud AND redacted — never a
            # crash, never a fabricated answer.
            redacted = format_redacted_error(
                exc,
                key_id=os.getenv("MCP_HMAC_KEY_ID", ""),
                secret=os.getenv("MCP_HMAC_KEY"),
            )
            click.echo(f"  FAILED: {redacted}", err=True)
            results.append(
                {
                    **sample,
                    "answer": None,
                    "contexts": [],
                    "error": redacted,
                }
            )
    _ensure_questions(len(results))

    # Persist raw tuples for reproducibility.
    with open(_RESULTS_OUTPUT, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    click.echo(f"\nRaw results saved: {_RESULTS_OUTPUT}")

    # ── RAGAS metrics ──────────────────────────────────────────────────
    failed_rows = [r for r in results if r.get("error")]

    _BENCHMARK_DIR.mkdir(parents=True, exist_ok=True)
    after: dict[str, Any] = {
        "dataset": dataset,
        "detail_level": detail_level,
        "total_questions": len(results),
        "global_questions": sum(1 for r in results if r.get("type") == "global"),
        "local_questions": sum(1 for r in results if r.get("type") == "local"),
        "failed_questions": [
            {"question": r.get("question", ""), "error": r["error"]} for r in failed_rows
        ],
        "metrics": None,
        "per_question": [],
    }

    if no_ragas:
        click.echo("Skipping RAGAS (--no-ragas).")
    else:
        click.echo("Computing RAGAS metrics…")
        after["metrics"], after["per_question"] = _run_ragas(results, settings)

    no_compare = no_compare or no_baseline
    if no_compare:
        after["baseline"] = None
        after["delta"] = None
    else:
        baseline = _load_baseline(_BASELINE_OUTPUT)
        after["baseline"] = baseline.get("metrics")
        after["delta"] = _compute_deltas(after, baseline)
        click.echo(f"Loaded baseline: {_BASELINE_OUTPUT}")

    after_target = after_output if after_output is not None else _AFTER_OUTPUT
    with open(after_target, "w", encoding="utf-8") as f:
        json.dump(after, f, ensure_ascii=False, indent=2)
    click.echo(f"After-metrics saved: {after_target}")

    if json_output is not None:
        json_output.parent.mkdir(parents=True, exist_ok=True)
        with json_output.open("w", encoding="utf-8") as f:
            json.dump(after, f, ensure_ascii=False, indent=2)
        click.echo(f"JSON output saved: {json_output}")

    if after["metrics"] is None:
        return

    # Print quick summary.
    click.echo("\n── RAGAS After ──")
    for metric_name, value in after["metrics"].items():
        delta = after.get("delta", {}).get(metric_name) if after.get("delta") else None
        delta_str = f" (Δ {delta:+.4f})" if isinstance(delta, float) else ""
        click.echo(f"  {metric_name}: {value}{delta_str}")


if __name__ == "__main__":
    main()
