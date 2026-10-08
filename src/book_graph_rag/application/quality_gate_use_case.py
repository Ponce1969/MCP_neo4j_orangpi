"""Application use case: select the skills to expose to the LLM (Unit 1.2 + 1.3).

The gate is deterministic and stateless — the same registry snapshot yields the same
selection — and it never mutates the graph or calls the LLM. It sits upstream of the
existing ``ToolRiskTier``/``ResourcePolicy`` enforcement, which stays untouched
(``openspec/changes/skill-quality-gating/design.md`` §5).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from book_graph_rag.domain.skill_models import (
    Skill,
    binds_high_tier_tool,
    effective_quality_score,
    validate_tool_names,
)
from book_graph_rag.ports.skill_registry_port import SkillRegistryPort


class QualityGateResult(BaseModel):
    """Outcome of gating: the selection, the knobs, what was left out, and why."""

    model_config = ConfigDict(frozen=True)

    selected: tuple[Skill, ...]
    min_quality: float
    top_k: int
    gated_out: tuple[str, ...]
    rationale: tuple[str, ...] = ()


class QualityGateUseCase:
    """Select the skills to expose to the LLM (base dependency: ``SkillRegistryPort``).

    ``safety_cap`` is the HIGH-tier ceiling (``skill_safety_high_tier_cap``), injected at
    construction so ``execute`` keeps the signature the design fixes.
    """

    def __init__(self, registry: SkillRegistryPort, *, safety_cap: float) -> None:
        self._registry = registry
        self._safety_cap = safety_cap

    async def execute(self, *, min_quality: float, top_k: int) -> QualityGateResult:
        """Apply the threshold, the ceiling and the cap, reporting every decision."""
        rationale: list[str] = []
        passing: list[tuple[Skill, float]] = []
        gated_out: list[str] = []

        for skill in await self._registry.load_active():
            # REQ-SK-05: fail closed here too, so a skill can never carry an unregistered
            # tool into the model-facing tool list.
            validate_tool_names(skill)
            score = effective_quality_score(skill, safety_cap=self._safety_cap)
            if binds_high_tier_tool(skill.tool_names):
                # REQ-SK-06 asks for the ceiling to be reported, not only for a score it changes:
                # the seeded HIGH-tier skill stores its raw safety already at the cap, and a
                # rationale that stayed empty exactly there would hide the ceiling that matters.
                rationale.append(
                    f"{skill.id}: safety capped at {self._safety_cap:.2f} "
                    f"(HIGH-tier tool in {', '.join(skill.tool_names)})"
                )
            if score >= min_quality:
                passing.append((skill, score))
            else:
                gated_out.append(skill.id)

        # REQ-SK-04: deterministic order, quality_score descending with ties broken by id.
        passing.sort(key=lambda item: (-item[1], item[0].id))
        selected = tuple(skill for skill, _ in passing[:top_k])
        gated_out.extend(skill.id for skill, _ in passing[top_k:])

        return QualityGateResult(
            selected=selected,
            min_quality=min_quality,
            top_k=top_k,
            gated_out=tuple(gated_out),
            rationale=tuple(rationale),
        )
