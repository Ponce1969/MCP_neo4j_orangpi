"""GraphMergePort — atomic merge application + rollback contract."""

from __future__ import annotations

import abc

from pydantic import BaseModel, ConfigDict

from book_graph_rag.domain.merge_ledger_models import (
    EdgeInverseMap,
    FoldedAlias,
    MergeLedgerEntry,
)


class InverseMappingSnapshot(BaseModel):
    """Pre-merge state captured before any graph mutation.

    ``aliases_before`` records each duplicate's aliases so rollback can decide
    what was folded onto the canonical entity. ``edge_inverse_map`` records the
    original endpoints of every edge that is about to be re-pointed, enabling
    deterministic restoration.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    aliases_before: dict[str, tuple[str, ...]]
    edge_inverse_map: list[EdgeInverseMap]


class GraphMergePort(abc.ABC):
    """Atomic graph merge/rollback operations behind a hexagonal port."""

    @abc.abstractmethod
    async def capture_inverse_mapping(
        self, candidate_ids: list[str]
    ) -> InverseMappingSnapshot:
        """Read the current aliases and edge endpoints for ``candidate_ids``.

        The returned snapshot MUST be captured before ``apply_merge`` mutates
        the graph so that rollback can restore the pre-merge state.
        """
        ...

    @abc.abstractmethod
    async def apply_merge(
        self,
        canonical_id: str,
        candidate_ids: list[str],
        aliases_folded: list[FoldedAlias],
        inverse_mapping: InverseMappingSnapshot,
    ) -> None:
        """Atomically apply a merge in a single transaction.

        Steps (all-or-nothing):
        1. Mark each duplicate with ``merged_into = canonical_id`` and
           ``merged_at = datetime()`` (soft-delete).
        2. Re-point incoming/outgoing ``MENTIONS`` and ``RELATED`` edges from
           duplicates to the canonical entity, carrying edge properties.
        3. Append folded aliases onto ``canonical.aliases``.

        Raises ``ResolutionError`` on any failure; the transaction rolls back
        and the graph remains unchanged.
        """
        ...

    @abc.abstractmethod
    async def rollback_merge(self, entry: MergeLedgerEntry) -> None:
        """Reverse a previously applied merge.

        Steps (all-or-nothing):
        1. Remove ``merged_into`` / ``merged_at`` from ``entry.candidate_ids``.
        2. Restore edges to their original endpoints using
           ``entry.edge_inverse_map``.
        3. Remove folded aliases from ``entry.canonical_id.aliases``.

        Raises ``ResolutionError`` if the entry cannot be reversed.
        """
        ...
