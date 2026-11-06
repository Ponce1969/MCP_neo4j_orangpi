"""Unit tests for the per-intent tuning table (T6, specs S3 and S13).

The table is asserted against S13's exact values — the maintainer's verbatim
k numbers (3 / 5 / 4), the traversal flags and the three citation styles —
and against completeness over the ``Intent`` enum so a future intent cannot
be silently forgotten.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from book_graph_rag.domain.intent_tuning import (
    INTENT_TUNING_TABLE,
    CitationStyle,
    IntentTuning,
    tuning_for,
)
from book_graph_rag.domain.orchestration_models import Intent


def test_foundation_tuning_matches_s13() -> None:
    """S13: foundation → k=3, no relationship traversal, literal chunk_id citation."""
    tuning = tuning_for(Intent.FOUNDATION)
    assert tuning is not None
    assert tuning.k == 3
    assert tuning.traverses_relationships is False
    assert tuning.citation_style is CitationStyle.LITERAL_CHUNK


def test_design_tuning_matches_s13() -> None:
    """S13: design → k=5, traverses relationships (grafo + pasajes), concept + reference."""
    tuning = tuning_for(Intent.DESIGN)
    assert tuning is not None
    assert tuning.k == 5
    assert tuning.traverses_relationships is True
    assert tuning.citation_style is CitationStyle.CONCEPT_REFERENCE


def test_implementation_tuning_matches_s13() -> None:
    """S13: implementation → k=4, no traversal ("solo código" = citation style only)."""
    tuning = tuning_for(Intent.IMPLEMENTATION)
    assert tuning is not None
    assert tuning.k == 4
    assert tuning.traverses_relationships is False
    assert tuning.citation_style is CitationStyle.CODE_WITH_CHUNK


def test_tuning_for_none_returns_none() -> None:
    """S13 defines no row for "no intent": no default is invented (L7)."""
    assert tuning_for(None) is None


def test_three_intents_yield_three_distinct_tunings() -> None:
    """No aliasing: three separate instances carrying three separate value triples."""
    foundation = tuning_for(Intent.FOUNDATION)
    design = tuning_for(Intent.DESIGN)
    implementation = tuning_for(Intent.IMPLEMENTATION)
    assert foundation is not None
    assert design is not None
    assert implementation is not None
    triples = {
        (foundation.k, foundation.traverses_relationships, foundation.citation_style),
        (design.k, design.traverses_relationships, design.citation_style),
        (implementation.k, implementation.traverses_relationships, implementation.citation_style),
    }
    assert len(triples) == 3
    assert foundation is not design
    assert design is not implementation
    assert foundation is not implementation


@pytest.mark.parametrize("bad_k", [0, -1, -10], ids=["zero", "minus_one", "minus_ten"])
def test_k_rejects_zero_and_negative_values(bad_k: int) -> None:
    """S13's k is a positive top-k: 0 and negatives are rejected, not coerced."""
    with pytest.raises(ValidationError, match="k"):
        IntentTuning(
            k=bad_k,
            traverses_relationships=False,
            citation_style=CitationStyle.LITERAL_CHUNK,
        )


def test_intent_tuning_is_frozen() -> None:
    """S13's table is data: mutating a delivered tuning must raise."""
    tuning = tuning_for(Intent.DESIGN)
    assert tuning is not None
    with pytest.raises(ValidationError, match="frozen"):
        tuning.k = 99  # pydantic frozen: runtime refusal


def test_table_covers_every_intent_member_and_nothing_else() -> None:
    """S3/S13: every Intent member has exactly one row; no extra keys exist."""
    for intent in Intent:
        assert intent in INTENT_TUNING_TABLE
        assert tuning_for(intent) is INTENT_TUNING_TABLE[intent]
    assert set(INTENT_TUNING_TABLE) == set(Intent)
