"""Pure planning of intra-namespace entity merges (audit ``DUPLICATE_ENTITY_LOGICAL``).

The audit groups every non-merged entity by exact ``name`` + ``type`` inside one
namespace and reports the duplicate groups as a warning. This module turns those
raw rows into the deterministic merge plan that ``ApplyMergeUseCase`` consumes: it
touches no graph and imports nothing outside the domain layer.

Canonical policy: the shortest entity id wins, ties broken lexicographically. That
is the recipe already used by the one-off resolution scripts; it prefers the terse
slug (``llm-concept``) over the long spelling (``large-language-model-concept``),
which then survives in the graph while the long variants become folded aliases.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import cast, get_args

from pydantic import BaseModel, ConfigDict, field_validator

from book_graph_rag.domain.models import EntityType

__all__ = [
    "ENTITY_TYPES",
    "DuplicateMemberRow",
    "PlannedMerge",
    "choose_canonical_id",
    "plan_intra_resolution",
    "to_entity_type",
]

ENTITY_TYPES: frozenset[str] = frozenset(get_args(EntityType))


class DuplicateMemberRow(BaseModel):
    """One audit duplicate group: same ``name`` + ``kind``, one id per member."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    kind: EntityType
    entity_ids: tuple[str, ...]

    @field_validator("entity_ids")
    @classmethod
    def _reject_blank_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not entity_id.strip() for entity_id in value):
            raise ValueError("entity_ids must not contain blank ids")
        return value


class PlannedMerge(BaseModel):
    """One merge ready to be staged as a ``MergeGroup``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    kind: EntityType
    canonical_id: str
    duplicate_ids: tuple[str, ...]


def choose_canonical_id(entity_ids: Sequence[str]) -> str:
    """Return the canonical id: shortest wins, ties broken lexicographically."""
    if not entity_ids:
        raise ValueError("cannot choose a canonical id from an empty id set")
    return min(entity_ids, key=lambda entity_id: (len(entity_id), entity_id))


def to_entity_type(value: str) -> EntityType:
    """Narrow a raw graph value to the ``EntityType`` contract, failing fast."""
    if value not in ENTITY_TYPES:
        raise ValueError(f"unsupported entity type: {value!r}")
    return cast(EntityType, value)


def plan_intra_resolution(rows: Iterable[DuplicateMemberRow]) -> list[PlannedMerge]:
    """Build the deterministic merge plan; singleton groups produce no entry."""
    plan: list[PlannedMerge] = []
    for row in rows:
        members = sorted(set(row.entity_ids))
        if len(members) < 2:
            continue
        canonical_id = choose_canonical_id(members)
        plan.append(
            PlannedMerge(
                name=row.name,
                kind=row.kind,
                canonical_id=canonical_id,
                duplicate_ids=tuple(member for member in members if member != canonical_id),
            )
        )
    plan.sort(
        key=lambda merge: (
            -len(merge.duplicate_ids),
            merge.name,
            merge.kind,
            merge.canonical_id,
        )
    )
    return plan
