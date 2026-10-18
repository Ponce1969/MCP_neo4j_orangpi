"""Tests for scripts/run_ragas_evaluation.py flag extensions (Slice B, T-B.9)."""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))

import run_ragas_evaluation as run_ragas_module

from book_graph_rag.config import Settings

_SCRIPT_ERROR_MARKER = "provider said 401 for api-key=secret-marker-xyz"
_SCRIPT_KEY_ID = "evaluation-script-test-v1"
_SCRIPT_SECRET = "independent-script-test-key-that-is-long-enough"


class _ScriptProviderFailureError(Exception):
    """Marker-carrying provider failure for artifact redaction tests."""


def _expected_script_fingerprint(message: str) -> str:
    canonical = json.dumps(
        {"error_message": message},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(_SCRIPT_SECRET.encode("utf-8"), canonical, hashlib.sha256).hexdigest()


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run every case in its own directory.

    The script writes ``evaluation_results.jsonl`` (and, when it compares, ``docs/benchmarks``)
    relative to the working directory. Invoked through ``CliRunner`` from the checkout root, that
    left an artifact behind on every suite run — the escaped file the adversarial verification
    found. Its content is redacted now, but a test must not write into the checkout at all.
    """
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def cli_runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set minimal settings env vars for the script to construct Settings."""
    monkeypatch.setenv("NEO4J_URI", "bolt://localhost:7687")
    monkeypatch.setenv("NEO4J_USER", "neo4j")
    monkeypatch.setenv("NEO4J_PASSWORD", "password")
    monkeypatch.setenv("QUERY_LLM_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("QUERY_LLM_MODEL_NAME", "model")
    monkeypatch.setenv("GRAPH_LLM_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("GRAPH_LLM_MODEL_NAME", "model")


@pytest.fixture
def patched_module(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the dataset loader: the script scores what it receives (E3b).

    The answering adapters this fixture used to patch (``Neo4jQueryAdapter``,
    ``Neo4jCommunityAdapter``, ``LLMAdapter``, ``GlobalQueryUseCase``) are gone:
    the script no longer wires a composition root, because it no longer answers.
    """
    monkeypatch.setattr(
        run_ragas_module, "_load_dataset", lambda path: [{"type": "local", "question": "q?"}]
    )


def test_json_output_flag_writes_file(
    cli_runner: CliRunner,
    env_vars: None,
    patched_module: None,
    tmp_path: Path,
) -> None:
    """--json-output writes the after-metrics JSON to the requested path."""
    output_file = tmp_path / "out.json"
    result = cli_runner.invoke(
        run_ragas_module.main,
        [
            "--no-ragas",
            "--no-baseline",
            "--json-output",
            str(output_file),
            "--after-output",
            str(tmp_path / "after.json"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert output_file.exists()
    parsed = json.loads(output_file.read_text(encoding="utf-8"))
    assert parsed["total_questions"] == 1
    assert parsed["baseline"] is None
    assert parsed["delta"] is None


def test_no_baseline_alias_accepted(
    cli_runner: CliRunner,
    env_vars: None,
    patched_module: None,
    tmp_path: Path,
) -> None:
    """--no-baseline is accepted as an alias for --no-compare."""
    output_file = tmp_path / "out.json"
    result = cli_runner.invoke(
        run_ragas_module.main,
        [
            "--no-ragas",
            "--no-baseline",
            "--json-output",
            str(output_file),
            "--after-output",
            str(tmp_path / "after.json"),
        ],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(output_file.read_text(encoding="utf-8"))
    assert parsed["baseline"] is None
    assert parsed["delta"] is None


def test_existing_flags_unmodified(
    cli_runner: CliRunner,
    env_vars: None,
    patched_module: None,
    tmp_path: Path,
) -> None:
    """The existing --no-ragas and --no-compare flags still work."""
    result = cli_runner.invoke(
        run_ragas_module.main,
        [
            "--no-ragas",
            "--no-compare",
            "--after-output",
            str(tmp_path / "after.json"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Skipping RAGAS" in result.output


def test_after_output_redirect_never_touches_repo_benchmark(
    cli_runner: CliRunner,
    env_vars: None,
    patched_module: None,
    tmp_path: Path,
) -> None:
    """--after-output redirects the default gr3_after.json write (regression)."""
    benchmark = Path("docs/benchmarks/gr3_after.json")
    before = benchmark.read_text(encoding="utf-8") if benchmark.exists() else None
    after_file = tmp_path / "after.json"
    result = cli_runner.invoke(
        run_ragas_module.main,
        [
            "--no-ragas",
            "--no-compare",
            "--after-output",
            str(after_file),
        ],
    )
    assert result.exit_code == 0, result.output
    assert after_file.exists()
    if before is not None:
        assert benchmark.read_text(encoding="utf-8") == before
    else:
        assert not benchmark.exists(), (
            "--after-output recreated the default docs/benchmarks/gr3_after.json file"
        )


@pytest.mark.parametrize("with_hmac_key", [True, False], ids=["keyed", "without-key"])
def test_failed_question_is_redacted_in_all_persisted_script_artifacts(
    cli_runner: CliRunner,
    env_vars: None,
    patched_module: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    with_hmac_key: bool,
) -> None:
    """A marker exception is redacted in JSONL, after-output and json-output artifacts."""
    monkeypatch.chdir(tmp_path)
    if with_hmac_key:
        monkeypatch.setenv("MCP_HMAC_KEY_ID", _SCRIPT_KEY_ID)
        monkeypatch.setenv("MCP_HMAC_KEY", _SCRIPT_SECRET)
    else:
        monkeypatch.delenv("MCP_HMAC_KEY_ID", raising=False)
        monkeypatch.delenv("MCP_HMAC_KEY", raising=False)

    def fail_row(*_: Any, **__: Any) -> dict[str, Any]:
        raise _ScriptProviderFailureError(_SCRIPT_ERROR_MARKER)

    monkeypatch.setattr(run_ragas_module, "_prepare_row", fail_row)
    after_file = tmp_path / "after.json"
    json_file = tmp_path / "result.json"
    result = cli_runner.invoke(
        run_ragas_module.main,
        [
            "--no-ragas",
            "--no-compare",
            "--json-output",
            str(json_file),
            "--after-output",
            str(after_file),
        ],
    )

    assert result.exit_code == 0, result.output
    results_jsonl = (tmp_path / "evaluation_results.jsonl").read_text(encoding="utf-8")
    after_text = after_file.read_text(encoding="utf-8")
    json_text = json_file.read_text(encoding="utf-8")
    persisted = "\n".join((results_jsonl, after_text, json_text))
    fingerprint = _expected_script_fingerprint(_SCRIPT_ERROR_MARKER)

    assert _SCRIPT_ERROR_MARKER not in persisted
    assert "_ScriptProviderFailureError" in persisted
    assert f"message_length={len(_SCRIPT_ERROR_MARKER)}" in persisted
    if with_hmac_key:
        assert f"{_SCRIPT_KEY_ID}:{fingerprint}" in persisted
    else:
        assert "fingerprint=" not in persisted
    assert '"question": "q?"' in after_text


def test_ensure_questions_fails_loudly_on_an_empty_dataset() -> None:
    """An empty dataset must not look like a clean run with null metrics."""
    with pytest.raises(run_ragas_module.click.ClickException):
        run_ragas_module._ensure_questions(0)

    run_ragas_module._ensure_questions(3)


def test_an_all_skipped_dataset_fails_loudly_instead_of_looking_clean(
    env_vars: None,
) -> None:
    """E3b/S9: a total skip must never read as a measurement.

    Rows whose answer or contexts are empty are skipped by the scorer's input
    filter (E3a skip-when-empty); when that filter leaves nothing, the script
    must fail loudly — a click error, exit != 0 — instead of emitting null
    metrics that look like a clean run. The check runs before any ragas import
    or model construction, so an empty dataset fails the same in every
    environment.
    """
    rows: list[dict[str, Any]] = [
        {"type": "local", "question": "q1?", "answer": "", "contexts": []},
        {"type": "global", "question": "q2?", "answer": None, "contexts": ["ctx"]},
    ]

    with pytest.raises(run_ragas_module.click.ClickException):
        run_ragas_module._run_ragas(rows, _settings())


def _settings() -> Settings:
    """Settings built from explicit values, never from the machine's environment.

    ``Settings.model_validate({})`` resolves the required fields from the ambient
    environment and the checkout's ``.env``, so it is valid on a developer machine and
    a ValidationError on a clean one — the same machine dependence the rest of the suite
    was cleaned of. Testing a scoring filter needs no real credentials, only a value.
    """
    return Settings.model_validate(
        {
            "neo4j_uri": "bolt://localhost:7687",
            "neo4j_user": "neo4j",
            "neo4j_password": "fake",  # pragma: allowlist secret
        }
    )


def test_a_whitespace_only_answer_counts_as_nothing_to_score() -> None:
    """Blank is empty here too: Python truthiness would score a string of spaces.

    Mirrors the layer's rule at the script's own filter, so a hand-fed dataset
    cannot smuggle a whitespace-only row into the metric either.
    """
    rows: list[dict[str, Any]] = [
        {"type": "local", "question": "q?", "answer": "   ", "contexts": ["ctx"]},
        {"type": "global", "question": "q2?", "answer": "real", "contexts": [" "]},
    ]

    with pytest.raises(run_ragas_module.click.ClickException):
        run_ragas_module._run_ragas(rows, _settings())


def test_the_scorer_stays_reference_free_even_though_ground_truth_travels() -> None:
    """S6: the reference travels as evidence in the input JSONL, never into the metric.

    ``LLMContextPrecisionWithoutReference`` judges contexts against the question
    alone; feeding a reference column would change what the number measures and
    invalidate the gate's historical 0.80 calibration. The metric wiring can
    only be exercised with live LLM calls, so this pin holds the source
    contract: the reference-free metric is the one instantiated, and no
    ``reference`` column is ever handed to the scored dataset.

    Its limit, stated rather than implied: this is a *wiring* contract read from
    the source, not a behavioural pin, and an alias that kept the class name
    while changing what it wraps would evade it. That is why it also asserts the
    scored dataset's columns — the half closer to behaviour.
    """
    source = Path(run_ragas_module.__file__).read_text(encoding="utf-8")

    assert "_LLMContextPrecisionWithoutReference" in source, (
        "the instantiated metric must be the reference-free one"
    )
    assert "_LLMContextPrecisionWithReference" not in source, (
        "switching to the reference-based metric would change what the number measures"
    )
    assert '"reference"' not in source, (
        "the curated dataset carries no reference; feeding one would change what is measured"
    )
    assert '"retrieved_contexts"' in source, (
        "the scored dataset is built from user_input/response/retrieved_contexts only"
    )
