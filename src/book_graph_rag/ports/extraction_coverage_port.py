"""Read-only port for the extraction coverage instrument (front d, S1/S6)."""

from __future__ import annotations

import abc

from pydantic import BaseModel, ConfigDict, Field, model_validator


class BookCoverage(BaseModel):
    """Per-book/namespace breakdown of the coverage quotient (AC-PROV-01 detail).

    Both counts exclude merged entities (``merged_into IS NULL OR merged_into = ''``):
    the audit inventory includes them, this detail never does, which is why the sum of
    ``mentioned_entities`` can sit below the inventory's entity total.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    book_id: str = Field(min_length=1)
    chunk_count: int = Field(ge=0)
    mentioned_entities: int = Field(ge=0)


class ExtractionCoverageMeasurement(BaseModel):
    """The operator's approved quotient (S1), verbatim in algebra.

    Denominator: active entities (``merged_into IS NULL OR merged_into = ''``) — merged
    entities are explicitly discarded so they cannot inflate the base. Numerator:
    ``COUNT(DISTINCT e)`` of active entities holding at least one incoming
    ``(:Chunk)-[:MENTIONS]->(e)`` relationship, the only algebraically valid definition
    for a quotient bounded in [0, 1].
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    active_entities: int = Field(ge=0)
    mentioned_active_entities: int = Field(ge=0)
    books: tuple[BookCoverage, ...] = ()

    @model_validator(mode="after")
    def _quotient_bounded(self) -> ExtractionCoverageMeasurement:
        if self.mentioned_active_entities > self.active_entities:
            raise ValueError(
                "numerator cannot exceed the active denominator: the quotient must stay "
                "bounded in [0, 1]"
            )
        return self


class ExtractionCoveragePort(abc.ABC):
    """Contract for measuring the :MENTIONS coverage invariant (REQ-PROV-03).

    Read-only by construction: every implementation is a ``MATCH … RETURN`` over the
    graph, never a mutation.
    """

    @abc.abstractmethod
    async def measure(self) -> ExtractionCoverageMeasurement:
        """Return the coverage quotient's two halves plus the per-book breakdown."""
        ...
