"""S3 context validation scoring (Slice D).

Pure functions that measure relationship and description overlap between an
anchor and a candidate entity. No model, no I/O, no randomness.
"""

from __future__ import annotations

import re
import unicodedata

from book_graph_rag.domain.resolution_models import S3ContextSignals


def _desc_tokens(text: str) -> set[str]:
    """Return a deterministic token set for a description.

    Tokens are Unicode word characters after NFKC normalization and casefolding.
    Punctuation and spacing collapse naturally, making the overlap robust to
    minor formatting differences.
    """
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return set(re.findall(r"\w+", normalized, flags=re.UNICODE))


def mentions_jaccard(
    anchor_source_ids: set[str],
    candidate_source_ids: set[str],
) -> tuple[float, int, int]:
    """Jaccard overlap of the sets of source ids that mention each entity.

    Returns ``(jaccard, shared_count, union_count)``. Empty sets produce
    ``(0.0, 0, 0)`` so downstream math stays well-defined.
    """
    shared = anchor_source_ids & candidate_source_ids
    union = anchor_source_ids | candidate_source_ids
    if not union:
        return 0.0, 0, 0
    return len(shared) / len(union), len(shared), len(union)


def related_jaccard(
    anchor_neighbor_ids: set[str],
    candidate_neighbor_ids: set[str],
) -> tuple[float, int, int]:
    """Jaccard overlap of the sets of entity ids related to each entity.

    Returns ``(jaccard, shared_count, union_count)``.
    """
    shared = anchor_neighbor_ids & candidate_neighbor_ids
    union = anchor_neighbor_ids | candidate_neighbor_ids
    if not union:
        return 0.0, 0, 0
    return len(shared) / len(union), len(shared), len(union)


def description_overlap(anchor_desc: str, candidate_desc: str) -> float:
    """Token-set Jaccard overlap between two descriptions.

    Empty descriptions have zero overlap. Identical normalized descriptions
    have perfect overlap.
    """
    anchor_tokens = _desc_tokens(anchor_desc)
    candidate_tokens = _desc_tokens(candidate_desc)
    union = anchor_tokens | candidate_tokens
    if not union:
        return 0.0
    shared = anchor_tokens & candidate_tokens
    return len(shared) / len(union)


def s3_context_score(
    mentions_j: float,
    related_j: float,
    desc_o: float,
    cosine: float,
    *,
    mentions_source_count: int = 0,
    mentions_shared_count: int = 0,
    related_neighbor_count: int = 0,
    related_shared_count: int = 0,
) -> S3ContextSignals:
    """Build the composite S3 signal from the three individual overlaps.

    The composite score is the arithmetic mean of the three overlap scores.
    A conflict flag is raised when the composite score is below 0.1 while the
    embedding cosine is at least 0.80 — a strong embedding signal with no
    supporting context is treated as a warning sign (R4.1 / design §1.4).
    """
    composite = (mentions_j + related_j + desc_o) / 3.0
    conflict_flag = composite < 0.1 and cosine >= 0.80
    return S3ContextSignals(
        mentions_jaccard=mentions_j,
        related_jaccard=related_j,
        description_overlap=desc_o,
        mentions_source_count=mentions_source_count,
        mentions_shared_count=mentions_shared_count,
        related_neighbor_count=related_neighbor_count,
        related_shared_count=related_shared_count,
        conflict_flag=conflict_flag,
    )
