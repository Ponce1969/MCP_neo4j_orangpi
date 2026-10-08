"""Unit tests for the scoped community runner's namespace handling.

The runner talks to Neo4j and an LLM, so what is testable — and what the `--namespace`
generalization turns on — is the pure part: turning a `corpus:source` argument into the
namespace, its scope and the id prefix, and refusing anything that is not that grammar. Getting
it wrong would silently run a whole book's community pipeline against the wrong entities.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_ROOT = Path(__file__).parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "run_communities_scoped", _ROOT / "scripts-ops/run_communities_scoped.py"
)
assert _SPEC is not None
assert _SPEC.loader is not None
_SCRIPT: ModuleType = importlib.util.module_from_spec(_SPEC)
sys.modules["run_communities_scoped"] = _SCRIPT
_SPEC.loader.exec_module(_SCRIPT)


def test_default_namespace_is_the_book_it_was_written_for() -> None:
    """Without `--namespace` the runner keeps scoping to the GA book, as it always did."""
    namespace, scope, prefix = _SCRIPT._resolve_namespace(_SCRIPT.DEFAULT_NAMESPACE)

    assert namespace.source_id == "knowledge:graphrag-agentic"
    assert scope.source.source_id == "knowledge:graphrag-agentic"
    assert prefix == "knowledge:graphrag-agentic:"


def test_another_book_gets_its_own_scope_and_prefix() -> None:
    """The point of the change: any book can be measured without touching the others."""
    namespace, scope, prefix = _SCRIPT._resolve_namespace("knowledge:ai-engineering-huyen")

    assert namespace.corpus == "knowledge"
    assert namespace.source == "ai-engineering-huyen"
    assert scope.source is namespace
    assert prefix == "knowledge:ai-engineering-huyen:"


@pytest.mark.parametrize(
    "value",
    ["graphrag-agentic", "knowledge:", ":huyen", "knowledge:a:b", ""],
)
def test_a_value_that_is_not_corpus_colon_source_is_refused(value: str) -> None:
    """A malformed namespace must fail loudly: guessing one would scope the run wrongly."""
    with pytest.raises(SystemExit):
        _SCRIPT._resolve_namespace(value)
