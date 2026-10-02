"""Pure planning of re-points for edges that touch soft-deleted (``merged_into``) entities.

A merge marks the duplicate with ``merged_into = <canonical>`` but leaves the
incoming/outgoing edges where they were whenever the adapter's inverse mapping
missed them; this module turns those dangling edges into a deterministic,
reversible plan. It touches no graph and imports nothing outside the domain
layer, so every anomaly decision is unit-testable in isolation.

Canonical policy (frozen decision D2): resolution walks ``merged_into``
transitively until it reaches an id that is not a key, with a visited-set guard
that reports cycles instead of looping. Frozen decision D3: cycles and chains
that end on a missing target are *excluded and reported*, never guessed.
Frozen decision D1: knowledge is preserved by re-pointing onto the terminal
canonical; only a RELATED edge whose endpoints collapse onto the same node is
deleted (it would become a self-loop). MENTIONS are never deleted.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

__all__ = [
    "CanonicalResolution",
    "DanglingEdge",
    "EdgeAction",
    "ExcludedEntry",
    "RepointEntry",
    "RepointPlan",
    "ResolutionState",
    "merge_key_for",
    "plan_repoint",
    "resolve_canonical",
]


class ResolutionState(StrEnum):
    """Outcome of walking an id's ``merged_into`` chain to its terminal node."""

    RESOLVED = "resolved"
    CYCLE = "cycle"
    MISSING_TARGET = "missing_target"


class CanonicalResolution(BaseModel):
    """One endpoint's walk: terminal canonical (when resolved) and the visited path."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    state: ResolutionState
    start_id: str
    canonical_id: str | None
    chain: tuple[str, ...]


class DanglingEdge(BaseModel):
    """One live edge with a soft-deleted endpoint, as read from the graph.

    ``properties`` carries the live edge properties verbatim so the operation
    stays reversible: replaying the properties onto the original endpoints
    restores the pre-run state. For MENTIONS the source is the chunk identity
    (``<source_id>:chunk-<index>``, the same composite the merge adapter uses)
    and ``relation_type`` is ``None``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["MENTIONS", "RELATED"]
    source_id: str
    target_id: str
    relation_type: str | None = None
    properties: dict[str, Any]


class EdgeAction(StrEnum):
    """What the apply phase does with a planned entry."""

    REPOINT = "repoint"
    DELETE_COLLAPSE = "delete_collapse"


class RepointEntry(BaseModel):
    """One edge ready to mutate: original endpoints live on ``edge``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    edge: DanglingEdge
    new_source_id: str
    new_target_id: str
    action: EdgeAction


class ExcludedEntry(BaseModel):
    """One edge kept out of the mutation because resolution failed (D3)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    edge: DanglingEdge
    reason: ResolutionState


def merge_key_for(entry: RepointEntry) -> str:
    """MERGE dedupe key of a re-point: ``new_source|kind|relation_type|new_target``.

    Two re-points that produce the same key converge onto one MERGE'd edge, so
    only the first creates it and the rest fold their properties into it. For
    MENTIONS ``relation_type`` renders as ``None``.
    """
    return (
        f"{entry.new_source_id}|{entry.edge.kind}|{entry.edge.relation_type}|{entry.new_target_id}"
    )


class RepointPlan(BaseModel):
    """The full, frozen plan: mutations to apply and anomalies to report."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entries: tuple[RepointEntry, ...]
    excluded: tuple[ExcludedEntry, ...]

    @property
    def repoint_count(self) -> int:
        """Number of entries that will be re-pointed (excludes deletions)."""
        return sum(1 for entry in self.entries if entry.action is EdgeAction.REPOINT)

    @property
    def collapse_count(self) -> int:
        """Number of entries deleted because both endpoints collapse onto one node."""
        return sum(1 for entry in self.entries if entry.action is EdgeAction.DELETE_COLLAPSE)

    @property
    def merge_key_count(self) -> int:
        """Distinct MERGE keys produced by the re-point entries."""
        return len(
            {merge_key_for(entry) for entry in self.entries if entry.action is EdgeAction.REPOINT}
        )

    @property
    def merge_collapsed(self) -> int:
        """Re-points folded into an existing MERGE key instead of creating a new edge."""
        return self.repoint_count - self.merge_key_count


def resolve_canonical(
    start_id: str,
    *,
    merged_into: Mapping[str, str],
    known_ids: Collection[str],
) -> CanonicalResolution:
    """Walk ``merged_into`` transitively until an id that is not a key.

    A visited set detects cycles, including the self-referential ``a -> a``;
    the reported ``chain`` is the ordered visited path (start first) and, for a
    cycle, ends with the repeated id that closed it. A terminal id outside
    ``known_ids`` yields ``MISSING_TARGET`` — the canonical node is gone, so
    guessing is not an option (D3).
    """
    visited: list[str] = []
    seen: set[str] = set()
    current = start_id
    while True:
        visited.append(current)
        if current in seen:
            return CanonicalResolution(
                state=ResolutionState.CYCLE,
                start_id=start_id,
                canonical_id=None,
                chain=tuple(visited),
            )
        seen.add(current)
        next_id = merged_into.get(current)
        if not next_id:  # not a key (or a blank marker): current is terminal
            resolved = current in known_ids
            return CanonicalResolution(
                state=ResolutionState.RESOLVED if resolved else ResolutionState.MISSING_TARGET,
                start_id=start_id,
                canonical_id=current if resolved else None,
                chain=tuple(visited),
            )
        current = next_id


def plan_repoint(
    edges: Sequence[DanglingEdge],
    *,
    merged_into: Mapping[str, str],
    known_ids: Collection[str],
) -> RepointPlan:
    """Build the deterministic mutation plan for the read set of dangling edges.

    Only endpoints that are merge keys are resolved; live endpoints pass
    through unchanged. Any edge with a ``CYCLE`` or ``MISSING_TARGET`` endpoint
    goes to ``excluded`` (checked source first, then target) and never reaches
    ``entries``. MENTIONS are always re-pointed (never deleted); RELATED is
    deleted exactly when both endpoints collapse onto the same node, otherwise
    re-pointed — direction is preserved per endpoint, so an in-edge whose
    source was merged becomes ``canonical -> other`` as expected.
    """
    entries: list[RepointEntry] = []
    excluded: list[ExcludedEntry] = []
    for edge in edges:
        canonicals: dict[str, str] = {}
        anomaly: ResolutionState | None = None
        for endpoint in (edge.source_id, edge.target_id):
            if endpoint not in merged_into:
                canonicals[endpoint] = endpoint
                continue
            resolution = resolve_canonical(endpoint, merged_into=merged_into, known_ids=known_ids)
            if resolution.state is not ResolutionState.RESOLVED:
                anomaly = resolution.state
                break
            canonicals[endpoint] = resolution.canonical_id or endpoint
        if anomaly is not None:
            excluded.append(ExcludedEntry(edge=edge, reason=anomaly))
            continue
        new_source_id = canonicals[edge.source_id]
        new_target_id = canonicals[edge.target_id]
        collapses = edge.kind == "RELATED" and new_source_id == new_target_id
        entries.append(
            RepointEntry(
                edge=edge,
                new_source_id=new_source_id,
                new_target_id=new_target_id,
                action=EdgeAction.DELETE_COLLAPSE if collapses else EdgeAction.REPOINT,
            )
        )
    return RepointPlan(entries=tuple(entries), excluded=tuple(excluded))
