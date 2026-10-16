"""Tests for the code-provenance helper: a report must name the code that ran.

The gate report used to carry an empty commit, and the generation layer inherited the baseline's
commit instead — which names code that did not run. These tests pin both halves of the contract:
a real checkout answers with its commit, and anything else answers ``unknown`` rather than nothing,
because an empty field reads like a form nobody filled.
"""

from __future__ import annotations

import subprocess
from typing import Any

from book_graph_rag.infrastructure.code_provenance import UNKNOWN_COMMIT, current_code_commit


def test_it_returns_the_checkout_commit() -> None:
    """Inside this repository the helper answers with a real short commit."""
    commit = current_code_commit()

    assert commit != UNKNOWN_COMMIT
    assert commit, "a commit is never empty"
    assert commit == commit.strip(), "and it comes trimmed"
    assert all(character in "0123456789abcdef" for character in commit), commit


def test_a_broken_git_answers_unknown_instead_of_raising(monkeypatch: Any) -> None:
    """Provenance is evidence, never a precondition: a failing git cannot break a run."""

    def explode(*_: Any, **__: Any) -> Any:
        raise subprocess.CalledProcessError(128, "git")

    monkeypatch.setattr(subprocess, "run", explode)

    assert current_code_commit() == UNKNOWN_COMMIT


def test_an_empty_answer_is_unknown_too(monkeypatch: Any) -> None:
    """An empty stdout is the same as no answer: never an empty field in the report."""

    def empty(*_: Any, **__: Any) -> Any:
        return subprocess.CompletedProcess([], 0, stdout="\n", stderr="")

    monkeypatch.setattr(subprocess, "run", empty)

    assert current_code_commit() == UNKNOWN_COMMIT
