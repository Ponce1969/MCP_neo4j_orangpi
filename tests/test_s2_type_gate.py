"""Tests for the S2 hard type gate (Slice B)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from book_graph_rag.domain.models import EntityType
from book_graph_rag.domain.s2_type_gate import s2_type_gate

_ENTITY_TYPES = (
    "pattern",
    "agent",
    "component",
    "concept",
    "tool",
    "framework",
    "mcp",
    "llmops",
    "risk",
)


@pytest.mark.parametrize("entity_type", _ENTITY_TYPES)
def test_same_type_passes(entity_type: EntityType) -> None:
    result = s2_type_gate(entity_type, entity_type)
    assert result.passed is True
    assert entity_type in result.reason


def test_different_types_reject() -> None:
    result = s2_type_gate("concept", "agent")
    assert result.passed is False
    assert "concept" in result.reason
    assert "agent" in result.reason


def test_s2_type_gate_result_is_frozen() -> None:
    result = s2_type_gate("concept", "concept")
    with pytest.raises(ValidationError, match="frozen_instance"):
        result.passed = False
