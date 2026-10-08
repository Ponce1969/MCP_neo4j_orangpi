"""Unit tests for the skill quality gate (skill-quality-gating, Unit 1.2 + 1.3).

Covers REQ-SK-03 (``min_quality``), REQ-SK-04 (``top_k`` and deterministic order),
REQ-SK-05 (fail-closed on unknown tools) and REQ-SK-06 (the HIGH-tier ceiling is
applied and reported).
"""

from __future__ import annotations

import pytest

from book_graph_rag.application.quality_gate_use_case import (
    QualityGateResult,
    QualityGateUseCase,
)
from book_graph_rag.domain.mcp_security import UnknownToolError
from book_graph_rag.domain.skill_models import Skill, SkillQualityScores
from book_graph_rag.ports.skill_registry_port import SkillRegistryPort


def _skill_at(
    skill_id: str,
    score: float,
    tool_names: tuple[str, ...] = ("find_entity",),
) -> Skill:
    """A skill whose weighted score equals ``score`` (every dimension set to it)."""
    return Skill(
        id=skill_id,
        name=skill_id,
        version="1.0.0",
        status="active",
        tool_names=tool_names,
        scores=SkillQualityScores(
            safety=score,
            executability=score,
            completeness=score,
            maintainability=score,
            cost_awareness=score,
        ),
    )


class _FakeRegistry(SkillRegistryPort):
    """Deterministic in-memory registry: one snapshot per instance."""

    def __init__(self, skills: tuple[Skill, ...]) -> None:
        self._skills = skills

    async def load_active(self) -> tuple[Skill, ...]:
        return self._skills


async def test_selects_only_skills_at_or_above_min_quality() -> None:
    """REQ-SK-03: the threshold is inclusive; 0.59 is gated out and reported."""
    use_case = QualityGateUseCase(
        _FakeRegistry((_skill_at("s:at", 0.60), _skill_at("s:below", 0.59))),
        safety_cap=0.40,
    )

    result = await use_case.execute(min_quality=0.60, top_k=3)

    assert [skill.id for skill in result.selected] == ["s:at"]
    assert result.gated_out == ("s:below",)
    assert result.min_quality == 0.60
    assert result.top_k == 3


async def test_top_k_caps_the_selection_and_lists_the_rest_as_gated_out() -> None:
    """REQ-SK-04: 5 passing skills with top_k=3 return exactly 3 and gate out the other 2."""
    registry = _FakeRegistry(tuple(_skill_at(f"s:{n}", 0.9 - n / 100) for n in range(5)))
    use_case = QualityGateUseCase(registry, safety_cap=0.40)

    result = await use_case.execute(min_quality=0.60, top_k=3)

    assert [skill.id for skill in result.selected] == ["s:0", "s:1", "s:2"]
    assert result.gated_out == ("s:3", "s:4")


async def test_order_is_deterministic_with_ties_broken_by_id() -> None:
    """REQ-SK-04: quality_score descending, ties broken by id."""
    use_case = QualityGateUseCase(
        _FakeRegistry(
            (
                _skill_at("s:zulu", 0.80),
                _skill_at("s:alpha", 0.80),
                _skill_at("s:bravo", 0.90),
            )
        ),
        safety_cap=0.40,
    )

    result = await use_case.execute(min_quality=0.60, top_k=3)

    assert [skill.id for skill in result.selected] == ["s:bravo", "s:alpha", "s:zulu"]


async def test_empty_registry_is_fail_closed_without_raising() -> None:
    """An empty snapshot selects nothing and is not an error."""
    use_case = QualityGateUseCase(_FakeRegistry(()), safety_cap=0.40)

    result = await use_case.execute(min_quality=0.60, top_k=3)

    assert result == QualityGateResult(selected=(), min_quality=0.60, top_k=3, gated_out=())


async def test_high_tier_skill_is_scored_with_the_ceiling_and_reported() -> None:
    """REQ-SK-06: the ceiling applies to the score and appears in the rationale."""
    high_tier = _skill_at("s:high", 1.0, tool_names=("query_cypher",))
    low_tier = _skill_at("s:low", 0.70)
    use_case = QualityGateUseCase(_FakeRegistry((high_tier, low_tier)), safety_cap=0.40)

    result = await use_case.execute(min_quality=0.60, top_k=3)

    # (2*0.40 + 2*1 + 1 + 1 + 1) / 7 = 0.8286 beats the 0.70 low-tier skill, and it
    # passes the 0.60 threshold: the ceiling is a scoring signal, not a gate.
    assert [skill.id for skill in result.selected] == ["s:high", "s:low"]
    assert any("query_cypher" in line and "0.40" in line for line in result.rationale)


async def test_ceiling_is_reported_when_the_raw_safety_is_already_at_the_cap() -> None:
    """REQ-SK-06 wants the ceiling reported, not only a score it happens to change.

    The seeded ``skill:raw-cypher:v1`` is exactly this shape — its raw safety is 0.40, equal to
    the cap — so a rationale that only fired on score changes would stay empty for the one skill
    whose ceiling actually matters.
    """
    at_cap = Skill(
        id="s:at-cap",
        name="Raw cypher (high tier)",
        version="v1",
        status="active",
        tool_names=("query_cypher",),
        scores=SkillQualityScores(
            safety=0.4,
            executability=1.0,
            completeness=1.0,
            maintainability=1.0,
            cost_awareness=0.9,
        ),
    )
    use_case = QualityGateUseCase(_FakeRegistry((at_cap,)), safety_cap=0.40)

    result = await use_case.execute(min_quality=0.60, top_k=5)

    assert [skill.id for skill in result.selected] == ["s:at-cap"]
    assert any("query_cypher" in line and "0.40" in line for line in result.rationale)


async def test_unknown_tool_in_a_loaded_skill_is_rejected_fail_closed() -> None:
    """REQ-SK-05: the gate never exposes a tool outside the registered eight."""
    use_case = QualityGateUseCase(
        _FakeRegistry((_skill_at("s:bad", 0.90, tool_names=("not_a_registered_tool",)),)),
        safety_cap=0.40,
    )

    with pytest.raises(UnknownToolError):
        await use_case.execute(min_quality=0.60, top_k=3)


async def test_same_snapshot_yields_the_same_selection() -> None:
    """The gate is deterministic: no clock, no randomness, no mutation."""
    skills = tuple(_skill_at(f"s:{n}", 0.70 + n / 100) for n in range(4))
    use_case = QualityGateUseCase(_FakeRegistry(skills), safety_cap=0.40)

    first = await use_case.execute(min_quality=0.60, top_k=2)
    second = await use_case.execute(min_quality=0.60, top_k=2)

    assert first == second
