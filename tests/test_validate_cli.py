"""Tests for the validate CLI command."""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from book_graph_rag.main import cli


def test_validate_fails_closed_on_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*args: object, **kwargs: object) -> None:
        raise ValueError("missing settings")

    monkeypatch.setattr("book_graph_rag.main.Settings.model_validate", _boom)
    runner = CliRunner()
    result = runner.invoke(cli, ["validate", "--book-id", "book-1"])
    assert result.exit_code == 1
    assert "Configuration error" in result.output


def test_validate_rejects_negative_sample_limit() -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli, ["validate", "--book-id", "book-1", "--sample-limit", "-1"]
    )
    assert result.exit_code != 0
