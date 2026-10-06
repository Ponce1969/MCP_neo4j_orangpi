"""ApplyMergeUseCase — capture inverse, apply graph merge, append ledger (Slice E4)."""

from __future__ import annotations

from datetime import UTC, datetime

from book_graph_rag.domain.merge_approval import MergeApproval
from book_graph_rag.domain.merge_ledger_models import (
    FoldedAlias,
    MergeBand,
    MergeLedgerEntry,
)
from book_graph_rag.domain.resolution_errors import (
    CrossNamespaceApprovalRequired,
    ResolutionError,
)
from book_graph_rag.domain.resolution_models import ConfidenceBand
from book_graph_rag.domain.s0_normalization import namespace_from_id
from book_graph_rag.ports.graph_db_port import GraphDatabasePort
from book_graph_rag.ports.graph_merge_port import GraphMergePort, InverseMappingSnapshot
from book_graph_rag.ports.merge_ledger_port import MergeLedgerPort

from .resolve_entities_use_case import MergeGroup

__all__ = ["ApplyMergeUseCase", "MergeGroup"]


def _namespace_of(entity_id: str) -> str | None:
    """Namespace of a qualified id; ``None`` when the id carries no namespace.

    Extraction stays the public ``namespace_from_id`` (no reimplementation).
    An id without a ``corpus:source`` prefix has no namespace to cross, so two
    unqualified ids never form a boundary; unqualified-vs-qualified still do.
    """
    namespace = namespace_from_id(entity_id)
    return namespace if ":" in namespace else None


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

    async def apply(
        self,
        group: MergeGroup,
        *,
        approver: str,
        approval: MergeApproval | None = None,
    ) -> MergeLedgerEntry:
        """Capture inverse mapping, apply the merge, and append the ledger entry.

        Raises ``ResolutionError`` if the group band is not auto-merge-eligible
        or if any candidate is missing from the active entity set.

        Namespace guard (spec 03 §2.4, policy R6.2, design D-A2): checked
        first, before any other validation and before any port is touched — a
        group whose canonical and duplicates span namespaces is refused unless
        ``approval`` is a ``MergeApproval`` naming exactly this canonical and
        the crossing candidates. A same-namespace group never requires an
        approval; a supplied one is deliberately **ignored** there (it cannot
        widen the group and must not block a legitimate merge).
        """
        self._guard_cross_namespace(group, approval)

        if group.band not in {
            ConfidenceBand.EXACT,
            ConfidenceBand.HIGH,
            ConfidenceBand.MEDIUM,
        }:
            raise ResolutionError(f"Cannot apply merge group with non-auto band: {group.band}")

        inverse_mapping = await self._graph_merge.capture_inverse_mapping(group.duplicate_ids)
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

    def _guard_cross_namespace(self, group: MergeGroup, approval: MergeApproval | None) -> None:
        """Refuse a crossing group without a matching ``MergeApproval``.

        Compares the namespace of the canonical (via the public
        ``namespace_from_id``) against every duplicate, then checks the
        supplied reference: canonical must match and ``candidate_ids`` must
        cover the crossing candidates exactly (no missing, no extras).
        """
        canonical_ns = _namespace_of(group.canonical_id)
        crossing = [dup for dup in group.duplicate_ids if _namespace_of(dup) != canonical_ns]
        if not crossing:
            # Same-namespace group: approvals are irrelevant here and ignored.
            return
        if approval is None:
            raise CrossNamespaceApprovalRequired(
                canonical_id=group.canonical_id,
                crossing_ids=crossing,
                detail="no MergeApproval was supplied",
            )
        if approval.canonical_id != group.canonical_id:
            raise CrossNamespaceApprovalRequired(
                canonical_id=group.canonical_id,
                crossing_ids=crossing,
                detail=(
                    f"the supplied MergeApproval names canonical {approval.canonical_id!r} instead"
                ),
            )
        missing = [dup for dup in crossing if dup not in approval.candidate_ids]
        extra = [c for c in approval.candidate_ids if c not in crossing]
        if missing or extra:
            raise CrossNamespaceApprovalRequired(
                canonical_id=group.canonical_id,
                crossing_ids=crossing,
                detail=(
                    "the supplied MergeApproval does not cover exactly the crossing "
                    f"candidates (missing={missing}; extra={extra})"
                ),
            )

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
                folded.append(FoldedAlias(from_entity_id=entity.id, alias_value=alias))
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
