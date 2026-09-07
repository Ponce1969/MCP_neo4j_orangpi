"""Docs consistency guard: roadmap and spec status must reflect the implemented state."""

from __future__ import annotations

from pathlib import Path


def _read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def test_roadmap_phase3_marked_done() -> None:
    roadmap = _read("docs/spec/roadmap.md")
    assert "Phase 3 status (2026-09-07): **implemented**" in roadmap


def test_spec03_status_is_implemented() -> None:
    spec = _read("docs/spec/03-semantic-entity-resolution.md")
    assert spec.startswith("# 03 — Semantic Entity Resolution")
    assert "Status: Implemented (Phase 3" in spec


def test_spec03_high_band_human_confirm_documented() -> None:
    """The policy clarification (human-confirm for high band) must be documented."""
    spec = _read("docs/spec/03-semantic-entity-resolution.md")
    assert "human-confirm queue" in spec
    assert "Policy clarification (maintainer, 2026-09-06)" in spec


def test_spec03_escalation_note_present() -> None:
    """The F1-beats-baseline escalation must be honestly documented, not hidden."""
    spec = _read("docs/spec/03-semantic-entity-resolution.md")
    assert "Escalation note" in spec
    assert "0.695" in spec  # best hybrid retrieval F1 recorded


def test_spec03_open_decisions_resolved() -> None:
    """All [OPEN] decision markers must be struck through (resolved), not open."""
    spec = _read("docs/spec/03-semantic-entity-resolution.md")
    import re

    unresolved = [
        line
        for line in spec.splitlines()
        if "[OPEN]" in line and not line.strip().startswith("- ~")
    ]
    assert unresolved == [], f"Unresolved [OPEN] decisions: {unresolved}"
    assert re.search(r"RESOLVED|resolved in|~~", spec)


def test_spec03_no_target_stage_markers() -> None:
    spec = _read("docs/spec/03-semantic-entity-resolution.md")
    assert "`[TARGET]`" not in spec
