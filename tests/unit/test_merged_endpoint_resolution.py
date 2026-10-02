"""TDD: transitive canonical resolution and merged-endpoint repoint planning.

Behaviour under test: an edge whose endpoint carries ``merged_into`` must be
re-pointed onto the *terminal* canonical of the chain, anomalies (cycles and
missing targets) must be excluded rather than guessed, and only RELATED edges
that collapse onto themselves may be deleted. MENTIONS are never deleted.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from book_graph_rag.domain.merged_endpoint_resolution import (
    DanglingEdge,
    EdgeAction,
    ExcludedEntry,
    RepointPlan,
    ResolutionState,
    plan_repoint,
    resolve_canonical,
)

NS = "knowledge:essential-graphrag"


def _entity(slug: str) -> str:
    return f"{NS}:{slug}"


def _mentions(target: str, *, chunk: str = "doc-7:chunk-17") -> DanglingEdge:
    return DanglingEdge(
        kind="MENTIONS",
        source_id=chunk,
        target_id=target,
        properties={"chunk_index": 17},
    )


def _related(
    source: str,
    target: str,
    relation_type: str = "enables",
) -> DanglingEdge:
    return DanglingEdge(
        kind="RELATED",
        source_id=source,
        target_id=target,
        relation_type=relation_type,
        properties={"type": relation_type, "chunk_index": 3, "source_page": 12},
    )


# ── resolve_canonical ─────────────────────────────────────────────────────


def test_resolve_single_step_returns_the_terminal_canonical() -> None:
    duplicate, canonical = _entity("dup-a"), _entity("canon-b")

    result = resolve_canonical(
        duplicate,
        merged_into={duplicate: canonical},
        known_ids={canonical},
    )

    assert result.state is ResolutionState.RESOLVED
    assert result.start_id == duplicate
    assert result.canonical_id == canonical
    assert result.chain == (duplicate, canonical)


def test_resolve_walks_a_multi_step_chain_transitively() -> None:
    first, middle, terminal = _entity("dup-a"), _entity("dup-b"), _entity("canon-c")

    result = resolve_canonical(
        first,
        merged_into={first: middle, middle: terminal},
        known_ids={terminal},
    )

    assert result.state is ResolutionState.RESOLVED
    assert result.canonical_id == terminal
    assert result.chain == (first, middle, terminal)


def test_resolve_detects_a_self_referential_cycle() -> None:
    entity = _entity("dup-a")

    result = resolve_canonical(
        entity,
        merged_into={entity: entity},
        known_ids={entity},
    )

    assert result.state is ResolutionState.CYCLE
    assert result.canonical_id is None
    assert result.chain == (entity, entity)


def test_resolve_detects_a_two_node_cycle() -> None:
    a, b = _entity("dup-a"), _entity("dup-b")

    result = resolve_canonical(a, merged_into={a: b, b: a}, known_ids={a, b})

    assert result.state is ResolutionState.CYCLE
    assert result.canonical_id is None
    assert result.chain == (a, b, a)


def test_resolve_detects_a_three_node_cycle() -> None:
    a, b, c = _entity("dup-a"), _entity("dup-b"), _entity("dup-c")

    result = resolve_canonical(a, merged_into={a: b, b: c, c: a}, known_ids={a, b, c})

    assert result.state is ResolutionState.CYCLE
    assert result.canonical_id is None
    assert result.chain == (a, b, c, a)


def test_resolve_reports_a_missing_terminal_target() -> None:
    duplicate, ghost = _entity("dup-a"), _entity("ghost")

    result = resolve_canonical(
        duplicate,
        merged_into={duplicate: ghost},
        known_ids=set(),
    )

    assert result.state is ResolutionState.MISSING_TARGET
    assert result.canonical_id is None
    assert result.chain == (duplicate, ghost)


def test_resolve_accepts_a_terminal_id_that_exists_but_is_not_merged() -> None:
    duplicate, canonical = _entity("dup-a"), _entity("canon-b")

    result = resolve_canonical(
        duplicate,
        merged_into={duplicate: canonical},
        known_ids={duplicate, canonical},
    )

    assert result.state is ResolutionState.RESOLVED
    assert result.canonical_id == canonical
    assert canonical not in {duplicate}  # terminal is not itself a merge key


def test_resolve_returns_the_id_itself_when_it_is_not_a_merge_key() -> None:
    entity = _entity("live")

    result = resolve_canonical(entity, merged_into={}, known_ids={entity})

    assert result.state is ResolutionState.RESOLVED
    assert result.canonical_id == entity
    assert result.chain == (entity,)


# ── plan_repoint: MENTIONS ────────────────────────────────────────────────


def test_plan_repoints_a_single_step_mentions_edge_and_never_deletes_it() -> None:
    duplicate, canonical = _entity("dup-a"), _entity("canon-b")

    plan = plan_repoint(
        [_mentions(duplicate)],
        merged_into={duplicate: canonical},
        known_ids={canonical},
    )

    assert plan.excluded == ()
    assert plan.collapse_count == 0
    entry = plan.entries[0]
    assert entry.action is EdgeAction.REPOINT
    assert entry.edge.source_id == "doc-7:chunk-17"
    assert entry.new_source_id == "doc-7:chunk-17"
    assert entry.new_target_id == canonical


def test_plan_walks_a_multi_step_chain_for_a_related_edge() -> None:
    first, middle, terminal = _entity("dup-a"), _entity("dup-b"), _entity("canon-c")

    plan = plan_repoint(
        [_related(first, _entity("live"))],
        merged_into={first: middle, middle: terminal},
        known_ids={terminal},
    )

    entry = plan.entries[0]
    assert entry.action is EdgeAction.REPOINT
    assert entry.new_source_id == terminal
    assert entry.new_target_id == _entity("live")


# ── plan_repoint: RELATED ─────────────────────────────────────────────────


def test_plan_deletes_a_related_edge_that_collapses_onto_itself() -> None:
    duplicate, canonical = _entity("dup-a"), _entity("canon-b")

    plan = plan_repoint(
        [_related(canonical, duplicate)],
        merged_into={duplicate: canonical},
        known_ids={canonical},
    )

    entry = plan.entries[0]
    assert entry.action is EdgeAction.DELETE_COLLAPSE
    assert entry.new_source_id == canonical
    assert entry.new_target_id == canonical
    assert plan.collapse_count == 1


def test_plan_repoints_both_merged_endpoints_onto_different_canonicals() -> None:
    x, cx, y, cy = _entity("dup-x"), _entity("canon-x"), _entity("dup-y"), _entity("canon-y")

    plan = plan_repoint(
        [_related(x, y, relation_type="requires")],
        merged_into={x: cx, y: cy},
        known_ids={cx, cy},
    )

    entry = plan.entries[0]
    assert entry.action is EdgeAction.REPOINT
    assert entry.new_source_id == cx
    assert entry.new_target_id == cy
    assert entry.edge.relation_type == "requires"


def test_plan_keeps_the_direction_of_an_in_edge_whose_target_is_merged() -> None:
    duplicate, canonical, other = _entity("dup-a"), _entity("canon-b"), _entity("live-c")

    plan = plan_repoint(
        [_related(other, duplicate)],
        merged_into={duplicate: canonical},
        known_ids={canonical},
    )

    entry = plan.entries[0]
    assert entry.action is EdgeAction.REPOINT
    assert entry.new_source_id == other
    assert entry.new_target_id == canonical


def test_plan_flips_nothing_when_the_source_is_the_merged_endpoint() -> None:
    duplicate, canonical, other = _entity("dup-a"), _entity("canon-b"), _entity("live-c")

    plan = plan_repoint(
        [_related(duplicate, other)],
        merged_into={duplicate: canonical},
        known_ids={canonical},
    )

    entry = plan.entries[0]
    assert entry.action is EdgeAction.REPOINT
    assert entry.new_source_id == canonical
    assert entry.new_target_id == other


# ── plan_repoint: exclusions (D3) ─────────────────────────────────────────


def test_plan_excludes_edges_touching_a_cycle_and_keeps_them_out_of_entries() -> None:
    a, b = _entity("dup-a"), _entity("dup-b")
    live = _entity("live")

    plan = plan_repoint(
        [_related(a, live), _mentions(b)],
        merged_into={a: b, b: a},
        known_ids={a, b},
    )

    assert plan.entries == ()
    assert len(plan.excluded) == 2
    reasons = {
        (entry.edge.source_id, entry.edge.target_id): entry.reason for entry in plan.excluded
    }
    assert reasons[(_related(a, live).source_id, live)] is ResolutionState.CYCLE
    assert reasons[(_mentions(b).source_id, b)] is ResolutionState.CYCLE


def test_plan_excludes_edges_whose_chain_ends_on_a_missing_target() -> None:
    duplicate, ghost = _entity("dup-a"), _entity("ghost")

    plan = plan_repoint(
        [_mentions(duplicate)],
        merged_into={duplicate: ghost},
        known_ids=set(),
    )

    assert plan.entries == ()
    excluded: ExcludedEntry = plan.excluded[0]
    assert excluded.reason is ResolutionState.MISSING_TARGET
    assert excluded.edge.target_id == duplicate


def test_resolve_reports_a_cycle_reached_from_outside_the_cycle() -> None:
    start, a, b = _entity("dup-start"), _entity("dup-a"), _entity("dup-b")

    result = resolve_canonical(
        start,
        merged_into={start: a, a: b, b: a},
        known_ids={a, b},
    )

    assert result.state is ResolutionState.CYCLE
    assert result.canonical_id is None
    assert result.chain == (start, a, b, a)


def test_plan_reports_the_source_anomaly_first_when_both_endpoints_fail() -> None:
    cycled, other_cycled = _entity("dup-a"), _entity("dup-b")
    ghosted, gone = _entity("ghost"), _entity("gone")

    plan = plan_repoint(
        [_related(cycled, ghosted)],
        merged_into={cycled: other_cycled, other_cycled: cycled, ghosted: gone},
        known_ids={cycled, other_cycled},
    )

    assert plan.entries == ()
    assert plan.excluded[0].reason is ResolutionState.CYCLE


# ── merge key accounting ──────────────────────────────────────────────────


def test_plan_counts_distinct_merge_keys_when_two_edges_converge() -> None:
    d1, d2, canonical, live = (
        _entity("dup-a"),
        _entity("dup-b"),
        _entity("canon-c"),
        _entity("live-d"),
    )

    plan = plan_repoint(
        [_related(d1, live), _related(d2, live)],
        merged_into={d1: canonical, d2: canonical},
        known_ids={canonical},
    )

    assert plan.repoint_count == 2
    assert plan.merge_key_count == 1
    assert plan.merge_collapsed == 1
    assert plan.collapse_count == 0


def test_plan_merge_keys_differ_across_relation_types() -> None:
    duplicate, canonical, live = _entity("dup-a"), _entity("canon-b"), _entity("live-c")

    plan = plan_repoint(
        [_related(duplicate, live, relation_type="enables"), _related(duplicate, live, "requires")],
        merged_into={duplicate: canonical},
        known_ids={canonical},
    )

    assert plan.repoint_count == 2
    assert plan.merge_key_count == 2
    assert plan.merge_collapsed == 0


def test_plan_of_an_empty_input_is_an_empty_plan() -> None:
    plan = plan_repoint([], merged_into={}, known_ids=set())

    assert plan.entries == ()
    assert plan.excluded == ()
    assert plan.repoint_count == 0
    assert plan.collapse_count == 0
    assert plan.merge_key_count == 0
    assert plan.merge_collapsed == 0


def test_plan_dump_is_json_serializable_and_carries_the_edge_payloads() -> None:
    d1, d2, canonical, live = (
        _entity("dup-a"),
        _entity("dup-b"),
        _entity("canon-c"),
        _entity("live-d"),
    )

    plan = plan_repoint(
        [_related(d1, live), _related(d2, live), _related(canonical, d1)],
        merged_into={d1: canonical, d2: canonical},
        known_ids={canonical},
    )

    payload = json.loads(plan.model_dump_json())
    assert plan.repoint_count == 2
    assert plan.collapse_count == 1
    assert plan.merge_key_count == 1
    assert plan.merge_collapsed == 1
    assert payload["entries"][0]["edge"]["properties"]["source_page"] == 12
    assert payload["excluded"] == []


def test_plan_model_is_frozen() -> None:
    plan = RepointPlan(entries=(), excluded=())

    with pytest.raises(ValidationError):
        plan.entries = ()
