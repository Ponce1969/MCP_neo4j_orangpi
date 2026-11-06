"""Cache write policy for orchestration outcomes (T5, specs S10-S12).

Spec home: ``odd/tasks/tool-intent-router.md``, Regla 3. This module answers
exactly one question — *may this answer be written to the query cache?* — as a
pure domain predicate the separate ``agentic-memory`` repository can consume.
There is deliberately **no store, no SQLite, no I/O and no infrastructure
adapter** here: this repo owns the contract only.

The rules it encodes:

* **S10** — a graph answer built on incomplete entities is never cacheable
  (*"si hubo fallback a texto, no se cachea como respuesta del grafo"*). The
  decision reads the outcome's ``state``, never whether passages happen to be
  present — see :func:`should_cache` for the maintainer's ruling (L8/L9).
* **S11** — a synthesis (``ask_global``) answer without clear provenance is
  never cacheable. The rule is synthesis-scoped by design.
* **S12** — a ``graph_version`` change changes the key, so the cache never
  serves a previous graph's answer (*"si cambia graph_version, cambia la
  clave"*) — this is **not** implemented here: it is already satisfied by
  :func:`book_graph_rag.domain.routing_models.route_cache_key`,
  whose ``graph_snapshot`` dimension rotates the key. This module adds no key
  logic of its own.

The wire vocabulary is English (repo convention for artifacts); the mapping to
the maintainer's Spanish words lives in each member's docstring.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from book_graph_rag.domain.orchestration_models import Mode
from book_graph_rag.domain.orchestration_outcomes import CoverageState, FallbackOutcome


class CacheDecisionReason(StrEnum):
    """Why a cache write is allowed or blocked — one member per blocking rule.

    Mapping to the maintainer's words (S10/S11; wire tokens English per the
    repo's English-artifact convention):

    * ``cacheable`` — no rule fired; the write is allowed.
    * ``insufficient_coverage`` — S10: *"respuestas del grafo construidas
      sobre entidades incompletas"* / *"cobertura insuficiente"*.
    * ``synthesis_without_provenance`` — S11: *"respuestas de síntesis
      (ask_global) que no tengan procedencia clara"*.
    * ``no_evidence`` — defensive: evidence mode produced an outcome with an
      empty ``evidence`` tuple. S6-S8 do not construct that shape, but the
      policy fails closed rather than caching what it cannot see.
    """

    CACHEABLE = "cacheable"
    INSUFFICIENT_COVERAGE = "insufficient_coverage"
    SYNTHESIS_WITHOUT_PROVENANCE = "synthesis_without_provenance"
    NO_EVIDENCE = "no_evidence"


class CacheDecision(BaseModel):
    """Verdict of :func:`should_cache`: the boolean plus the rule behind it.

    The reason rides along deliberately: S10-S12 are exactly the kind of rule
    a later debugging session questions, and a bare boolean would leave that
    session blind to *why* the write was refused.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    cacheable: bool
    reason: CacheDecisionReason


def should_cache(outcome: FallbackOutcome, mode: Mode) -> CacheDecision:
    """Decide whether ``outcome`` may be written to the query cache (S10/S11).

    Rule order — first match wins, documented because more than one rule can
    fire on the same outcome:

    1. **S10 coverage (highest precedence).** ``state is
       INSUFFICIENT_COVERAGE`` blocks the write. The decision reads *only*
       ``outcome.state``: ``outcome.passages`` being non-empty does **not**
       mean "entidades incompletas". Passages present because the caller
       demanded them (S5's ``requires_passages``) while the graph still
       covered the question belong to a ``covered``/``fanout`` outcome and
       stay cacheable — the maintainer's ruling on the T3 nuance (L8/L9).
       Never test ``outcome.passages`` here; testing it would re-open the
       exact distinction that ruling closed.
    2. **S11 provenance (synthesis only).** ``mode is SYNTHESIS`` with no
       non-empty citation id across any evidence is not cacheable.
       Provenance means *at least one non-empty citation id* somewhere in
       ``outcome.evidence``. S11 is synthesis-scoped: **evidence mode is not
       restricted by it, even when its citations are thin** — widening the
       rule would be a spec change, not this predicate's call.
    3. **Defensive ``no_evidence`` (evidence only).** Evidence mode with an
       empty ``evidence`` tuple fails closed. This check is evidence-scoped,
       so it is mutually exclusive with rule 2: in synthesis mode an empty
       ``evidence`` tuple is already caught by S11 as missing provenance.
    4. Otherwise the write is ``cacheable``.

    S12 is not part of this predicate: key rotation with ``graph_version``
    lives in :func:`book_graph_rag.domain.routing_models.route_cache_key`.
    """
    if outcome.state is CoverageState.INSUFFICIENT_COVERAGE:
        return CacheDecision(cacheable=False, reason=CacheDecisionReason.INSUFFICIENT_COVERAGE)
    if mode is Mode.SYNTHESIS and not _has_provenance(outcome):
        return CacheDecision(
            cacheable=False,
            reason=CacheDecisionReason.SYNTHESIS_WITHOUT_PROVENANCE,
        )
    if mode is Mode.EVIDENCE and not outcome.evidence:
        return CacheDecision(cacheable=False, reason=CacheDecisionReason.NO_EVIDENCE)
    return CacheDecision(cacheable=True, reason=CacheDecisionReason.CACHEABLE)


def _has_provenance(outcome: FallbackOutcome) -> bool:
    """True when any evidence item carries at least one non-empty citation id."""
    return any(citation for item in outcome.evidence for citation in item.citations)
