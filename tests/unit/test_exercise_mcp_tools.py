"""Unit tests for the read-only MCP tool exerciser (skill-quality-gating, Unit 4.2/4.3).

The script is mostly I/O against a live server, so what is testable — and what matters — is
the helper that finds a traversal seed in a tool payload. The end-to-end evidence it produces
is recorded in ``data/evaluation/skill_gate_evidence.json``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

_ROOT = Path(__file__).parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "exercise_mcp_tools", _ROOT / "scripts-ops/exercise_mcp_tools.py"
)
assert _SPEC is not None
assert _SPEC.loader is not None
_EXERCISE: ModuleType = importlib.util.module_from_spec(_SPEC)
sys.modules["exercise_mcp_tools"] = _EXERCISE
_SPEC.loader.exec_module(_EXERCISE)


def test_first_entity_id_finds_a_namespaced_id() -> None:
    """The traversal seed comes out of whatever shape the tool returns."""
    payload = (
        '{\n  "entities": [\n    {\n      "entity": {\n'
        '        "id": "knowledge:ai-engineering-huyen:rag-pattern",\n'
        '        "name": "RAG pattern"\n      }\n    }\n  ]\n}'
    )

    assert _EXERCISE._first_entity_id(payload) == "knowledge:ai-engineering-huyen:rag-pattern"


def test_first_entity_id_returns_none_without_one() -> None:
    """No id means the script reports the traversal as failed instead of guessing a seed."""
    assert _EXERCISE._first_entity_id('{"answer": "no entities here"}') is None
    assert _EXERCISE._first_entity_id("") is None
