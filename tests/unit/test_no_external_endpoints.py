"""Grep guard: production endpoints must not appear in src/, tests/, scripts/.

T-C.9 (Slice C / R11.1 / R11.3): scans the intended code paths for hard-coded
production host references and real LLM endpoints. The guard intentionally
ignores unrelated documentation and git internals, and permits its own
forbidden-pattern literals via an explicit pragma.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Final

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SCAN_ROOTS: Final = (
    _PROJECT_ROOT / "src",
    _PROJECT_ROOT / "tests",
    _PROJECT_ROOT / "scripts",
)

# Production hosts and real LLM endpoints that must never be referenced from
# scanned code paths. Each literal may be carried in source only when tagged
# with the allow pragma below.
_FORBIDDEN_PATTERNS: Final = (
    "100.106.85.109",  # no-external-endpoints-allow
    "bolt://neo4j.production",  # no-external-endpoints-allow
    "gonzalo@100",  # no-external-endpoints-allow
    "api.openai.com",  # no-external-endpoints-allow
    "api.anthropic.com",  # no-external-endpoints-allow
)

_ALLOW_PRAGMA: Final = "# no-external-endpoints-allow"


def _iter_text_files(root: Path) -> Iterator[Path]:
    for path in root.rglob("*"):
        if not path.is_file() or path.stat().st_size == 0:
            continue
        if path.suffix in {".pyc", ".pyo", ".so", ".dll", ".dylib"}:
            continue
        if "__pycache__" in path.parts:
            continue
        yield path


def _find_forbidden_matches() -> list[str]:
    matches: list[str] = []
    for root in _SCAN_ROOTS:
        for path in _iter_text_files(root):
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if _ALLOW_PRAGMA in line:
                    continue
                for pattern in _FORBIDDEN_PATTERNS:
                    if pattern in line:
                        matches.append(
                            f"{path.relative_to(_PROJECT_ROOT)}:{lineno}: {pattern}"
                        )
    return matches


def test_no_external_endpoints_in_source_tests_or_scripts() -> None:
    """src/, tests/, and scripts/ must not reference production endpoints."""
    matches = _find_forbidden_matches()
    assert not matches, "Found forbidden external endpoint references:\n" + "\n".join(matches)
