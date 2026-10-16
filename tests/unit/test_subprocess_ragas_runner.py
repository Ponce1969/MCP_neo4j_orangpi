"""Tests for ``SubprocessRAGASRunner``: the runner must keep the repo clean.

The script it shells out to writes ``docs/benchmarks/gr3_after.json`` by default. That file is
untracked and **not** gitignored, so every gate run left the working tree dirty and broke the
repo's own ``test_gr3_after_removed_or_distinct`` until someone deleted it by hand — three times
in one day, recorded as E1 in ``odd/backlog.md``. The runner owns its scratch files and must
send that artifact somewhere temporary, exactly like the metrics JSON it already deletes.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from book_graph_rag.infrastructure.subprocess_ragas_runner import SubprocessRAGASRunner

_REPO_ARTIFACT = Path("docs/benchmarks/gr3_after.json")


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
    runner = SubprocessRAGASRunner(answers_dir=str(tmp_path / "answers"))

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
