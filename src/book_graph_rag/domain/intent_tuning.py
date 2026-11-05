"""Per-intent tuning table for the orchestration router (T6, specs S3 and S13).

S3 says the caller-declared intent tunes *k*, whether relationships are
traversed, and the citation style — but names no values. S13 (the
maintainer's table, verbatim) supplies them, and this module materialises
them as data so the values live in exactly one place.

Pure domain: stdlib + pydantic + :mod:`book_graph_rag.domain.orchestration_models`
only. Nothing here talks to the MCP, Neo4j or a text store.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from book_graph_rag.domain.orchestration_models import Intent


class CitationStyle(StrEnum):
    """How an answer must cite its evidence for a given intent (S13).

    S13, verbatim (maintainer's table): foundation → *"Cita literal
    (chunk_id)"*; design → *"Concepto + referencia"*; implementation →
    *"Cita con código + chunk_id"*.

    Mapping to the maintainer's Spanish words (wire tokens are English, per
    this repo's English-artifact convention; nothing consumes them yet):

    * ``literal_chunk``     — "Cita literal (chunk_id)"
    * ``concept_reference`` — "Concepto + referencia"
    * ``code_with_chunk``   — "Cita con código + chunk_id"
    """

    LITERAL_CHUNK = "literal_chunk"
    CONCEPT_REFERENCE = "concept_reference"
    CODE_WITH_CHUNK = "code_with_chunk"


class IntentTuning(BaseModel):
    """The three knobs S3 lets an intent tune, with S13's exact values.

    Frozen because it is table data, not runtime state: a tuning delivered
    from :data:`INTENT_TUNING_TABLE` must never drift after construction.
    ``k`` is a positive top-k (``gt=0`` rejects 0 and negatives rather than
    coercing them into something that would silently return nothing).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    k: int = Field(gt=0, description="Evidence top-k for this intent (S13)")
    traverses_relationships: bool = Field(
        description="Whether the route walks graph relationships (S13)"
    )
    citation_style: CitationStyle = Field(
        description="Required citation style for this intent (S13)"
    )


# S13 — the maintainer's table, verbatim; no value invented or "fixed" here.
# NOTE on implementation's "No (solo código)": that "code only" is encoded as
# the citation style ONLY, never as a source filter. The corpus is
# architectural prose with no code-chunk set to filter against, so a filter
# would return nothing (open note to the maintainer, L7).
INTENT_TUNING_TABLE: dict[Intent, IntentTuning] = {
    Intent.FOUNDATION: IntentTuning(
        k=3,
        traverses_relationships=False,
        citation_style=CitationStyle.LITERAL_CHUNK,
    ),
    Intent.DESIGN: IntentTuning(
        k=5,
        traverses_relationships=True,
        citation_style=CitationStyle.CONCEPT_REFERENCE,
    ),
    Intent.IMPLEMENTATION: IntentTuning(
        k=4,
        traverses_relationships=False,
        citation_style=CitationStyle.CODE_WITH_CHUNK,
    ),
}


def tuning_for(intent: Intent | None) -> IntentTuning | None:
    """Return the S13 tuning for ``intent``; ``None`` in, ``None`` out.

    S13 defines no row for "no intent", so no default is invented: a caller
    that passes ``intent=None`` keeps its own defaults (L7).
    """
    if intent is None:
        return None
    return INTENT_TUNING_TABLE[intent]
