"""Read the active skills from Neo4j (skill-quality-gating, Unit 2.2).

Read-only by construction: one ``MATCH`` over ``:Skill`` and nothing else. The threshold
and ``top_k`` decision stays in the gate, so the rule lives in exactly one place and the
adapter can never disagree with it.
"""

from __future__ import annotations

from typing import Any

from book_graph_rag.domain.skill_models import Skill
from book_graph_rag.infrastructure.skill_record_mapping import skill_from_record
from book_graph_rag.ports.skill_registry_port import SkillRegistryPort

#: Read-only snapshot of the active skills. The weighted score is deliberately NOT computed
#: here: the domain recomputes it from the dimensions, so a stale materialized value can
#: never decide what the model is allowed to see.
_LOAD_ACTIVE_SKILLS = """
MATCH (s:Skill)
WHERE s.status = 'active'
RETURN s.id AS id,
       s.name AS name,
       s.version AS version,
       s.status AS status,
       s.tool_names AS tool_names,
       s.safety AS safety,
       s.executability AS executability,
       s.completeness AS completeness,
       s.maintainability AS maintainability,
       s.cost_awareness AS cost_awareness,
       s.weight_safety AS weight_safety,
       s.weight_executability AS weight_executability,
       s.weight_completeness AS weight_completeness,
       s.weight_maintainability AS weight_maintainability,
       s.weight_cost_awareness AS weight_cost_awareness
"""


class Neo4jSkillRegistryAdapter(SkillRegistryPort):
    """Read the active skills of the registry from Neo4j."""

    def __init__(self, driver: Any) -> None:
        self._driver = driver

    async def load_active(self) -> tuple[Skill, ...]:
        """Return every trustworthy active skill, dropping the ones that are not."""
        async with self._driver.session() as session:
            result = await session.run(_LOAD_ACTIVE_SKILLS)
            skills: list[Skill] = []
            async for record in result:
                skill = skill_from_record(dict(record))
                if skill is not None:
                    skills.append(skill)
            return tuple(skills)
