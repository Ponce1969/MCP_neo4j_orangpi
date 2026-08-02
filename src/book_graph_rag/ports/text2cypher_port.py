"""Port for natural-language to validated Cypher execution."""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class Text2CypherResult:
    """Result of a natural-language Cypher generation pipeline.

    Attributes:
        question: The original natural-language question.
        cypher: The Cypher query that was validated and executed (or last attempted).
        rows: Structured records returned by the query, or empty on failure.
        schema_source: Whether the schema came from APOC or the hardcoded fallback.
        retries: Number of self-healing retries consumed (0..2).
    """

    question: str
    cypher: str
    rows: list[dict[str, Any]]
    schema_source: Literal["apoc", "hardcoded"]
    retries: int


class Text2CypherPort(abc.ABC):
    """Contract for end-to-end text-to-Cypher generation and execution.

    Implementations own schema inference, LLM generation, read-only validation,
    EXPLAIN validation, and query execution.
    """

    @abc.abstractmethod
    async def generate_and_run(self, question: str) -> Text2CypherResult:
        """Generate a Cypher query from ``question`` and execute it safely.

        Args:
            question: Natural-language question from the user.

        Returns:
            A ``Text2CypherResult`` with the generated query and rows.
        """
        ...
