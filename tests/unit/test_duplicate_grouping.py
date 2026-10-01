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
