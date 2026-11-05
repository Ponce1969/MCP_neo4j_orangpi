"""Orchestration request contract: mode, intent and explicit scope (S1-S4).

Pure domain: stdlib + pydantic only. The future orchestration layer parses
untrusted wire payloads into :class:`OrchestrationRequest`; nothing in this
module talks to the MCP, an HTTP client or Neo4j.

The wire vocabulary is English (repo convention for artifacts); the mapping
to the maintainer's Spanish words lives in each enum's docstring. Nothing
consumes these tokens yet, so the choice is cheap to reverse.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from book_graph_rag.domain.namespaces import SourceNamespace


class Mode(StrEnum):
    """How expensive a route the caller opts into (S1).

    S1, verbatim: *"Modo: evidencia (search_rag, $0) vs síntesis (ask_global,
    caro). Default: evidencia. La síntesis es opt-in explícito del llamador."*

    Mapping to the maintainer's words (wire tokens are English, per this
    repo's English-artifact convention; nothing consumes them yet):

    * ``evidence``  — "evidencia": ``search_rag``, $0. Default.
    * ``synthesis`` — "síntesis": ``ask_global``, expensive; only ever chosen
      by an explicit caller opt-in, never inferred by the router.
    """

    EVIDENCE = "evidence"
    SYNTHESIS = "synthesis"


class Intent(StrEnum):
    """Optional caller-declared intent (S3); ``None`` means "unspecified".

    S3, verbatim: *"Valores: fundamento | diseño | implementacion."*

    Mapping to the maintainer's words (wire tokens are English):

    * ``foundation``     — "fundamento"
    * ``design``         — "diseño"
    * ``implementation`` — "implementacion"

    Deliberately absent: any mapping from intent to ``k``, traversal or
    citation style. S3 says the intent tunes those, but those numbers do not
    exist yet and inventing them is out of scope — pending the maintainer's
    table. The intent also never decides which book to read (S4).
    """

    FOUNDATION = "foundation"
    DESIGN = "design"
    IMPLEMENTATION = "implementation"


class OrchestrationRequest(BaseModel):
    """A parsed orchestration request: question plus explicit caller choices.

    Defaults encode S1/S2: ``mode=evidence`` (synthesis is opt-in) and
    ``scope=None``, which means all books — the maintainer's *"los 4 libros
    por defecto"*. An explicit ``scope`` is the only way to narrow it, and an
    empty tuple is rejected because "narrow to nothing" is not a meaningful
    caller choice.

    ``requires_passages`` follows the same posture: it defaults to False and
    is only ever the caller's word (S5) — no validator derives it from
    ``intent`` or ``mode``, which makes the maintainer's ruling on S13's
    *"grafo + pasajes"* ambiguity structural rather than a promise.

    S4 (*"el router no filtra por libro"*) is structural here: ``extra=
    "forbid"`` plus the field set below means the model cannot name a single
    book, and no validator derives ``scope`` from ``intent`` — intent and
    scope stay independent fields.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    question: str
    mode: Mode = Mode.EVIDENCE
    intent: Intent | None = None
    scope: tuple[SourceNamespace, ...] | None = None
    requires_passages: bool = Field(
        default=False,
        description=(
            "Caller's explicit word that passages (BTRAG) are required (S5): when true, "
            "the text port is called in every outcome — covered and fan-out included — "
            "because the caller demands full textual coverage or literal citations. "
            "Never inferred from ``intent`` or ``mode``: only the caller can declare "
            "this requirement."
        ),
    )

    @model_validator(mode="after")
    def _question_not_blank(self) -> OrchestrationRequest:
        """Reject empty/whitespace-only questions without stripping the stored value."""
        if not self.question.strip():
            raise ValueError("question must not be empty or whitespace-only")
        return self

    @model_validator(mode="after")
    def _scope_explicit_and_unique(self) -> OrchestrationRequest:
        """Reject an empty scope (omit it instead) and duplicate namespaces."""
        if self.scope is None:
            return self
        if len(self.scope) == 0:
            raise ValueError("scope must not be empty: omit scope entirely to mean all books")
        if len(set(self.scope)) != len(self.scope):
            raise ValueError("scope must not contain duplicate namespaces")
        return self
