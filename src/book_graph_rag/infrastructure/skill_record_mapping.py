"""Map a stored skill record to the domain model, dropping what cannot be trusted.

Both registry readers (the Neo4j adapter and the JSON mirror) go through here, so the
fail-closed policy lives in exactly one place: a record missing a score or a weight, or
carrying an unregistered tool, is ineligible and never reaches the gate (REQ-SK-01,
REQ-SK-05).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from book_graph_rag.domain.mcp_security import UnknownToolError
from book_graph_rag.domain.skill_models import (
    Skill,
    SkillQualityScores,
    SkillQualityWeights,
    validate_tool_names,
)

_DIMENSIONS = ("safety", "executability", "completeness", "maintainability", "cost_awareness")


def skill_from_record(record: Mapping[str, Any]) -> Skill | None:
    """Return the ``Skill`` for ``record``, or ``None`` when it must not be exposed.

    Dropping is deliberate rather than raising: one untrustworthy node must not take the
    whole snapshot down, and an empty snapshot is already the fail-closed answer.
    """
    tool_names = record.get("tool_names")
    if not isinstance(tool_names, (list, tuple)) or not all(
        isinstance(name, str) for name in tool_names
    ):
        return None

    try:
        skill = Skill(
            id=str(record.get("id") or ""),
            name=str(record.get("name") or ""),
            version=str(record.get("version") or ""),
            status=str(record.get("status") or ""),
            tool_names=tuple(tool_names),
            scores=SkillQualityScores.model_validate(
                {name: record.get(name) for name in _DIMENSIONS}
            ),
            weights=SkillQualityWeights.model_validate(
                {name: record.get(f"weight_{name}") for name in _DIMENSIONS}
            ),
        )
    except (ValidationError, TypeError):
        return None

    if not skill.id or skill.status != "active":
        return None
    try:
        validate_tool_names(skill)
    except UnknownToolError:
        return None
    return skill
