"""Documentation contract tests for docs/spec/06-evaluation-and-readiness.md.

These tests guard the Phase 5 spec surface: resolved-decision markers, no stale
open decisions, and the explicit W1 boundary note.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SPEC_PATH = Path(__file__).parents[2] / "docs" / "spec" / "06-evaluation-and-readiness.md"


@pytest.fixture(scope="session")
def spec_text() -> str:
    if not SPEC_PATH.exists():
        pytest.fail(f"Spec file not found: {SPEC_PATH}")
    return SPEC_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def section_11_text(spec_text: str) -> str:
    """Return the text of section 11 (decisions) and nothing after it."""
    match = re.search(r"^## 11\.\s.*$", spec_text, flags=re.MULTILINE)
    if not match:
        pytest.fail("Section 11 heading not found in spec")
    start = match.start()
    # Section 11 is the last section; stop at next top-level heading ## (without 11.)
    # or end of file.
    next_heading = re.search(r"^##\s+(?!11\.).*$", spec_text[start:], flags=re.MULTILINE)
    end = start + next_heading.start() if next_heading else len(spec_text)
    return spec_text[start:end]


def test_section_11_heading_does_not_claim_open(section_11_text: str) -> None:
    """A resolved section must not be titled 'Open decisions'."""
    heading = section_11_text.splitlines()[0]
    assert "Open decisions" not in heading, (
        "Section 11 heading still claims decisions are open; rename to 'Resolved decisions'"
    )


def test_section_11_contains_resolved_phase5_markers(section_11_text: str) -> None:
    """The three closed Phase 5 decisions must be marked [RESOLVED-PHASE5]."""
    resolved = re.findall(r"\[RESOLVED-PHASE5\]", section_11_text)
    assert len(resolved) >= 3, (
        f"Expected at least 3 [RESOLVED-PHASE5] markers in section 11, found {len(resolved)}"
    )


def test_section_11_has_no_stale_open_marker(section_11_text: str) -> None:
    """No [OPEN] decision marker may remain in section 11."""
    open_markers = re.findall(r"\[OPEN\]", section_11_text)
    assert not open_markers, (
        f"Found {len(open_markers)} stale [OPEN] marker(s) in section 11"
    )


def test_w1_boundary_paragraph_is_present(spec_text: str) -> None:
    """The spec must contain an explicit W1 boundary paragraph with the required claims."""
    required_phrases = (
        "Phase 5 produces graph-context evidence",
        "W1 closure is a separate human decision",
        "readiness gate may pass while W1 remains open",
    )
    for phrase in required_phrases:
        assert phrase in spec_text, f"Required W1 boundary phrase not found: {phrase!r}"
