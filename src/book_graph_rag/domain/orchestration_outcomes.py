"""Fallback coverage outcomes: one outcome, three explicit states (S6-S8).

Spec home: ``odd/tasks/tool-intent-router.md``, Regla 2 (Fallback). S6 says the
orchestrator must never invent when the graph misses the entity: it returns an
explicit *"cobertura insuficiente"* state and derives the query to text. S7
makes that derivation an evidence-based decision, not a blind retry. S8 turns a
``low_margin`` abstention into a top-2 fan-out + merge instead of an error.

Pure domain: stdlib + pydantic only. Nothing here talks to Neo4j, the MCP or a
text store; the ports that feed these models live in the application layer
(``application/orchestration/ports.py``).

The wire vocabulary is English (repo convention for artifacts); the mapping to
the maintainer's Spanish words lives in each member's docstring.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from book_graph_rag.domain.namespaces import SourceNamespace


class GraphEvidence(BaseModel):
    """Evidence the graph produced for one scoped question.

    ``citations`` carries the citation ids exactly as the graph returns them —
    never invented, never renumbered (S6: *"el orquestador no debe inventar"*).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    namespace: SourceNamespace
    summary: str
    citations: tuple[str, ...]


class Passage(BaseModel):
    """One text passage from the text store (BTRAG), used when the graph does not cover."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    chunk_id: str
    text: str
    namespace: SourceNamespace | None = None


class CoverageState(StrEnum):
    """Coverage state of a fallback resolution — exactly one per outcome (S6).

    S6, verbatim: *"Debe devolver un estado explícito de 'cobertura insuficiente'"*.
    The tokens are English per the repo's English-artifact convention:

    * ``covered``               — the validated scope produced graph evidence.
    * ``fanout``                — S8: ``low_margin`` top-2 fan-out merged evidence.
    * ``insufficient_coverage`` — *"cobertura insuficiente"*: derive to text (S6/S7).
    """

    COVERED = "covered"
    FANOUT = "fanout"
    INSUFFICIENT_COVERAGE = "insufficient_coverage"


class FallbackOutcome(BaseModel):
    """Result of ``resolve_with_fallback``: one state, never a fabricated one.

    ``scopes`` lists only the namespaces whose graph query produced evidence,
    in route order. ``reason`` is diagnostic text naming the router reason
    (and, on a graph miss, the missing scope); it never substitutes for
    ``state``, and it never carries invented evidence.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: CoverageState
    evidence: tuple[GraphEvidence, ...] = ()
    passages: tuple[Passage, ...] = ()
    scopes: tuple[SourceNamespace, ...] = ()
    reason: str
