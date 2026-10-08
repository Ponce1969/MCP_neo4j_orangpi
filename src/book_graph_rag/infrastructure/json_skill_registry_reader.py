"""Read the active skills from a JSON artifact (skill-quality-gating, Unit 2.3).

Deterministic mirror of the Neo4j reader, for tests that must not need a graph. It follows
the ``JsonNamespaceProfileReader`` convention: no artifact yet is an empty snapshot, not an
error. A malformed artifact does raise, because that is a broken fixture the caller wants to
hear about rather than a state the gate should paper over.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from book_graph_rag.domain.skill_models import Skill
from book_graph_rag.infrastructure.skill_record_mapping import skill_from_record
from book_graph_rag.ports.skill_registry_port import SkillRegistryPort


class JsonSkillRegistryReader(SkillRegistryPort):
    """Expose a persisted skill snapshot to the gate."""

    def __init__(self, path: Path) -> None:
        self._path = path

    async def load_active(self) -> tuple[Skill, ...]:
        """Return the trustworthy active skills, or an empty tuple when there is no artifact."""
        if not self._path.exists():
            return ()
        payload: Any = json.loads(self._path.read_text(encoding="utf-8"))
        entries: Any = payload.get("skills", []) if isinstance(payload, dict) else []
        if not isinstance(entries, list):
            return ()
        skills: list[Skill] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            skill = skill_from_record(entry)
            if skill is not None:
                skills.append(skill)
        return tuple(skills)
