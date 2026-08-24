"""Tests for evaluation-script LLM configuration fail-fast behavior."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

import pytest
from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import deduplicate_dataset  # noqa: E402
import generate_ragas_dataset  # noqa: E402
import run_ragas_evaluation  # noqa: E402


class _MissingLLMSettings:
    """Settings stand-in with valid non-LLM fields and missing provider values."""

    graph_llm_base_url = ""
    graph_llm_model_name = ""
    query_llm_base_url = ""
    query_llm_model_name = ""

    @classmethod
    def model_validate(cls, data: object) -> _MissingLLMSettings:
        return cls()


def _fail_if_client_constructed(**kwargs: object) -> None:
    raise AssertionError("the script constructed a client before validating settings")


@pytest.mark.parametrize(
    ("module", "arguments", "required_names"),
    [
        (deduplicate_dataset, [], ("QUERY_LLM_BASE_URL", "QUERY_LLM_MODEL_NAME")),
        (generate_ragas_dataset, [], ("QUERY_LLM_BASE_URL", "QUERY_LLM_MODEL_NAME")),
        (
            run_ragas_evaluation,
            ["--no-ragas"],
            (
                "GRAPH_LLM_BASE_URL",
                "GRAPH_LLM_MODEL_NAME",
                "QUERY_LLM_BASE_URL",
                "QUERY_LLM_MODEL_NAME",
            ),
        ),
    ],
)
def test_evaluation_scripts_fail_before_network_setup(
    module: ModuleType,
    arguments: list[str],
    required_names: tuple[str, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each evaluation composition root rejects missing role settings first."""
    monkeypatch.setattr(module, "Settings", _MissingLLMSettings)
    if hasattr(module, "AsyncOpenAI"):
        monkeypatch.setattr(module, "AsyncOpenAI", _fail_if_client_constructed)
    if hasattr(module, "LLMAdapter"):
        monkeypatch.setattr(module, "LLMAdapter", _fail_if_client_constructed)

    result = CliRunner().invoke(module.main, arguments)

    assert result.exit_code != 0
    assert "Configuration error:" in result.output
    for name in required_names:
        assert name in result.output
