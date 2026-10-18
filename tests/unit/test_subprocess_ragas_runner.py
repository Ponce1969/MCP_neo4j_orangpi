"""Tests for ``SubprocessRAGASRunner``: the runner must keep the repo clean.

The script it shells out to writes ``docs/benchmarks/gr3_after.json`` by default. That file is
untracked and **not** gitignored, so every gate run left the working tree dirty and broke the
repo's own ``test_gr3_after_removed_or_distinct`` until someone deleted it by hand — three times
in one day, recorded as E1 in ``odd/backlog.md``. The runner owns its scratch files and must
send that artifact somewhere temporary, exactly like the metrics JSON it already deletes.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

from book_graph_rag.domain.evaluation_models import RagasSample
from book_graph_rag.infrastructure.subprocess_ragas_runner import SubprocessRAGASRunner

_REPO_ARTIFACT = Path("docs/benchmarks/gr3_after.json")
_MARKER = "provider said 401 for api-key=secret-marker-xyz"
_TEST_KEY_ID = "evaluation-test-v1"
_TEST_SECRET = "independent-test-key-that-is-long-enough"


class _ProviderFailureError(Exception):
    """Test exception whose provider message must not be persisted."""


def _expected_fingerprint(message: str) -> str:
    canonical = json.dumps(
        {"error_message": message},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(_TEST_SECRET.encode("utf-8"), canonical, hashlib.sha256).hexdigest()


def _fake_subprocess_run(captured: list[tuple[list[str], dict[str, Any]]]) -> Any:
    """Record argv/environment and emulate the script's cwd-local raw-results artifact."""

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured.append((list(args), dict(kwargs)))
        output = Path(args[args.index("--json-output") + 1])
        output.write_text(
            json.dumps({"metrics": {"faithfulness": 0.75}, "per_question": []}),
            encoding="utf-8",
        )
        cwd = kwargs.get("cwd")
        if cwd is not None:
            (Path(cwd) / "evaluation_results.jsonl").write_text(
                '{"error":"subprocess artifact"}',
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(args, 0, "", "")

    return fake_run


async def test_the_subprocess_cannot_dirty_the_repo(tmp_path: Path, monkeypatch: Any) -> None:
    """Every artifact the script writes goes to a scratch path the runner deletes."""
    captured: list[tuple[list[str], dict[str, Any]]] = []
    monkeypatch.setattr(subprocess, "run", _fake_subprocess_run(captured))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    runner = SubprocessRAGASRunner(
        answers_dir=str(tmp_path / "answers"),
        hmac_key_id=_TEST_KEY_ID,
        hmac_secret=_TEST_SECRET,
    )
    monkeypatch.setattr(
        runner,
        "_write_generation_jsonl",
        lambda _results: tmp_path / "answers.jsonl",
    )

    metrics = await runner.run(
        dataset_id="generation_dataset",
        generation_results=(
            RagasSample(
                question_id="gen-001",
                question="q",
                answer="an answer",
                contexts=("a context",),
            ),
        ),
    )

    assert metrics.available, metrics.notes
    assert captured, "the subprocess must have run"
    argv, kwargs = captured[0]
    assert "--after-output" in argv, (
        "without it the script writes docs/benchmarks/gr3_after.json, the file that breaks "
        "the repo's own guard"
    )
    after_path = Path(argv[argv.index("--after-output") + 1])
    assert after_path != _REPO_ARTIFACT
    assert not after_path.exists(), "the scratch artifact must be deleted, like the metrics json"
    assert not _REPO_ARTIFACT.exists(), "the repo's benchmarks dir must be untouched by a run"

    # evaluation_results.jsonl is fixed by the script and follows cwd, so isolate that cwd in a
    # runner-owned temporary directory instead of deleting a caller-owned file after the run.
    workdir = Path(kwargs["cwd"])
    assert workdir != Path.cwd()
    assert not workdir.exists(), "the runner-owned cwd and its JSONL artifact must be removed"
    child_env = kwargs["env"]
    assert child_env["MCP_HMAC_KEY_ID"] == _TEST_KEY_ID
    assert child_env["MCP_HMAC_KEY"] == _TEST_SECRET
    assert _TEST_SECRET not in argv, "the secret must never be visible in process arguments"


async def test_json_parse_failure_notes_are_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = Path("tests/fixtures/evaluation/ragas.json")
    runner = SubprocessRAGASRunner(
        json_output_path=str(output),
        hmac_key_id=_TEST_KEY_ID,
        hmac_secret=_TEST_SECRET,
    )

    def fail_parse(_path: Path) -> Any:
        raise _ProviderFailureError(_MARKER)

    monkeypatch.setattr(runner, "_parse_output", fail_parse)
    metrics = await runner.run(dataset_id="generation_dataset", generation_results=())
    serialized = metrics.model_dump_json()

    assert _MARKER not in serialized
    assert "_ProviderFailure" in serialized
    assert f"message_length={len(_MARKER)}" in serialized
    assert _expected_fingerprint(_MARKER) in serialized


async def test_the_input_jsonl_carries_the_real_answer_textually(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E3b pin: the intermediate JSONL contains the produced answer verbatim.

    The runner used to write ``"question": question_id`` and received an answer
    field the layer filled with ``""`` — the scorer's input was a phantom, and
    the script re-answered the id from the graph. This pin reads the artifact the
    runner keeps (the dataset the scorer is actually handed) and holds that the
    real query text, the answer text, the contexts and the evidence-only
    ``ground_truth`` all survive textually.
    """
    captured: list[tuple[list[str], dict[str, Any]]] = []
    monkeypatch.setattr(subprocess, "run", _fake_subprocess_run(captured))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    runner = SubprocessRAGASRunner(
        answers_dir=str(tmp_path / "answers"),
        hmac_key_id=_TEST_KEY_ID,
        hmac_secret=_TEST_SECRET,
    )
    answer_text = "ReAct interleaves reasoning and acting over the retrieved summaries."
    ground_truth = "ReAct couples a reasoning trace with tool actions."

    metrics = await runner.run(
        dataset_id="generation_dataset",
        generation_results=(
            RagasSample(
                question_id="gen-001",
                question="What is the ReAct pattern?",
                answer=answer_text,
                contexts=("ctx-a", "ctx-b"),
                ground_truth=ground_truth,
            ),
        ),
    )

    assert metrics.available, metrics.notes
    kept = sorted((tmp_path / "answers").glob("*.jsonl"))
    assert len(kept) == 1, "the dataset sent to RAGAS must be kept on disk"
    raw = kept[0].read_text(encoding="utf-8")
    row = json.loads(raw.splitlines()[0])
    assert answer_text in raw, "the produced answer must survive textually into the input JSONL"
    assert row["answer"] == answer_text
    assert row["question"] == "What is the ReAct pattern?", (
        "the real query text must be scored, never the question id"
    )
    assert row["question_id"] == "gen-001"
    assert row["contexts"] == ["ctx-a", "ctx-b"]
    assert row["ground_truth"] == ground_truth, (
        "the reference travels as evidence for human audit (S6), even though no metric reads it"
    )


async def test_subprocess_failure_notes_are_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    runner = SubprocessRAGASRunner(
        script_path="unused-script.py",
        answers_dir=str(tmp_path / "answers"),
        hmac_key_id=_TEST_KEY_ID,
        hmac_secret=_TEST_SECRET,
    )

    def fail_subprocess(*_: Any, **__: Any) -> Any:
        raise _ProviderFailureError(_MARKER)

    monkeypatch.setattr(subprocess, "run", fail_subprocess)
    monkeypatch.setattr(
        runner,
        "_write_generation_jsonl",
        lambda _results: tmp_path / "answers.jsonl",
    )
    metrics = await runner.run(
        dataset_id="generation_dataset",
        generation_results=(
            RagasSample(question_id="q1", question="q", answer="answer", contexts=("context",)),
        ),
    )
    serialized = metrics.model_dump_json()

    assert _MARKER not in serialized
    assert "_ProviderFailure" in serialized
    assert f"message_length={len(_MARKER)}" in serialized
    assert _expected_fingerprint(_MARKER) in serialized
