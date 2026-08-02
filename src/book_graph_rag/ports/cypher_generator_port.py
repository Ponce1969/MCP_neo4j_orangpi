"""Port for LLM-based Cypher generation."""

from __future__ import annotations

import abc
from dataclasses import dataclass


@dataclass(frozen=True)
class CypherFailureContext:
    """Context passed to the LLM when a previously generated Cypher query failed.

    Attributes:
        failed_cypher: The Cypher query that did not pass validation/EXPLAIN.
        error_message: The exact error message returned by Neo4j or the validator.
    """

    failed_cypher: str
    error_message: str


class CypherGeneratorPort(abc.ABC):
    """Contract for generating a Cypher query from a natural-language question.

    Implementations receive a graph schema description, the user question, and
    an optional failure context for self-healing retries.
    """

    @abc.abstractmethod
    async def generate_cypher(
        self, schema: str, question: str, failure: CypherFailureContext | None
    ) -> str:
        """Return a Cypher query string that answers ``question``.

        Args:
            schema: A concise description of the graph schema (labels, rels).
            question: Natural-language question from the user.
            failure: Optional context from a previous failed attempt.

        Returns:
            A Cypher query string. Implementations SHOULD include a LIMIT clause.
        """
        ...
