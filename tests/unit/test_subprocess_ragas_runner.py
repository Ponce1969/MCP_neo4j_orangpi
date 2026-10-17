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


def _fake_subprocess_run(captured: list[list[str]]) -> Any:
    """Record the argv and write a parseable metrics file where the runner asked for it."""

    def fake_run(args: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        captured.append(list(args))
        output = Path(args[args.index("--json-output") + 1])
        output.write_text(
            json.dumps({"metrics": {"faithfulness": 0.75}, "per_question": []}),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(args, 0, "", "")

    return fake_run


async def test_the_subprocess_cannot_dirty_the_repo(tmp_path: Path, monkeypatch: Any) -> None:
    """Every artifact the script writes goes to a scratch path the runner deletes."""
    captured: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", _fake_subprocess_run(captured))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    runner = SubprocessRAGASRunner(answers_dir=str(tmp_path / "answers"))
    monkeypatch.setattr(
        runner,
        "_write_generation_jsonl",
        lambda _results: tmp_path / "answers.jsonl",
    )

    metrics = await runner.run(
        dataset_id="generation_dataset",
        generation_results=(("gen-001", "an answer", ("a context",)),),
    )

    assert metrics.available, metrics.notes
    assert captured, "the subprocess must have run"
    argv = captured[0]
    assert "--after-output" in argv, (
        "without it the script writes docs/benchmarks/gr3_after.json, the file that breaks "
        "the repo's own guard"
    )
    after_path = Path(argv[argv.index("--after-output") + 1])
    assert after_path != _REPO_ARTIFACT
    assert not after_path.exists(), "the scratch artifact must be deleted, like the metrics json"
    assert not _REPO_ARTIFACT.exists(), "the repo's benchmarks dir must be untouched by a run"


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
        generation_results=(("q1", "answer", ("context",)),),
    )
    serialized = metrics.model_dump_json()

    assert _MARKER not in serialized
    assert "_ProviderFailure" in serialized
    assert f"message_length={len(_MARKER)}" in serialized
    assert _expected_fingerprint(_MARKER) in serialized
