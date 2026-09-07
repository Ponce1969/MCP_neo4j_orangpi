"""ApplyMergeUseCase — capture inverse, apply graph merge, append ledger (Slice E4)."""

from __future__ import annotations

from datetime import UTC, datetime

from book_graph_rag.domain.merge_ledger_models import (
    FoldedAlias,
    MergeBand,
    MergeLedgerEntry,
)
from book_graph_rag.domain.resolution_errors import ResolutionError
from book_graph_rag.domain.resolution_models import ConfidenceBand
from book_graph_rag.ports.graph_db_port import GraphDatabasePort
from book_graph_rag.ports.graph_merge_port import GraphMergePort, InverseMappingSnapshot
from book_graph_rag.ports.merge_ledger_port import MergeLedgerPort

from .resolve_entities_use_case import MergeGroup

__all__ = ["ApplyMergeUseCase", "MergeGroup"]


class ApplyMergeUseCase:
    """Apply a staged merge group to the graph and record it in the ledger."""

    def __init__(
        self,
        graph_merge: GraphMergePort,
        ledger: MergeLedgerPort,
        entity_loader: GraphDatabasePort,
    ) -> None:
        self._graph_merge = graph_merge
        self._ledger = ledger
        self._entity_loader = entity_loader

    async def apply(self, group: MergeGroup, *, approver: str) -> MergeLedgerEntry:
        """Capture inverse mapping, apply the merge, and append the ledger entry.

        Raises ``ResolutionError`` if the group band is not auto-merge-eligible
        or if any candidate is missing from the active entity set.
        """
        if group.band not in {
            ConfidenceBand.EXACT,
            ConfidenceBand.HIGH,
            ConfidenceBand.MEDIUM,
        }:
            raise ResolutionError(
                f"Cannot apply merge group with non-auto band: {group.band}"
            )

        inverse_mapping = await self._graph_merge.capture_inverse_mapping(
            group.duplicate_ids
        )
        aliases_folded = await self._aliases_to_fold(group)

        await self._graph_merge.apply_merge(
            canonical_id=group.canonical_id,
            candidate_ids=group.duplicate_ids,
            aliases_folded=aliases_folded,
            inverse_mapping=inverse_mapping,
        )

        entry = self._build_ledger_entry(group, aliases_folded, inverse_mapping, approver)
        self._ledger.append(entry)
        # The ledger returns a chained copy with hashes populated.
        return self._ledger.read_all()[-1]

    async def _aliases_to_fold(self, group: MergeGroup) -> list[FoldedAlias]:
        """Return every alias belonging to the duplicates as a FoldedAlias."""
        entities = await self._entity_loader.load_active_entities()
        active = {e.id: e for e in entities}
        folded: list[FoldedAlias] = []
        for dup_id in group.duplicate_ids:
            entity = active.get(dup_id)
            if entity is None:
                raise ResolutionError(
                    f"Cannot apply merge: candidate {dup_id} is not an active entity"
                )
            for alias in entity.aliases:
                folded.append(
                    FoldedAlias(from_entity_id=entity.id, alias_value=alias)
                )
        return folded

    def _build_ledger_entry(
        self,
        group: MergeGroup,
        aliases_folded: list[FoldedAlias],
        inverse_mapping: InverseMappingSnapshot,
        approver: str,
    ) -> MergeLedgerEntry:
        """Construct the ledger entry with the next monotonic sequence number."""
        existing = self._ledger.read_all()
        next_seq = 1 if not existing else max(e.seq for e in existing) + 1
        return MergeLedgerEntry(
            schema_version="1.0.0",
            seq=next_seq,
            candidate_ids=list(group.duplicate_ids),
            canonical_id=group.canonical_id,
            band=MergeBand(group.band.value),
            evidence=list(group.evidence),
            aliases_folded=aliases_folded,
            edge_inverse_map=list(inverse_mapping.edge_inverse_map),
            approver=approver,
            applied_at=datetime.now(UTC),
        )
