"""S0 deterministic normalization and exact-match logic (Slice B).

Pure functions only: NFKC → casefold → whitespace collapse. No model, no I/O,
no randomness. Used by the resolution pipeline before any embedding-backed
stage runs.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Literal

from book_graph_rag.domain.models import Entity
from book_graph_rag.domain.resolution_models import (
    ConfidenceBand,
    ResolutionEvidence,
    S0NormalizedForm,
)


def _normalize_text(text: str) -> str:
    """NFKC + casefold + whitespace-collapse. Pure."""
    nfkc = unicodedata.normalize("NFKC", text)
    case = nfkc.casefold()
    return re.sub(r"\s+", " ", case).strip()


def _compact(text: str) -> str:
    """Strip whitespace, hyphen, underscore; preserve +, #, .. Pure."""
    return re.sub(r"[\s\-_]+", "", text)


def _tokens(text: str) -> tuple[str, ...]:
    """Split on whitespace/hyphen/underscore; preserve other punctuation."""
    return tuple(t for t in re.split(r"[\s\-_]+", text) if t)


def _namespace_from_id(entity_id: str) -> str:
    """Extract ``corpus:source`` namespace from ``corpus:source:slug-type``.

    Falls back to the full id when the format does not contain at least two
    segments, keeping the function total and deterministic.
    """
    parts = entity_id.split(":")
    if len(parts) >= 2:
        return f"{parts[0]}:{parts[1]}"
    return entity_id


def normalize_form(text: str) -> S0NormalizedForm:
    """Return the deterministic normalized form of ``text``."""
    nfkc = unicodedata.normalize("NFKC", text)
    casefold = nfkc.casefold()
    compact = _compact(casefold)
    return S0NormalizedForm(
        original=text,
        nfkc=_normalize_text(text),
        casefold=casefold,
        compact=compact,
        tokens=_tokens(casefold),
    )


def _canonical_forms(entity: Entity) -> set[str]:
    """Normalized canonical strings derived from an entity's names."""
    forms: set[str] = set()
    forms.add(normalize_form(entity.name).compact)
    if entity.canonical_name:
        forms.add(normalize_form(entity.canonical_name).compact)
    return forms


def _alias_forms(entity: Entity) -> set[str]:
    """Normalized alias strings derived from an entity's aliases."""
    forms: set[str] = set()
    for alias in entity.aliases:
        forms.add(normalize_form(alias).compact)
    return forms


def _detect_s0_match(
    anchor: Entity,
    candidate: Entity,
) -> tuple[Literal["id", "canonical", "alias", "none"], bool]:
    """Determine the highest-priority S0 match between two entities.

    Priority: id > canonical (name/canonical_name) > alias. All comparisons use
    the compact normalized form so that case, whitespace, and hyphen variants
    collapse to a single value.
    """
    anchor_norm = normalize_form(anchor.id)
    candidate_norm = normalize_form(candidate.id)
    if anchor_norm.compact == candidate_norm.compact:
        return "id", True

    anchor_canonical = _canonical_forms(anchor)
    candidate_canonical = _canonical_forms(candidate)
    if anchor_canonical & candidate_canonical:
        return "canonical", True

    anchor_aliases = _alias_forms(anchor)
    candidate_aliases = _alias_forms(candidate)
    candidate_names = _canonical_forms(candidate)
    anchor_names = _canonical_forms(anchor)

    if (
        anchor_aliases & candidate_aliases
        or anchor_aliases & candidate_names
        or candidate_aliases & anchor_names
    ):
        return "alias", True

    return "none", False


def s0_match(anchor: Entity, candidate: Entity) -> ResolutionEvidence:
    """Return a ResolutionEvidence for the S0 stage.

    If id/canonical/alias matches after normalization, ``band`` is set to
    ``exact`` and ``s0_matched_field`` records the matched path. Otherwise the
    field is ``"none"`` and downstream stages (S1–S4) decide the band.
    """
    matched_field, is_match = _detect_s0_match(anchor, candidate)
    anchor_namespace = _namespace_from_id(anchor.id)
    candidate_namespace = _namespace_from_id(candidate.id)

    return ResolutionEvidence(
        anchor_id=anchor.id,
        candidate_id=candidate.id,
        anchor_type=anchor.type,
        candidate_type=candidate.type,
        anchor_namespace=anchor_namespace,
        candidate_namespace=candidate_namespace,
        anchor_normalized=normalize_form(anchor.name),
        candidate_normalized=normalize_form(candidate.name),
        s0_matched_field=matched_field,
        band=ConfidenceBand.EXACT if is_match else ConfidenceBand.LOW,
        cross_namespace=anchor_namespace != candidate_namespace,
        cross_type=anchor.type != candidate.type,
    )
