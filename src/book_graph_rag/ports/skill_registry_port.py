"""Port for reading the active skills the quality gate selects from.

The gate depends on this port and never on Neo4j: the registry adapter (Unit 2) is the
only piece that knows about the graph, and it stays read-only.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from book_graph_rag.domain.skill_models import Skill


class SkillRegistryPort(ABC):
    """Read the active skills of the registry snapshot."""

    @abstractmethod
    async def load_active(self) -> Sequence[Skill]:
        """Return the skills whose ``status`` is active, failing closed on invalid nodes."""
