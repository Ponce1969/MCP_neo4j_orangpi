"""Tests for StubRAGASRunner and SubprocessRAGASRunner (Slice B, T-B.7)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from book_graph_rag.domain.evaluation_models import RAGASSecondaryMetrics
from book_graph_rag.infrastructure.stub_ragas_runner import StubRAGASRunner
from book_graph_rag.infrastructure.subprocess_ragas_runner import SubprocessRAGASRunner


@pytest.fixture
def fixture_path() -> Path:
    return Path("tests/fixtures/evaluation/ragas.json")


@pytest.fixture
def runner(fixture_path: Path) -> StubRAGASRunner:
    return StubRAGASRunner(fixture_path)


def test_stub_ragas_runner_unavailable_returns_warning(
    runner: StubRAGASRunner,
) -> None:
    """Fixture available=False yields a warning metric, not an exception."""
    metrics = asyncio.run(
        runner.run(
            dataset_id="unavailable_dataset",
            generation_results=(),
            previous_metrics=None,
        )
    )
    assert metrics.available is False
    assert "import failed" in metrics.notes


def test_stub_ragas_runner_drop_warning_computed(
    runner: StubRAGASRunner,
) -> None:
    """A sharp faithfulness drop vs previous_metrics surfaces drop_warning."""
    previous = RAGASSecondaryMetrics(faithfulness=0.6825, available=True)
    metrics = asyncio.run(
        runner.run(
            dataset_id="drop_dataset",
            generation_results=(),
            previous_metrics=previous,
        )
    )
    assert metrics.drop_warning is True
    assert metrics.faithfulness == 0.55


def test_stub_ragas_runner_no_drop_when_stable(
    runner: StubRAGASRunner,
) -> None:
    """Stable faithfulness does not trigger drop_warning."""
    previous = RAGASSecondaryMetrics(faithfulness=0.6825, available=True)
    metrics = asyncio.run(
        runner.run(
            dataset_id="generation_dataset",
            generation_results=(),
            previous_metrics=previous,
        )
    )
    assert metrics.drop_warning is False
    assert metrics.faithfulness == 0.6825


def test_subprocess_ragas_runner_parses_json_output_file(tmp_path: Path) -> None:
    """SubprocessRAGASRunner parses the script JSON output format."""
    output_file = tmp_path / "ragas_output.json"
    output_file.write_text(
        json.dumps(
            {
                "dataset": "generation_dataset.jsonl",
                "detail_level": 1,
                "total_questions": 2,
                "global_questions": 1,
                "local_questions": 1,
                "failed_questions": [],
                "metrics": {
                    "faithfulness": 0.7,
                    "answer_relevancy": 0.6,
                    "llm_context_precision_without_reference": 0.5,
                },
            }
        ),
        encoding="utf-8",
    )

    runner = SubprocessRAGASRunner(script_path=None, json_output_path=str(output_file))
    metrics = asyncio.run(
        runner.run(
            dataset_id="generation_dataset",
            generation_results=(),
            previous_metrics=None,
        )
    )
    assert metrics.available is True
    assert metrics.faithfulness == pytest.approx(0.7)
    assert metrics.answer_relevancy == pytest.approx(0.6)
    assert metrics.context_precision == pytest.approx(0.5)
    # An older JSON with no per-question rows must stay safe (backwards compatible).
    assert metrics.per_question == ()


def test_subprocess_ragas_runner_parses_per_question_rows(tmp_path: Path) -> None:
    """Per-question RAGAS scores must survive parsing: aggregates hide WHICH question failed."""
    output_file = tmp_path / "ragas_output.json"
    output_file.write_text(
        json.dumps(
            {
                "metrics": {"faithfulness": 0.7},
                "per_question": [
                    {
                        "question_id": "gen-001",
                        "faithfulness": 1.0,
                        "answer_relevancy": 0.9,
                        "llm_context_precision_without_reference": 0.8,
                    },
                    {
                        "question": "gen-002",
                        "faithfulness": 0.4,
                        "answer_relevancy": None,
                        "llm_context_precision_without_reference": 0.2,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    runner = SubprocessRAGASRunner(script_path=None, json_output_path=str(output_file))
    metrics = asyncio.run(
        runner.run(
            dataset_id="generation_dataset",
            generation_results=(),
            previous_metrics=None,
        )
    )

    assert [row.question_id for row in metrics.per_question] == ["gen-001", "gen-002"]
    assert metrics.per_question[0].context_precision == pytest.approx(0.8)
    assert metrics.per_question[0].answer_relevancy == pytest.approx(0.9)
    assert metrics.per_question[1].faithfulness == pytest.approx(0.4)
    assert metrics.per_question[1].answer_relevancy is None


def test_subprocess_ragas_runner_missing_file_returns_available_false(
    tmp_path: Path,
) -> None:
    """A missing JSON output file is treated as RAGAS unavailable (WARNING)."""
    missing = tmp_path / "missing.json"
    runner = SubprocessRAGASRunner(script_path=None, json_output_path=str(missing))
    metrics = asyncio.run(
        runner.run(
            dataset_id="generation_dataset",
            generation_results=(),
            previous_metrics=None,
        )
    )
    assert metrics.available is False


def test_subprocess_ragas_runner_keeps_the_answers_it_sent(tmp_path: Path) -> None:
    """The dataset sent to RAGAS must survive the run: it carries the answer and the
    contexts per question, which is what makes a failing score diagnosable."""
    fake_script = tmp_path / "fake_ragas.py"
    fake_script.write_text(
        "import json, sys\n"
        "out = sys.argv[sys.argv.index('--json-output') + 1]\n"
        "with open(out, 'w', encoding='utf-8') as f:\n"
        "    json.dump({'metrics': {'faithfulness': 1.0}}, f)\n",
        encoding="utf-8",
    )
    answers_dir = tmp_path / "answers"
    runner = SubprocessRAGASRunner(script_path=str(fake_script), answers_dir=str(answers_dir))

    metrics = asyncio.run(
        runner.run(
            dataset_id="generation_dataset",
            generation_results=(
                ("gen-001", "ReAct interleaves reasoning and acting.", ("ctx-a", "ctx-b")),
            ),
            previous_metrics=None,
        )
    )

    assert metrics.available is True
    kept = sorted(answers_dir.glob("*.jsonl"))
    assert len(kept) == 1, "the answers sent to RAGAS must be kept on disk"
    row = json.loads(kept[0].read_text(encoding="utf-8").splitlines()[0])
    assert row["question_id"] == "gen-001"
    assert row["answer"] == "ReAct interleaves reasoning and acting."
    assert row["contexts"] == ["ctx-a", "ctx-b"]
