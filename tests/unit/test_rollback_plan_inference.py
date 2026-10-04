"""Pure T8c contract: direction inference, predicted census and fingerprint.

No ports, no graph: every test builds a legacy-shaped ``MergeLedgerEntry``
(pre-R1, ``direction=None`` on every inverse map) plus the edge observations a
planner reads from the live canonical, then asserts the pure domain output:

* the inference table (out / in / both / unknown / already-directed /
  MENTIONS untouched),
* the provenance rule — the second, provenance-based inference that runs only
  for geometric ``both``/``unknown`` verdicts and matches the loser's captured
  ``chunk_index``/``source_page`` against the live edges' properties,
* the predicted census arithmetic and the mirror prediction (mirrors are only
  expected from ``both``/``unknown`` fallbacks),
* the fingerprint's stability (same plan -> same hash, changed direction ->
  different hash),
* the plan's entry copy carries the inferred directions without mutating the
  original entry.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

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
    chunk_index: int | None = None,
) -> EdgeInverseMap:
    """A RELATED inverse entry; ``chunk_index`` is the loser's captured provenance."""
    edge_properties: dict[str, Any] = {"type": "requires", "source_page": 4}
    if chunk_index is not None:
        edge_properties["chunk_index"] = chunk_index
    return EdgeInverseMap(
        edge_kind="RELATED",
        duplicate_entity_id=_DUP,
        original_other_endpoint_id=other,
        edge_properties=edge_properties,
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


_LOSER_CHUNK = 12


def _live(chunk_index: int, source_page: int = 4) -> dict[str, Any]:
    """One live canonical edge as the port probe returns it (properties(r))."""
    return {"type": "requires", "chunk_index": chunk_index, "source_page": source_page}


def _obs_edges(
    index: int,
    a_edges: list[dict[str, Any]],
    b_edges: list[dict[str, Any]],
    *,
    seq: int = 305,
) -> RelatedEdgeObservation:
    """Probe carrying the live edges' properties per direction (provenance input)."""
    return RelatedEdgeObservation(
        seq=seq,
        map_index=index,
        probe=RelatedEdgeProbe(
            a_to_b=bool(a_edges),
            b_to_a=bool(b_edges),
            a_to_b_edges=a_edges,
            b_to_a_edges=b_edges,
        ),
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


# --- Provenance rule (second inference, geometric both/unknown only) ----------


def test_provenance_resolves_out_when_only_the_forward_edge_carries_the_chunk() -> None:
    """canonical->other carries the loser's chunk_index: original was dup -> other."""
    entry = _entry([_related(chunk_index=_LOSER_CHUNK)])

    entry_plan = build_entry_plan(
        entry,
        [_obs_edges(0, [_live(_LOSER_CHUNK)], [_live(99)])],
    )

    inference = entry_plan.inferences[0]
    assert inference.verdict == "out"
    assert inference.rule == "provenance"
    assert inference.applied_direction == "out"
    assert inference.fallback is False
    assert str(_LOSER_CHUNK) in inference.reason
    assert entry_plan.inferred_entry.edge_inverse_map[0].direction == "out"
    assert entry_plan.predicted == PredictedCensus(
        mentions_restored=0,
        related_restored=1,
        directions_inferred=1,
        fallback_both=0,
        fallback_unknown=0,
        predicted_mirrors=0,
    )


def test_provenance_resolves_in_when_only_the_reverse_edge_carries_the_chunk() -> None:
    """other->canonical carries the loser's chunk_index: original was other -> dup."""
    entry = _entry([_related(chunk_index=_LOSER_CHUNK)])

    entry_plan = build_entry_plan(
        entry,
        [_obs_edges(0, [_live(99)], [_live(_LOSER_CHUNK)])],
    )

    inference = entry_plan.inferences[0]
    assert inference.verdict == "in"
    assert inference.rule == "provenance"
    assert inference.applied_direction == "in"
    assert inference.fallback is False
    assert entry_plan.inferred_entry.edge_inverse_map[0].direction == "in"
    assert entry_plan.predicted.predicted_mirrors == 0
    assert entry_plan.predicted.directions_inferred == 1


def test_provenance_keeps_both_fallback_when_a_collapsed_merge_carries_both_sides() -> None:
    """Both live directions carry the loser's identity: verdict stays ``both``."""
    entry = _entry([_related(chunk_index=_LOSER_CHUNK)])

    entry_plan = build_entry_plan(
        entry,
        [_obs_edges(0, [_live(_LOSER_CHUNK)], [_live(_LOSER_CHUNK)])],
    )

    inference = entry_plan.inferences[0]
    assert inference.verdict == "both"
    assert inference.applied_direction is None
    assert inference.fallback is True
    assert inference.rule == "geometric"
    assert "collapsed MERGE" in inference.reason
    assert "legacy both-ways restore" in inference.reason
    assert entry_plan.predicted.predicted_mirrors == 1


def test_provenance_keeps_unknown_fallback_when_no_live_edge_carries_the_chunk() -> None:
    """Geometric unknown (edge gone): provenance runs and confirms no carrier."""
    entry = _entry([_related(chunk_index=_LOSER_CHUNK)])

    entry_plan = build_entry_plan(entry, [_obs_edges(0, [], [])])

    inference = entry_plan.inferences[0]
    assert inference.verdict == "unknown"
    assert inference.applied_direction is None
    assert inference.fallback is True
    assert inference.rule == "geometric"
    assert str(_LOSER_CHUNK) in inference.reason
    assert "legacy both-ways restore" in inference.reason
    assert entry_plan.predicted.predicted_mirrors == 1


def test_geometric_both_is_kept_when_neither_live_edge_carries_the_chunk() -> None:
    """both-geometry with no carrier: keep the current fallback (``both``) + reason."""
    entry = _entry([_related(chunk_index=_LOSER_CHUNK)])

    entry_plan = build_entry_plan(entry, [_obs_edges(0, [_live(99)], [_live(98)])])

    inference = entry_plan.inferences[0]
    assert inference.verdict == "both"
    assert inference.applied_direction is None
    assert inference.fallback is True
    assert "neither" in inference.reason
    assert str(_LOSER_CHUNK) in inference.reason
    assert entry_plan.predicted.fallback_both == 1
    assert entry_plan.predicted.predicted_mirrors == 1


def test_missing_chunk_index_keeps_the_geometric_path() -> None:
    """No usable chunk_index: the provenance rule never decides; geometric runs."""
    entry = _entry([_related()])  # type + source_page only, no chunk_index

    entry_plan = build_entry_plan(entry, [_obs_edges(0, [_live(5)], [_live(5)])])

    inference = entry_plan.inferences[0]
    assert inference.verdict == "both"
    assert inference.rule == "geometric"
    assert inference.applied_direction is None
    assert inference.fallback is True
    assert "chunk_index" in inference.reason  # provenance unavailability is reported
    assert "legacy both-ways restore" in inference.reason


def test_geometric_out_stays_even_when_provenance_would_disagree() -> None:
    """A geometric out/in verdict is frozen: provenance runs only for both/unknown."""
    entry = _entry([_related(chunk_index=_LOSER_CHUNK)])

    # Only the forward edge exists, but it does NOT carry the loser's chunk.
    entry_plan = build_entry_plan(entry, [_obs_edges(0, [_live(77)], [])])

    inference = entry_plan.inferences[0]
    assert inference.verdict == "out"
    assert inference.rule == "geometric"
    assert inference.applied_direction == "out"
    assert inference.fallback is False


def test_source_page_corroborates_the_chunk_index_when_both_carry_it() -> None:
    """Same chunk_index on both sides: source_page (present on both) breaks the tie."""
    entry = _entry([_related(chunk_index=_LOSER_CHUNK)])  # loser source_page: 4

    entry_plan = build_entry_plan(
        entry,
        [
            _obs_edges(
                0,
                [_live(_LOSER_CHUNK, source_page=4)],
                [_live(_LOSER_CHUNK, source_page=7)],
            )
        ],
    )

    inference = entry_plan.inferences[0]
    assert inference.verdict == "out"
    assert inference.rule == "provenance"
    assert inference.applied_direction == "out"
    assert inference.fallback is False
    assert entry_plan.predicted.predicted_mirrors == 0


def test_mirror_prediction_drops_to_zero_only_for_provenance_decided_entries() -> None:
    """Provenance-decided entry predicts 0 mirrors; a fallback entry still predicts 1."""
    entry = _entry(
        [
            _mentions(),
            _related("graphrag-agentic:other-a", chunk_index=_LOSER_CHUNK),
            _related("graphrag-agentic:other-b"),  # no chunk_index -> geometric fallback
        ]
    )

    entry_plan = build_entry_plan(
        entry,
        [
            _obs_edges(1, [_live(_LOSER_CHUNK)], [_live(99)]),  # provenance -> out
            _obs_edges(2, [_live(5)], [_live(5)]),  # geometric both -> fallback
        ],
    )

    assert entry_plan.inferences[0].rule == "provenance"
    assert entry_plan.inferences[1].rule == "geometric"
    assert entry_plan.predicted == PredictedCensus(
        mentions_restored=1,
        related_restored=3,  # 1 (provenance) + 2 (legacy both-ways fallback)
        directions_inferred=1,
        fallback_both=1,
        fallback_unknown=0,
        predicted_mirrors=1,
    )


def test_fingerprint_changes_when_the_provenance_decision_changes() -> None:
    """Same entry + provenance decision -> stable hash; decision flips -> new hash."""
    entry = _entry([_related(chunk_index=_LOSER_CHUNK)])

    decided = build_rollback_plan(
        [build_entry_plan(entry, [_obs_edges(0, [_live(_LOSER_CHUNK)], [_live(99)])])]
    )
    decided_again = build_rollback_plan(
        [build_entry_plan(entry, [_obs_edges(0, [_live(_LOSER_CHUNK)], [_live(99)])])]
    )
    fallback = build_rollback_plan(
        [build_entry_plan(entry, [_obs_edges(0, [_live(_LOSER_CHUNK)], [_live(_LOSER_CHUNK)])])]
    )

    assert decided.fingerprint == decided_again.fingerprint
    assert decided.fingerprint != fallback.fingerprint
    assert decided.plans[0].inferred_entry.edge_inverse_map[0].direction == "out"
    assert fallback.plans[0].inferred_entry.edge_inverse_map[0].direction is None
    assert fallback.plans[0].predicted.predicted_mirrors == 1
