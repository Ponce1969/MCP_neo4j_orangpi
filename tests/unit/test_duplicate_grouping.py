"""TDD: canonical policy and group planning for intra-namespace entity resolution.

Behaviour under test: the plan that feeds ``ApplyMergeUseCase`` must be derived
from the audit's logical-duplicate rule (exact ``name`` + ``type`` inside one
namespace) and must choose the canonical entity deterministically.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from book_graph_rag.domain.duplicate_grouping import (
    DuplicateMemberRow,
    choose_canonical_id,
    choose_canonical_id_by_richness,
    plan_intra_resolution,
    to_entity_type,
)

NS = "knowledge:ai-engineering-huyen"


def test_choose_canonical_prefers_the_shortest_id() -> None:
    ids = (
        f"{NS}:large-language-model-concept",
        f"{NS}:modelo-de-lenguaje-grande-concept",
        f"{NS}:llm-concept",
    )

    assert choose_canonical_id(ids) == f"{NS}:llm-concept"


def test_choose_canonical_breaks_length_ties_lexicographically() -> None:
    ids = (f"{NS}:rag-pattern", f"{NS}:cpu-pattern")

    assert choose_canonical_id(ids) == f"{NS}:cpu-pattern"


def test_choose_canonical_rejects_an_empty_id_set() -> None:
    with pytest.raises(ValueError, match="empty"):
        choose_canonical_id(())


def test_plan_skips_rows_with_a_single_member() -> None:
    rows = [DuplicateMemberRow(name="LLM", kind="concept", entity_ids=(f"{NS}:llm-concept",))]

    assert plan_intra_resolution(rows) == []


def test_plan_builds_the_group_from_the_canonical_policy() -> None:
    rows = [
        DuplicateMemberRow(
            name="LLM",
            kind="concept",
            entity_ids=(
                f"{NS}:large-language-model-concept",
                f"{NS}:llm-concept",
                f"{NS}:modelo-de-lenguaje-grande-concept",
            ),
        )
    ]

    plan = plan_intra_resolution(rows)

    assert len(plan) == 1
    group = plan[0]
    assert group.canonical_id == f"{NS}:llm-concept"
    assert group.duplicate_ids == (
        f"{NS}:large-language-model-concept",
        f"{NS}:modelo-de-lenguaje-grande-concept",
    )
    assert group.name == "LLM"
    assert group.kind == "concept"


def test_plan_dedupes_repeated_ids_and_skips_the_resulting_singleton() -> None:
    rows = [
        DuplicateMemberRow(
            name="LLM",
            kind="concept",
            entity_ids=(f"{NS}:llm-concept", f"{NS}:llm-concept"),
        )
    ]

    assert plan_intra_resolution(rows) == []


def test_plan_orders_groups_by_descending_size_then_name() -> None:
    rows = [
        DuplicateMemberRow(
            name="zeta", kind="concept", entity_ids=(f"{NS}:b-concept", f"{NS}:c-concept")
        ),
        DuplicateMemberRow(
            name="alpha",
            kind="tool",
            entity_ids=(f"{NS}:a-tool", f"{NS}:bb-tool", f"{NS}:cc-tool"),
        ),
        DuplicateMemberRow(
            name="beta", kind="concept", entity_ids=(f"{NS}:a-concept", f"{NS}:b2-concept")
        ),
    ]

    plan = plan_intra_resolution(rows)

    assert [(g.name, len(g.duplicate_ids)) for g in plan] == [
        ("alpha", 2),
        ("beta", 1),
        ("zeta", 1),
    ]


def test_plan_rejects_blank_entity_ids() -> None:
    with pytest.raises(ValueError, match="blank"):
        DuplicateMemberRow(name="LLM", kind="concept", entity_ids=("", f"{NS}:llm-concept"))


def test_row_rejects_a_kind_outside_the_entity_type_contract() -> None:
    with pytest.raises(ValidationError):
        DuplicateMemberRow(name="LLM", kind="banana", entity_ids=(f"{NS}:llm-concept",))  # type: ignore[arg-type]


def test_to_entity_type_narrows_a_supported_kind() -> None:
    assert to_entity_type("concept") == "concept"


def test_to_entity_type_rejects_an_unsupported_kind() -> None:
    with pytest.raises(ValueError, match="unsupported entity type"):
        to_entity_type("banana")


def test_plan_is_deterministic_for_unsorted_input() -> None:
    ids = (f"{NS}:mmm-concept", f"{NS}:aaa-concept", f"{NS}:bb-concept")
    forward = DuplicateMemberRow(name="x", kind="concept", entity_ids=ids)
    backward = DuplicateMemberRow(name="x", kind="concept", entity_ids=tuple(reversed(ids)))

    assert plan_intra_resolution([forward]) == plan_intra_resolution([backward])


# ── T10-B: canonical by richness (mentions > degree > shortest > lexicographic) ──
# Scores are always ``(mentions, related degree)``.


def test_richness_ranks_mentions_before_related_degree() -> None:
    """Most mentions wins even against a much higher RELATED degree."""
    scores = {f"{NS}:b-concept": (5, 1), f"{NS}:a-concept": (2, 90)}

    assert choose_canonical_id_by_richness(scores) == f"{NS}:b-concept"


def test_richness_breaks_a_mentions_tie_with_the_related_degree() -> None:
    scores = {f"{NS}:a-concept": (4, 3), f"{NS}:b-concept": (4, 17)}

    assert choose_canonical_id_by_richness(scores) == f"{NS}:b-concept"


def test_richness_falls_back_to_the_shortest_id_on_equal_scores() -> None:
    scores = {
        f"{NS}:large-language-model-concept": (6, 12),
        f"{NS}:llm-concept": (6, 12),
    }

    assert choose_canonical_id_by_richness(scores) == f"{NS}:llm-concept"


def test_richness_falls_back_to_lexicographic_when_lengths_tie() -> None:
    scores = {
        f"{NS}:model-concept": (6, 12),
        f"{NS}:agent-concept": (6, 12),
    }

    assert choose_canonical_id_by_richness(scores) == f"{NS}:agent-concept"


def test_richness_rejects_an_empty_score_map() -> None:
    with pytest.raises(ValueError, match="empty"):
        choose_canonical_id_by_richness({})


# ── T10-B: the measured production disagreements (20 of 64 groups) ─────────────
# ``preference-fine-tuning-concept`` deg 4 / 1 mention LOSES to
# ``ajuste-fino-de-preferencias-concept`` deg 19 / 7; the historical
# shortest-id rule picks the loser in every one of the three pairs.


def test_richness_disagrees_with_shortest_id_on_preference_fine_tuning() -> None:
    english = f"{NS}:preference-fine-tuning-concept"
    spanish = f"{NS}:ajuste-fino-de-preferencias-concept"
    scores = {english: (1, 4), spanish: (7, 19)}

    assert choose_canonical_id([english, spanish]) == english
    assert choose_canonical_id_by_richness(scores) == spanish


def test_richness_disagrees_with_shortest_id_on_supervised_fine_tuning() -> None:
    english = f"{NS}:supervised-fine-tuning-concept"
    spanish = f"{NS}:ajuste-fino-supervisado-concept"
    scores = {english: (5, 11), spanish: (8, 20)}

    assert choose_canonical_id([english, spanish]) == english
    assert choose_canonical_id_by_richness(scores) == spanish


def test_richness_breaks_the_few_shot_mentions_tie_with_degree() -> None:
    english = f"{NS}:few-shot-learning-pattern"
    spanish = f"{NS}:aprendizaje-few-shot-pattern"
    scores = {english: (1, 3), spanish: (1, 8)}

    assert choose_canonical_id([english, spanish]) == english
    assert choose_canonical_id_by_richness(scores) == spanish


# ── T10-B: plan_intra_resolution(..., scores=...) ─────────────────────────────


def _preference_row() -> DuplicateMemberRow:
    return DuplicateMemberRow(
        name="ajuste fino de preferencias",
        kind="concept",
        entity_ids=(
            f"{NS}:preference-fine-tuning-concept",
            f"{NS}:ajuste-fino-de-preferencias-concept",
        ),
    )


def test_plan_without_scores_keeps_the_historical_shortest_id_canonical() -> None:
    plan = plan_intra_resolution([_preference_row()])

    assert plan[0].canonical_id == f"{NS}:preference-fine-tuning-concept"
    assert plan[0].duplicate_ids == (f"{NS}:ajuste-fino-de-preferencias-concept",)


def test_plan_uses_the_richest_member_when_scores_are_provided() -> None:
    scores = {
        f"{NS}:preference-fine-tuning-concept": (1, 4),
        f"{NS}:ajuste-fino-de-preferencias-concept": (7, 19),
    }

    plan = plan_intra_resolution([_preference_row()], scores=scores)

    assert plan[0].canonical_id == f"{NS}:ajuste-fino-de-preferencias-concept"
    assert plan[0].duplicate_ids == (f"{NS}:preference-fine-tuning-concept",)


def test_plan_with_scores_fails_closed_when_a_member_has_no_score() -> None:
    scores = {f"{NS}:preference-fine-tuning-concept": (1, 4)}

    with pytest.raises(ValueError, match="missing"):
        plan_intra_resolution([_preference_row()], scores=scores)
