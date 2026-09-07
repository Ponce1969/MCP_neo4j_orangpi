"""Property: rollback is idempotent and preserves the tamper-evident chain.

A rollback appends a compensating ledger entry.  Repeating the rollback on the
same original entry must be a safe no-op: no exception and no corruption of the
ledger.  The chain must remain verifiable after both calls.

These tests are pure: they use in-memory fakes for ``GraphMergePort`` and
``MergeLedgerPort`` with no Neo4j or testcontainers.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from book_graph_rag.application.rollback_merge_use_case import RollbackMergeUseCase
from book_graph_rag.domain.merge_ledger_models import (
    EdgeInverseMap,
    FoldedAlias,
    MergeBand,
    MergeLedgerEntry,
    chained_hash,
)
from book_graph_rag.domain.resolution_errors import LedgerChainBroken
from book_graph_rag.ports.graph_merge_port import GraphMergePort, InverseMappingSnapshot
from book_graph_rag.ports.merge_ledger_port import MergeLedgerPort


class _FakeGraphMerge(GraphMergePort):
    """Records rollback calls and tracks which candidates are still merged."""

    def __init__(self) -> None:
        self._merged: set[str] = set()
        self.rollbacks: list[MergeLedgerEntry] = []

    def seed_merged(self, candidate_ids: list[str]) -> None:
        """Mark candidates as currently merged before the first rollback."""
        self._merged.update(candidate_ids)

    async def capture_inverse_mapping(
        self, candidate_ids: list[str]
    ) -> InverseMappingSnapshot:
        return InverseMappingSnapshot(
            aliases_before=dict.fromkeys(candidate_ids, ()),
            edge_inverse_map=[],
        )

    async def apply_merge(
        self,
        canonical_id: str,
        candidate_ids: list[str],
        aliases_folded: list[FoldedAlias],
        inverse_mapping: InverseMappingSnapshot,
    ) -> None:
        raise NotImplementedError("not used by rollback property tests")

    async def rollback_merge(self, entry: MergeLedgerEntry) -> None:
        still_merged = self._merged & set(entry.candidate_ids)
        if not still_merged:
            from book_graph_rag.domain.resolution_errors import MergeNotReversible

            raise MergeNotReversible(
                f"candidates {entry.candidate_ids} are not currently merged"
            )
        self.rollbacks.append(entry)
        self._merged.difference_update(entry.candidate_ids)


class _FakeMergeLedger(MergeLedgerPort):
    """In-memory ledger that computes chained hashes like the JSONL adapter."""

    def __init__(self) -> None:
        self.entries: list[MergeLedgerEntry] = []
        self.verified: int = 0
        self._broken: bool = False

    def append(self, entry: MergeLedgerEntry) -> None:
        prev = self.entries[-1].entry_sha256 if self.entries else "0" * 64
        chained = chained_hash(entry, prev)
        self.entries.append(chained)

    def read_all(self) -> list[MergeLedgerEntry]:
        return list(self.entries)

    def read_by_seq(self, seq: int) -> MergeLedgerEntry | None:
        for entry in self.entries:
            if entry.seq == seq:
                return entry
        return None

    def verify_chain(self) -> None:
        self.verified += 1
        if self._broken:
            raise LedgerChainBroken("fake chain broken")

    def break_chain(self) -> None:
        self._broken = True


def _build_entry(
    seq: int,
    candidate_ids: list[str],
    aliases: list[FoldedAlias],
    edges: list[EdgeInverseMap],
) -> MergeLedgerEntry:
    return MergeLedgerEntry(
        schema_version="1.0.0",
        seq=seq,
        candidate_ids=list(candidate_ids),
        canonical_id="canon",
        band=MergeBand.HIGH,
        evidence=[],
        aliases_folded=list(aliases),
        edge_inverse_map=list(edges),
        approver="alice",
        applied_at=datetime(2024, 1, 1, tzinfo=UTC),
    )


@st.composite
def _rollback_scenario(
    draw: st.DrawFn,
) -> tuple[list[str], list[FoldedAlias], list[EdgeInverseMap]]:
    candidate_count = draw(st.integers(min_value=1, max_value=8))
    candidate_ids = [f"dup{i}" for i in range(candidate_count)]

    alias_count = draw(st.integers(min_value=0, max_value=candidate_count * 2))
    aliases: list[FoldedAlias] = []
    for i in range(alias_count):
        dup = draw(st.sampled_from(candidate_ids))
        aliases.append(
            FoldedAlias(from_entity_id=dup, alias_value=f"alias-{i}")
        )

    edge_kinds = ["MENTIONS", "RELATED"]
    edge_count = draw(st.integers(min_value=0, max_value=candidate_count * 2))
    edges: list[EdgeInverseMap] = []
    for i in range(edge_count):
        dup = draw(st.sampled_from(candidate_ids))
        edges.append(
            EdgeInverseMap(
                edge_kind=draw(st.sampled_from(edge_kinds)),
                duplicate_entity_id=dup,
                original_other_endpoint_id=f"other-{i}",
                edge_properties={"key": f"value-{i}"},
            )
        )

    return candidate_ids, aliases, edges


@given(scenario=_rollback_scenario())
@settings(max_examples=150, deadline=None)
@pytest.mark.asyncio
async def test_rollback_is_idempotent_and_preserves_verifiable_chain(
    scenario: tuple[list[str], list[FoldedAlias], list[EdgeInverseMap]],
) -> None:
    """First rollback succeeds; second rollback on the same entry is a no-op.

    The ledger must contain exactly one compensating entry pointing back to the
    original sequence, and the chain must still verify.
    """
    candidate_ids, aliases, edges = scenario
    original = _build_entry(
        seq=1, candidate_ids=candidate_ids, aliases=aliases, edges=edges
    )
    ledger = _FakeMergeLedger()
    ledger.append(original)
    original = ledger.entries[0]

    graph_merge = _FakeGraphMerge()
    graph_merge.seed_merged(candidate_ids)

    use_case = RollbackMergeUseCase(ledger=ledger, graph_merge=graph_merge)

    await use_case.rollback(seq=1)

    # Second rollback on the same original entry must be a safe no-op.
    await use_case.rollback(seq=1)

    assert len(graph_merge.rollbacks) == 1
    assert len(ledger.entries) == 2

    compensating = ledger.entries[1]
    assert compensating.rollback_of == original.seq
    assert compensating.candidate_ids == original.candidate_ids
    assert compensating.canonical_id == original.canonical_id
    assert compensating.aliases_folded == original.aliases_folded
    assert compensating.edge_inverse_map == original.edge_inverse_map
    assert compensating.entry_sha256 != ""
    assert compensating.prev_seq_sha256 == original.entry_sha256

    ledger.verify_chain()
    assert ledger.verified == 3  # two rollback calls + explicit verification
