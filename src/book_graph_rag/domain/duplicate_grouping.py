"""Pure planning of intra-namespace entity merges (audit ``DUPLICATE_ENTITY_LOGICAL``).

The audit groups every non-merged entity by ``toLower(trim(name))`` + ``type``
inside one namespace (T9b: case-insensitive, matching the rule the audit
reports) and reports the duplicate groups as a warning. This module turns those
raw rows into the deterministic merge plan that ``ApplyMergeUseCase`` consumes:
it touches no graph and imports nothing outside the domain layer.

Two canonical policies (T10):

* :func:`choose_canonical_id` — the historical rule (shortest id, ties broken
  lexicographically), kept as the default so nothing else in the repo changes
  behaviour.
* :func:`choose_canonical_id_by_richness` — the maintainer's T10 choice when
  measured scores are supplied: most mentions first, then highest RELATED
  degree. The canonical id is the one that SURVIVES and that consumers
  reference later (MCP lookups, evidence bundles, the decision registry), so
  the shortest slug is a poor proxy for "the id worth keeping"; measured on
  the production corpus the two rules disagree on 20 of the 64 case-only
  groups.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import cast, get_args

from pydantic import BaseModel, ConfigDict, field_validator

from book_graph_rag.domain.models import EntityType

__all__ = [
    "ENTITY_TYPES",
    "DuplicateMemberRow",
    "PlannedMerge",
    "choose_canonical_id",
    "choose_canonical_id_by_richness",
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
    """Return the canonical id: shortest wins, ties broken lexicographically.

    The historical policy (confirmed by the maintainer 2026-10-01): it prefers
    the terse slug (``llm-concept``) over the long spelling, which then
    survives in the graph while the long variants become folded aliases.
    """
    if not entity_ids:
        raise ValueError("cannot choose a canonical id from an empty id set")
    return min(entity_ids, key=lambda entity_id: (len(entity_id), entity_id))


def choose_canonical_id_by_richness(scores: Mapping[str, tuple[int, int]]) -> str:
    """Return the canonical id by measured richness (T10, maintainer's choice).

    ``scores`` maps entity id -> ``(mentions, related degree)``. Ranking:
    **most mentions first, then highest RELATED degree, then shortest id, then
    lexicographic** — deterministic regardless of mapping order.

    Why not the shortest id: the canonical id is the one that SURVIVES the
    merge and that consumers reference afterwards (MCP lookups, evidence
    bundles, the decision registry), so the shortest slug is a poor proxy for
    "the id worth keeping"; measured on the production corpus the two rules
    disagree on 20 of the 64 case-only groups. :func:`choose_canonical_id`
    stays the default when no scores are supplied, so nothing else in the repo
    changes behaviour.
    """
    if not scores:
        raise ValueError("cannot choose a canonical id from an empty id set")
    return min(
        scores,
        key=lambda entity_id: (
            -scores[entity_id][0],
            -scores[entity_id][1],
            len(entity_id),
            entity_id,
        ),
    )


def to_entity_type(value: str) -> EntityType:
    """Narrow a raw graph value to the ``EntityType`` contract, failing fast."""
    if value not in ENTITY_TYPES:
        raise ValueError(f"unsupported entity type: {value!r}")
    return cast(EntityType, value)


def plan_intra_resolution(
    rows: Iterable[DuplicateMemberRow],
    *,
    scores: Mapping[str, tuple[int, int]] | None = None,
) -> list[PlannedMerge]:
    """Build the deterministic merge plan; singleton groups produce no entry.

    ``scores`` (T10) is an optional ``entity id -> (mentions, related degree)``
    mapping covering every member of every row. When provided, each group's
    canonical is chosen with :func:`choose_canonical_id_by_richness`; when
    omitted, the historical :func:`choose_canonical_id` (shortest id) applies
    unchanged. Fail-closed: a member missing from ``scores`` raises
    ``ValueError`` instead of silently ranking it as (0, 0).
    """
    plan: list[PlannedMerge] = []
    for row in rows:
        members = sorted(set(row.entity_ids))
        if len(members) < 2:
            continue
        if scores is None:
            canonical_id = choose_canonical_id(members)
        else:
            missing = [member for member in members if member not in scores]
            if missing:
                raise ValueError(f"scores missing entity ids: {sorted(missing)}")
            canonical_id = choose_canonical_id_by_richness(
                {member: scores[member] for member in members}
            )
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
