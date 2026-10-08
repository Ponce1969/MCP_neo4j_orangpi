"""Domain models for quality-gated skill retrieval (skill-quality-gating, Unit 1.1).

Pure domain: stdlib + Pydantic only, no ports and no infrastructure. The five quality
dimensions and their weighted score are deterministic by construction — the seeding
script computes them, the retrieval path only reads them
(``openspec/changes/skill-quality-gating/design.md`` §1-§2).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from book_graph_rag.domain.mcp_security import ToolRiskTier
from book_graph_rag.domain.tool_tier_registry import tier_for


class SkillQualityScores(BaseModel):
    """Five deterministic quality dimensions, each in [0, 1]."""

    model_config = ConfigDict(frozen=True, strict=True)

    safety: float = Field(ge=0.0, le=1.0)
    executability: float = Field(ge=0.0, le=1.0)
    completeness: float = Field(ge=0.0, le=1.0)
    maintainability: float = Field(ge=0.0, le=1.0)
    cost_awareness: float = Field(ge=0.0, le=1.0)


class SkillQualityWeights(BaseModel):
    """Per-dimension weights; the defaults encode the 2x safety/executability policy."""

    model_config = ConfigDict(frozen=True, strict=True)

    safety: float = Field(default=2.0, gt=0.0)
    executability: float = Field(default=2.0, gt=0.0)
    completeness: float = Field(default=1.0, gt=0.0)
    maintainability: float = Field(default=1.0, gt=0.0)
    cost_awareness: float = Field(default=1.0, gt=0.0)


def _weighted_mean(scores: SkillQualityScores, weights: SkillQualityWeights) -> float:
    """``sum(w_i * d_i) / sum(w_i)`` (REQ-SK-02)."""
    total = (
        weights.safety
        + weights.executability
        + weights.completeness
        + weights.maintainability
        + weights.cost_awareness
    )
    weighted = (
        weights.safety * scores.safety
        + weights.executability * scores.executability
        + weights.completeness * scores.completeness
        + weights.maintainability * scores.maintainability
        + weights.cost_awareness * scores.cost_awareness
    )
    return weighted / total


class Skill(BaseModel):
    """A gated unit of capability binding one or more MCP tools."""

    model_config = ConfigDict(frozen=True, strict=True)

    id: str
    name: str
    version: str
    status: str
    tool_names: tuple[str, ...]
    scores: SkillQualityScores
    weights: SkillQualityWeights = SkillQualityWeights()

    @property
    def quality_score(self) -> float:
        """Weighted normalized mean over the five dimensions (REQ-SK-02)."""
        return _weighted_mean(self.scores, self.weights)


def validate_tool_names(skill: Skill) -> None:
    """Fail closed when a skill binds a tool outside the eight registered ones (REQ-SK-05).

    ``tier_for`` raises ``UnknownToolError`` for an unregistered name, which is the same
    boundary ``McpServerAdapter`` enforces, so an unknown tool can never inherit a
    default policy by travelling inside a skill.
    """
    for tool_name in skill.tool_names:
        tier_for(tool_name)


def binds_high_tier_tool(tool_names: tuple[str, ...]) -> bool:
    """True when any bound tool is HIGH tier (``query_cypher``); fails closed on unknown."""
    return any(tier_for(name) is ToolRiskTier.HIGH for name in tool_names)


def effective_quality_score(skill: Skill, *, safety_cap: float) -> float:
    """``quality_score`` with REQ-SK-06's HIGH-tier safety ceiling applied.

    A skill binding a HIGH-tier tool has its effective ``safety`` capped at ``safety_cap``
    whatever the raw score says, and the node's raw scores stay untouched: the ceiling is a
    scoring rule the gate reports, not a mutation of the stored quality.
    """
    if not binds_high_tier_tool(skill.tool_names):
        return skill.quality_score
    capped = skill.scores.model_copy(update={"safety": min(skill.scores.safety, safety_cap)})
    return _weighted_mean(capped, skill.weights)
