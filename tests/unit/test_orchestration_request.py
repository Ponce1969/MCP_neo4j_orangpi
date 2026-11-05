"""Unit tests for the orchestration request contract (T1, specs S1-S4).

Everything goes through the public API: ``parse_orchestration_request`` for
parsing behavior, and ``OrchestrationRequest``/``Mode``/``Intent`` for the
structural guarantees the parser delegates to the model.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from book_graph_rag.application.orchestration.parse_request import parse_orchestration_request
from book_graph_rag.domain.orchestration_models import Intent, Mode, OrchestrationRequest

_NAMESPACE_A = {"corpus": "knowledge", "source": "agentic-architectural-patterns"}
_NAMESPACE_B = {"corpus": "knowledge", "source": "graphrag-agentic"}

_INTENT_TOKENS: list[str] = ["foundation", "design", "implementation"]
_INTENT_CASES: list[tuple[str, Intent]] = [
    ("foundation", Intent.FOUNDATION),
    ("design", Intent.DESIGN),
    ("implementation", Intent.IMPLEMENTATION),
]


def test_minimal_payload_defaults_to_evidence_mode_without_intent_or_scope() -> None:
    """S1/S2/S3: question-only payload → evidence mode, unspecified intent, all books."""
    request = parse_orchestration_request({"question": "What is a tool router?"})
    assert request.mode is Mode.EVIDENCE
    assert request.intent is None
    assert request.scope is None


def test_synthesis_mode_is_opt_in_and_preserved() -> None:
    """S1: synthesis is chosen explicitly by the caller and kept verbatim."""
    request = parse_orchestration_request({"question": "q", "mode": "synthesis"})
    assert request.mode is Mode.SYNTHESIS


def test_unknown_mode_is_rejected_naming_the_field() -> None:
    with pytest.raises(ValidationError) as excinfo:
        parse_orchestration_request({"question": "q", "mode": "cheap"})
    assert "mode" in str(excinfo.value)


@pytest.mark.parametrize(("token", "member"), _INTENT_CASES)
def test_each_intent_parses(token: str, member: Intent) -> None:
    """S3: the three declared intent values parse into their enum members."""
    request = parse_orchestration_request({"question": "q", "intent": token})
    assert request.intent is member


def test_unknown_intent_is_rejected_naming_the_field() -> None:
    with pytest.raises(ValidationError) as excinfo:
        parse_orchestration_request({"question": "q", "intent": "shipping"})
    assert "intent" in str(excinfo.value)


def test_explicit_none_intent_parses_as_unspecified() -> None:
    request = parse_orchestration_request({"question": "q", "intent": None})
    assert request.intent is None


def test_explicit_scope_with_two_namespaces_is_preserved_in_order() -> None:
    """S2: an explicit caller scope is kept as given, order included."""
    request = parse_orchestration_request({"question": "q", "scope": [_NAMESPACE_A, _NAMESPACE_B]})
    assert request.scope is not None
    assert len(request.scope) == 2
    assert request.scope[0].source == "agentic-architectural-patterns"
    assert request.scope[1].source == "graphrag-agentic"


def test_empty_scope_is_rejected_pointing_at_omitting_scope() -> None:
    """S2: "narrow to nothing" is invalid; the caller must omit scope instead."""
    with pytest.raises(ValueError, match="omit scope"):
        parse_orchestration_request({"question": "q", "scope": []})


def test_duplicate_namespaces_in_scope_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate namespaces"):
        parse_orchestration_request({"question": "q", "scope": [_NAMESPACE_A, _NAMESPACE_A]})


@pytest.mark.parametrize("intent", [*_INTENT_TOKENS, None])
def test_intent_never_changes_scope(intent: str | None) -> None:
    """S3/S4: intent without an explicit scope never narrows anything."""
    request = parse_orchestration_request({"question": "q", "intent": intent})
    assert request.scope is None


def test_single_book_key_is_rejected_by_the_extra_field_guard() -> None:
    """S4: the request has no field that names a single book, and extras are forbidden."""
    assert set(OrchestrationRequest.model_fields) == {"question", "mode", "intent", "scope"}
    with pytest.raises(ValidationError) as excinfo:
        parse_orchestration_request({"question": "x", "book": "graphrag-agentic"})
    assert "book" in str(excinfo.value)


@pytest.mark.parametrize("question", ["", "   ", "\t\n"])
def test_blank_question_is_rejected(question: str) -> None:
    with pytest.raises(ValueError, match="question"):
        parse_orchestration_request({"question": question})


def test_question_with_surrounding_spaces_is_kept_verbatim() -> None:
    """The stored value is never stripped: validation checks, storage preserves."""
    padded = "  how do namespaces work?  "
    request = parse_orchestration_request({"question": padded})
    assert request.question == padded
