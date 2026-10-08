"""Unit tests for the skill quality domain models (skill-quality-gating, Unit 1.1).

Covers the normative requirements of ``openspec/changes/skill-quality-gating/spec.md``:
REQ-SK-01 (node contract), REQ-SK-02 (weighted formula with the 2x safety/executability
default) and REQ-SK-06 (the HIGH-tier safety ceiling).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from book_graph_rag.domain.mcp_security import UnknownToolError
from book_graph_rag.domain.skill_models import (
    Skill,
    SkillQualityScores,
    SkillQualityWeights,
    binds_high_tier_tool,
    effective_quality_score,
    validate_tool_names,
)


def _scores(**overrides: float) -> SkillQualityScores:
    """Five dimensions, all at 0.7 unless overridden."""
    base = {
        "safety": 0.7,
        "executability": 0.7,
        "completeness": 0.7,
        "maintainability": 0.7,
        "cost_awareness": 0.7,
    }
    return SkillQualityScores(**{**base, **overrides})


def _skill(
    skill_id: str,
    scores: SkillQualityScores,
    tool_names: tuple[str, ...] = ("find_entity",),
) -> Skill:
    return Skill(
        id=skill_id,
        name=skill_id,
        version="1.0.0",
        status="active",
        tool_names=tool_names,
        scores=scores,
    )


def test_weights_default_to_the_two_x_safety_and_executability_policy() -> None:
    """REQ-SK-02: the shipped defaults are 2.0 / 2.0 / 1.0 / 1.0 / 1.0."""
    weights = SkillQualityWeights()

    assert (
        weights.safety,
        weights.executability,
        weights.completeness,
        weights.maintainability,
        weights.cost_awareness,
    ) == (2.0, 2.0, 1.0, 1.0, 1.0)


def test_quality_score_is_the_weighted_mean() -> None:
    """REQ-SK-02: ``quality_score = sum(w_i * d_i) / sum(w_i)``, so the range is [0, 1]."""
    everything_perfect = _scores(
        safety=1.0, executability=1.0, completeness=1.0, maintainability=1.0, cost_awareness=1.0
    )
    everything_zero = _scores(
        safety=0.0, executability=0.0, completeness=0.0, maintainability=0.0, cost_awareness=0.0
    )
    mixed = _scores(
        safety=0.8, executability=0.6, completeness=0.5, maintainability=0.4, cost_awareness=0.3
    )

    assert _skill("s:perfect", everything_perfect).quality_score == pytest.approx(1.0)
    assert _skill("s:zero", everything_zero).quality_score == pytest.approx(0.0)
    # (2*0.8 + 2*0.6 + 0.5 + 0.4 + 0.3) / 7
    assert _skill("s:mixed", mixed).quality_score == pytest.approx(4.0 / 7)


def test_safety_outweighs_an_equal_raw_average() -> None:
    """REQ-SK-02: with equal raw averages, the safer skill MUST rank higher."""
    safer = _skill(
        "s:safer",
        _scores(
            safety=0.9, executability=0.5, completeness=0.7, maintainability=0.7, cost_awareness=0.7
        ),
    )
    riskier = _skill(
        "s:riskier",
        _scores(
            safety=0.7, executability=0.5, completeness=0.9, maintainability=0.7, cost_awareness=0.7
        ),
    )

    assert sum(safer.scores.model_dump().values()) == pytest.approx(
        sum(riskier.scores.model_dump().values())
    )
    assert safer.quality_score > riskier.quality_score


def test_scores_are_confined_to_the_unit_interval() -> None:
    """REQ-SK-01: every dimension is a float in [0, 1]; anything else is rejected."""
    for bad in (1.1, -0.1):
        with pytest.raises(ValidationError):
            _scores(safety=bad)


def test_weights_must_be_positive() -> None:
    """A zero or negative weight would make the denominator meaningless."""
    for bad in (0.0, -1.0):
        with pytest.raises(ValidationError):
            SkillQualityWeights(safety=bad)


def test_high_tier_binding_caps_the_effective_safety() -> None:
    """REQ-SK-06: a skill binding ``query_cypher`` is scored with safety capped."""
    perfect = _scores(
        safety=1.0, executability=1.0, completeness=1.0, maintainability=1.0, cost_awareness=1.0
    )
    high_tier = _skill("s:high", perfect, tool_names=("query_cypher",))
    low_tier = _skill("s:low", perfect, tool_names=("find_entity", "count_entities"))

    # (2*0.40 + 2*1 + 1 + 1 + 1) / 7
    assert effective_quality_score(high_tier, safety_cap=0.40) == pytest.approx(5.8 / 7)
    assert effective_quality_score(low_tier, safety_cap=0.40) == pytest.approx(1.0)
    assert high_tier.quality_score == pytest.approx(1.0), "the raw score must stay untouched"


def test_ceiling_is_reported_as_a_binding_and_not_a_lower_raw_score() -> None:
    """The ceiling is a scoring rule, so the binding is detectable on its own."""
    assert binds_high_tier_tool(("query_cypher",)) is True
    assert binds_high_tier_tool(("find_entity", "search_rag")) is False


def test_unknown_tool_is_rejected_fail_closed() -> None:
    """REQ-SK-05: a tool name outside the 8 registered ones is rejected."""
    validate_tool_names(_skill("s:ok", _scores(), tool_names=("find_entity", "query_cypher")))

    with pytest.raises(UnknownToolError):
        validate_tool_names(_skill("s:bad", _scores(), tool_names=("not_a_registered_tool",)))

    with pytest.raises(UnknownToolError):
        binds_high_tier_tool(("not_a_registered_tool",))
