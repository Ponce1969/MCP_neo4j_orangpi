"""Documentation contract tests for README.md and AGENTS.md Phase 5 updates."""

from __future__ import annotations

from pathlib import Path

import pytest

README_PATH = Path(__file__).parents[2] / "README.md"
AGENTS_PATH = Path(__file__).parents[2] / "AGENTS.md"


@pytest.fixture(scope="session")
def readme_text() -> str:
    if not README_PATH.exists():
        pytest.fail(f"README not found: {README_PATH}")
    return README_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def agents_text() -> str:
    if not AGENTS_PATH.exists():
        pytest.fail(f"AGENTS.md not found: {AGENTS_PATH}")
    return AGENTS_PATH.read_text(encoding="utf-8")


def test_readme_phase_5_row_links_to_spec_and_evaluation_readme(readme_text: str) -> None:
    """The Phase 5 row must link to the spec and to the evaluation data README."""
    assert "docs/spec/06-evaluation-and-readiness.md" in readme_text, (
        "README missing link to docs/spec/06-evaluation-and-readiness.md"
    )
    assert "data/evaluation/README.md" in readme_text, (
        "README missing link to data/evaluation/README.md"
    )


def test_readme_includes_evaluate_resolution_example(readme_text: str) -> None:
    """README must show the evaluate --layer resolution CLI example."""
    assert "book-graph-rag evaluate --layer resolution" in readme_text, (
        "README missing evaluate --layer resolution example"
    )


def test_readme_includes_gate_expose_mcp_readiness_example(readme_text: str) -> None:
    """README must show a readiness gate example for expose-mcp-readiness."""
    assert "book-graph-rag gate" in readme_text, "README missing gate command example"
    assert "expose-mcp-readiness" in readme_text, "README missing expose-mcp-readiness gate example"


def test_agents_md_declares_phase_5_read_only(agents_text: str) -> None:
    """AGENTS.md §7 must state that Phase 5 evaluators/readiness are read-only."""
    assert "Phase 5" in agents_text, "AGENTS.md missing Phase 5 section"
    assert "read-only" in agents_text or "solo lectura" in agents_text, (
        "AGENTS.md missing read-only safety note for Phase 5"
    )
    assert "evaluate" in agents_text.lower(), "AGENTS.md missing evaluate mention"
    assert "gate" in agents_text.lower(), "AGENTS.md missing gate mention"


def test_agents_md_phase_5_orangepi_uv_setup(agents_text: str) -> None:
    """AGENTS.md must document the OrangePi uv path and community extra."""
    assert "~/.local/bin/uv" in agents_text, "AGENTS.md missing OrangePi uv path (~/.local/bin/uv)"
    assert "uv sync --extra community" in agents_text, (
        "AGENTS.md missing uv sync --extra community instruction"
    )


def test_agents_md_phase_5_thresholds_mechanism_first(agents_text: str) -> None:
    """AGENTS.md must document the mechanism-first threshold policy."""
    assert "thresholds_finalized" in agents_text, "AGENTS.md missing thresholds_finalized mention"
    assert "mechanism-first" in agents_text or "mecanismo primero" in agents_text, (
        "AGENTS.md missing mechanism-first threshold note"
    )
