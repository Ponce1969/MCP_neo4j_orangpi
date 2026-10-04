"""Pure T8c contract: direction inference, predicted census and fingerprint.

No ports, no graph: every test builds a legacy-shaped ``MergeLedgerEntry``
(pre-R1, ``direction=None`` on every inverse map) plus the edge observations a
planner reads from the live canonical, then asserts the pure domain output:

* the inference table (out / in / both / unknown / already-directed /
  MENTIONS untouched),
* the predicted census arithmetic and the mirror prediction (mirrors are only
  expected from ``both``/``unknown`` fallbacks),
* the fingerprint's stability (same plan -> same hash, changed direction ->
  different hash),
* the plan's entry copy carries the inferred directions without mutating the
  original entry.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

import pytest

from book_graph_rag.domain.merge_ledger_models import (
    EdgeInverseMap,
    MergeBand,
    MergeLedgerEntry,
)
from book_graph_rag.domain.rollback_plan_models import (
    PredictedCensus,
    RelatedEdgeObservation,
    RelatedEdgeProbe,
    build_entry_plan,
    build_rollback_plan,
    infer_related_direction,
)

_CANON = "graphrag-agentic:api-calls-tool"
_DUP = "agentic-patterns:api-calls-tool"
_OTHER = "graphrag-agentic:tool-invocation"
_CHUNK = "graphrag-agentic:chunk-7"


def _mentions() -> EdgeInverseMap:
    return EdgeInverseMap(
        edge_kind="MENTIONS",
        duplicate_entity_id=_DUP,
        original_other_endpoint_id=_CHUNK,
        edge_properties={"source_page": 3},
    )


def _related(
    other: str = _OTHER,
    *,
    direction: Literal["out", "in"] | None = None,
) -> EdgeInverseMap:
    return EdgeInverseMap(
        edge_kind="RELATED",
        duplicate_entity_id=_DUP,
        original_other_endpoint_id=other,
        edge_properties={"type": "requires", "source_page": 4},
        direction=direction,
    )


def _entry(edge_inverse_map: list[EdgeInverseMap]) -> MergeLedgerEntry:
    return MergeLedgerEntry(
        seq=305,
        candidate_ids=[_DUP],
        canonical_id=_CANON,
        band=MergeBand.EXACT,
        evidence=[],
        aliases_folded=[],
        edge_inverse_map=edge_inverse_map,
        approver="auto:bypass",
        applied_at=datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC),
    )


def _obs(index: int, a_to_b: bool, b_to_a: bool, *, seq: int = 305) -> RelatedEdgeObservation:
    """Observation with ``a`` = canonical and ``b`` = the other endpoint."""
    return RelatedEdgeObservation(
        seq=seq,
        map_index=index,
        probe=RelatedEdgeProbe(a_to_b=a_to_b, b_to_a=b_to_a),
    )


@pytest.mark.parametrize(
    ("canonical_to_other", "other_to_canonical", "expected"),
    [
        (True, False, "out"),
        (False, True, "in"),
        (True, True, "both"),
        (False, False, "unknown"),
    ],
)
def test_inference_table(
    canonical_to_other: bool,
    other_to_canonical: bool,
    expected: Literal["out", "in", "both", "unknown"],
) -> None:
    """The frozen re-point inference table (spec: dup->other became canon->other)."""
    verdict = infer_related_direction(
        canonical_to_other=canonical_to_other,
        other_to_canonical=other_to_canonical,
    )
    assert verdict == expected


def test_plan_fills_inferred_directions_without_mutating_the_original() -> None:
    """The plan carries an entry copy with directions; the original stays legacy."""
    entry = _entry([_mentions(), _related()])
    original_map = entry.edge_inverse_map

    entry_plan = build_entry_plan(entry, [_obs(1, a_to_b=True, b_to_a=False)])

    inferred_related = entry_plan.inferred_entry.edge_inverse_map[1]
    assert inferred_related.direction == "out"

    # The original entry is untouched: same list, same (None) direction.
    assert entry.edge_inverse_map is original_map
    assert entry.edge_inverse_map[1].direction is None
    assert entry_plan.inferred_entry.edge_inverse_map is not original_map
    assert entry_plan.inferred_entry.edge_inverse_map[1] is not original_map[1]


def test_plan_keeps_an_already_recorded_direction_untouched() -> None:
    """An entry that already carries a direction is reported but never rewritten."""
    entry = _entry([_related(direction="in")])
    # The live canonical shows the *other* orientation: stored direction wins.
    entry_plan = build_entry_plan(entry, [_obs(0, a_to_b=True, b_to_a=False)])

    inference = entry_plan.inferences[0]
    assert inference.verdict == "out"
    assert inference.applied_direction == "in"
    assert inference.fallback is False
    assert entry_plan.inferred_entry.edge_inverse_map[0].direction == "in"


def test_mentions_entries_are_never_directional() -> None:
    """MENTIONS stays direction-less and never produces an inference row."""
    entry = _entry([_mentions(), _related()])

    entry_plan = build_entry_plan(
        entry,
        [_obs(1, a_to_b=False, b_to_a=True)],
    )

    assert [inf.map_index for inf in entry_plan.inferences] == [1]
    assert entry_plan.inferred_entry.edge_inverse_map[0].direction is None
    assert entry_plan.inferred_entry.edge_inverse_map[1].direction == "in"


def test_predicted_census_arithmetic() -> None:
    """1 MENTIONS + 5 RELATED (out, in, both, unknown, already-directed)."""
    entry = _entry(
        [
            _mentions(),
            _related("graphrag-agentic:other-a"),
            _related("graphrag-agentic:other-b"),
            _related("graphrag-agentic:other-c"),
            _related("graphrag-agentic:other-d"),
            _related("graphrag-agentic:other-e", direction="in"),
        ]
    )
    observations = [
        _obs(1, a_to_b=True, b_to_a=False),  # out
        _obs(2, a_to_b=False, b_to_a=True),  # in
        _obs(3, a_to_b=True, b_to_a=True),  # both -> fallback
        _obs(4, a_to_b=False, b_to_a=False),  # unknown -> fallback
        _obs(5, a_to_b=True, b_to_a=False),  # stored "in" wins over verdict "out"
    ]

    entry_plan = build_entry_plan(entry, observations)

    assert entry_plan.predicted == PredictedCensus(
        mentions_restored=1,
        # out 1 + in 1 + stored-in 1 + both 2 + unknown 2
        related_restored=7,
        directions_inferred=3,
        fallback_both=1,
        fallback_unknown=1,
        predicted_mirrors=2,
    )

    batch = build_rollback_plan([entry_plan])
    assert batch.predicted == entry_plan.predicted


def test_mirror_prediction_is_zero_when_every_related_entry_is_inferred() -> None:
    """No fallback -> no predicted mirror (the legacy both-ways path never runs)."""
    entry = _entry([_mentions(), _related(), _related("graphrag-agentic:other-b")])

    entry_plan = build_entry_plan(
        entry,
        [
            _obs(1, a_to_b=True, b_to_a=False),
            _obs(2, a_to_b=False, b_to_a=True),
        ],
    )

    assert entry_plan.predicted.fallback_both == 0
    assert entry_plan.predicted.fallback_unknown == 0
    assert entry_plan.predicted.predicted_mirrors == 0
    assert entry_plan.predicted.related_restored == 2


def test_mirror_prediction_counts_one_mirror_per_fallback() -> None:
    """N fallbacks (both/unknown) -> N predicted mirrors, 2 restored edges each."""
    entry = _entry(
        [
            _related("graphrag-agentic:other-a"),
            _related("graphrag-agentic:other-b"),
            _related("graphrag-agentic:other-c"),
        ]
    )
    observations = [
        _obs(0, a_to_b=True, b_to_a=True),  # both
        _obs(1, a_to_b=False, b_to_a=False),  # unknown
        _obs(2, a_to_b=False, b_to_a=True),  # in (inferred)
    ]

    entry_plan = build_entry_plan(entry, observations)

    assert entry_plan.predicted.fallback_both == 1
    assert entry_plan.predicted.fallback_unknown == 1
    assert entry_plan.predicted.predicted_mirrors == 2
    assert entry_plan.predicted.directions_inferred == 1
    assert entry_plan.predicted.related_restored == 2 + 2 + 1


def test_fallback_reasons_name_the_legacy_behaviour() -> None:
    """Honesty: every both/unknown fallback states why and what legacy will do."""
    entry = _entry(
        [
            _related("graphrag-agentic:other-a"),
            _related("graphrag-agentic:other-b"),
        ]
    )

    entry_plan = build_entry_plan(
        entry,
        [
            _obs(0, a_to_b=True, b_to_a=True),
            _obs(1, a_to_b=False, b_to_a=False),
        ],
    )

    for inference in entry_plan.inferences:
        assert inference.fallback is True
        assert inference.applied_direction is None
        assert "legacy both-ways restore" in inference.reason
        assert "mirror" in inference.reason


def test_fingerprint_is_stable_and_direction_sensitive() -> None:
    """Same plan -> same sha256; a changed direction -> different sha256."""
    entry = _entry([_mentions(), _related()])
    observations = [_obs(1, a_to_b=True, b_to_a=False)]

    first = build_rollback_plan([build_entry_plan(entry, observations)])
    second = build_rollback_plan([build_entry_plan(entry, observations)])
    assert first.fingerprint == second.fingerprint
    assert len(first.fingerprint) == 64

    # Same plan, opposite observation: direction flips -> hash must change.
    flipped = build_rollback_plan([build_entry_plan(entry, [_obs(1, a_to_b=False, b_to_a=True)])])
    assert flipped.fingerprint != first.fingerprint
    assert flipped.plans[0].inferred_entry.edge_inverse_map[1].direction == "in"
    assert first.plans[0].inferred_entry.edge_inverse_map[1].direction == "out"
