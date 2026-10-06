"""Rollback planning models: direction inference, predicted/actual census, fingerprint (T8c).

Pure Pydantic + stdlib (domain layer: no external libraries, no ports).

The 302 historical cross-namespace merges were written *before* the ledger
recorded a ``direction`` on RELATED inverse entries, so their inverse maps carry
``direction=None`` and ``rollback_merge``'s legacy path would restore BOTH
orientations, rebuilding mirror edges that never existed. The re-point of the
original apply preserved orientation:

* ``dup -> other`` became ``canonical -> other``;
* ``other -> dup`` became ``other -> canonical``.

So the original orientation is recoverable from the canonical's *current*
edges (:func:`infer_related_direction`). When the canonical holds BOTH
directions of the same type (``both``) or the matching edge is gone
(``unknown``), a second, provenance-based inference runs: the ledger's inverse
map captured the duplicate's own ``edge_properties`` (``type``,
``chunk_index``, ``source_page``) *before* the merge and the re-point copied
them onto the canonical's edge (``MERGE ... SET r2 += properties(r)``), so the
loser's provenance still identifies which live direction was originally the
duplicate's (:func:`provenance_direction`). Each inference records which
``rule`` decided it (``geometric`` vs ``provenance``) and why.

The planner fills a copy of the entry with the inferred directions (never
mutating the original), predicts the edge census and mirrors before anything is
written, and fingerprints the whole plan so a reviewed dry-run can be bound to
the later mutation. Post-apply, the same census arithmetic is re-computed from
the live graph and compared (:func:`compare_census`); any drift is reported
and fails the command. A *bidirectional original* — two inverse entries for
the same (other, type) pair, one decided ``out`` and one ``in`` — is counted
once per direction on both sides of that comparison (:func:`build_actual_outcomes`),
so both restored directions are restores and neither is a mirror.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from book_graph_rag.domain.merge_ledger_models import EdgeInverseMap, MergeLedgerEntry

#: Inference verdict for one RELATED inverse entry.
DirectionVerdict = Literal["out", "in", "both", "unknown"]

#: RELATED orientation relative to the duplicate (``EdgeInverseMap.direction``).
Direction = Literal["out", "in"]

#: Which rule decided a RELATED entry's outcome.
#: ``provenance`` only when the second, provenance-based inference resolved a
#: direction (then ``fallback`` is False and that entry predicts 0 mirrors);
#: ``geometric`` for stored/unambiguous verdicts and for kept fallbacks (any
#: provenance evidence is appended to ``reason``).
InferenceRule = Literal["geometric", "provenance"]


def infer_related_direction(
    *,
    canonical_to_other: bool,
    other_to_canonical: bool,
) -> DirectionVerdict:
    """Infer the duplicate's original RELATED orientation from the live canonical.

    Pure truth table over the canonical's current edges of the recorded type:

    =============== ======================== ==================== ===========
    canonical->other other->canonical         verdict              original
    =============== ======================== ==================== ===========
    True            False                    ``out``              dup -> other
    False           True                     ``in``               other -> dup
    True            True                     ``both`` (ambiguous) unknown
    False           False                    ``unknown`` (gone)   unknown
    =============== ======================== ==================== ===========

    ``both`` and ``unknown`` are fallbacks: the planner keeps the documented
    legacy both-ways restore and reports the fallback (with its reason) so the
    operator sees exactly where legacy behaviour applies.
    """
    if canonical_to_other and not other_to_canonical:
        return "out"
    if other_to_canonical and not canonical_to_other:
        return "in"
    if canonical_to_other and other_to_canonical:
        return "both"
    return "unknown"


class RelatedEdgeProbe(BaseModel):
    """Observed RELATED edges between two entities, with their properties.

    ``a_to_b`` is ``a -[type]-> b``; ``b_to_a`` the reverse, and
    ``a_to_b_edges``/``b_to_a_edges`` carry each observed edge's
    ``properties(r)`` per direction — the input of the provenance rule. The
    planner probes with ``a`` = canonical (before apply) and the measurement
    probes with ``a`` = duplicate (after apply).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    a_to_b: bool
    b_to_a: bool
    a_to_b_edges: list[dict[str, Any]] = []
    b_to_a_edges: list[dict[str, Any]] = []


class RelatedEdgeObservation(BaseModel):
    """One probe bound to an entry's inverse-map position."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int
    map_index: int
    probe: RelatedEdgeProbe


class DirectionInference(BaseModel):
    """Per-RELATED-entry inference result: what was observed and what will run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int
    map_index: int
    duplicate_entity_id: str
    original_other_endpoint_id: str
    edge_type: str
    observed_canonical_to_other: bool
    observed_other_to_canonical: bool
    verdict: DirectionVerdict
    #: Which rule decided the outcome (see :data:`InferenceRule`).
    rule: InferenceRule
    #: Direction the rollback will actually apply; ``None`` = legacy both-ways.
    applied_direction: Direction | None
    #: True when the legacy both-ways restore will run (``both``/``unknown``).
    fallback: bool
    #: Human-readable reason; fallbacks must name the legacy behaviour.
    reason: str


class PredictedCensus(BaseModel):
    """Predicted edge census for one entry plan or a whole batch (summed)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mentions_restored: int
    related_restored: int
    #: RELATED entries restored in exactly one direction (inferred or kept).
    #: "Single-direction" is a per-entry property: a bidirectional original
    #: contributes TWO entries for the same (other, type) pair — one decided
    #: ``out``, one ``in`` — and both count here, each restoring its own
    #: direction (two entries, one per direction, are two single-direction
    #: restores, not a fallback and not a mirror).
    directions_inferred: int
    fallback_both: int
    fallback_unknown: int
    #: Mirrors only come from fallbacks: one per ``both``/``unknown`` entry.
    predicted_mirrors: int


class RollbackEntryPlan(BaseModel):
    """Read-only plan for one ledger seq.

    ``selected_candidates`` is the T8f partial-rollback selection: ``None``
    means the whole entry (the historic behaviour, bit-identical), a list is
    the validated, entry-ordered subset this plan reverses — then ``entry``
    stays the stored whole entry while ``inferred_entry``, the inferences,
    the predicted census and ``affected_entities`` cover only the selection.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int
    #: The entry exactly as stored (legacy ``direction=None`` preserved).
    entry: MergeLedgerEntry
    #: Copy of ``entry`` with directions filled where inference succeeded;
    #: for a partial plan it additionally carries the filtered candidate_ids,
    #: inverse map and folded aliases of the selection.
    inferred_entry: MergeLedgerEntry
    inferences: list[DirectionInference]
    predicted: PredictedCensus
    #: Canonical + duplicates + other endpoints touched by this rollback.
    affected_entities: list[str]
    #: ``None`` = the whole entry; a list = partial rollback of exactly these
    #: candidates (non-empty subset of ``entry.candidate_ids``, entry order).
    selected_candidates: list[str] | None = None


class RollbackPlan(BaseModel):
    """Plan for every requested seq, with batch totals and a fingerprint.

    The fingerprint is sha256 over the ordered, canonical (sorted keys,
    compact separators) JSON serialization of the entry plans, so the same
    ledger+graph state always yields the same hash and any changed direction
    changes it. The graph census is deliberately not fingerprinted: unrelated
    graph activity must not invalidate a reviewed plan, only the plan's own
    inputs (entries + inference verdicts + the candidate selection).

    A tool change that adds a plan field (T8f's ``selected_candidates``) does
    change every fingerprint — the ``--expect-fingerprint`` guard then fails
    closed, which is the correct behaviour: a reviewed plan must be
    re-reviewed after a tool change.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    plans: list[RollbackEntryPlan]
    predicted: PredictedCensus
    fingerprint: str


class EdgeCensus(BaseModel):
    """MENTIONS/RELATED counts per affected entity plus db-wide totals."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mentions_by_entity: dict[str, int]
    related_by_entity: dict[str, int]
    total_mentions: int
    total_related: int
    merged_into_count: int


class ActualEntryOutcome(BaseModel):
    """Post-apply measurement for one seq (probed from the live graph)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int
    related_restored: int
    mirrors_created: int


class RollbackMeasurement(BaseModel):
    """Post-apply outcomes plus the post-apply census."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcomes: list[ActualEntryOutcome]
    census: EdgeCensus


class RollbackComparison(BaseModel):
    """Prediction vs reality; ``drift`` non-empty means the run must fail."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: bool
    drift: list[str]
    mentions_restored: int
    related_restored: int
    mirrors_created: int


def _edge_type(inverse: EdgeInverseMap) -> str:
    return str(inverse.edge_properties.get("type", ""))


def _carries_loser_identity(
    edge: dict[str, Any],
    loser: dict[str, Any],
    chunk_index: Any,
) -> bool:
    """Does one live edge carry the loser's captured identity?

    Matching order: ``chunk_index`` (the required key, already selected by
    ``type`` in the probe), corroborated by ``source_page`` when it is present
    on both the captured properties and the live edge.
    """
    if edge.get("chunk_index") != chunk_index:
        return False
    loser_page = loser.get("source_page")
    edge_page = edge.get("source_page")
    return not (loser_page is not None and edge_page is not None and edge_page != loser_page)


def provenance_direction(
    inverse: EdgeInverseMap,
    probe: RelatedEdgeProbe,
) -> tuple[Direction | None, str]:
    """Second, provenance-based inference (only called for ``both``/``unknown``).

    The inverse map stores the duplicate's own ``edge_properties`` as read
    before the merge, and the re-point copied them onto the canonical's edge,
    so the loser's provenance is still on the live edge. Matching order: the
    captured ``chunk_index`` (required — without it the caller keeps the
    geometric path), corroborated by ``source_page`` when present on both
    sides; the probe already selected edges of the same ``type``.

    Returns ``(direction, evidence)``:

    * exactly one live direction carries the loser's identity → that direction
      (``canonical -> other`` means the original was ``out``,
      ``other -> canonical`` ``in``);
    * both directions carry it (a collapsed MERGE) or neither does →
      ``(None, evidence)`` so the caller keeps the geometric fallback and
      reports why.
    """
    loser = inverse.edge_properties
    chunk_index = loser.get("chunk_index")
    if chunk_index is None:
        return None, "inverse entry has no usable chunk_index"
    forward = any(_carries_loser_identity(edge, loser, chunk_index) for edge in probe.a_to_b_edges)
    reverse = any(_carries_loser_identity(edge, loser, chunk_index) for edge in probe.b_to_a_edges)
    if forward and not reverse:
        return "out", (
            f"only the canonical -> other edge carries the loser's chunk_index={chunk_index}"
        )
    if reverse and not forward:
        return "in", (
            f"only the other -> canonical edge carries the loser's chunk_index={chunk_index}"
        )
    if forward and reverse:
        return None, (
            f"both directions carry the loser's chunk_index={chunk_index} (collapsed MERGE)"
        )
    return None, f"neither live edge carries the loser's chunk_index={chunk_index}"


def _build_inference(
    entry: MergeLedgerEntry,
    index: int,
    inverse: EdgeInverseMap,
    probe: RelatedEdgeProbe,
) -> DirectionInference:
    """Pure inference for one RELATED inverse entry (stored direction wins).

    Rule order: (1) a stored ``direction`` is left as recorded; (2) an
    unambiguous geometric verdict (``out``/``in``) is kept as is; (3) only for
    geometric ``both``/``unknown`` does :func:`provenance_direction` run — it
    decides a direction only when exactly one live direction carries the
    loser's ``chunk_index``, otherwise the geometric fallback is kept and the
    provenance evidence is appended to the reason.
    """
    verdict = infer_related_direction(
        canonical_to_other=probe.a_to_b,
        other_to_canonical=probe.b_to_a,
    )
    edge_type = _edge_type(inverse)
    if inverse.direction is not None:
        return DirectionInference(
            seq=entry.seq,
            map_index=index,
            duplicate_entity_id=inverse.duplicate_entity_id,
            original_other_endpoint_id=inverse.original_other_endpoint_id,
            edge_type=edge_type,
            observed_canonical_to_other=probe.a_to_b,
            observed_other_to_canonical=probe.b_to_a,
            verdict=verdict,
            rule="geometric",
            applied_direction=inverse.direction,
            fallback=False,
            reason=(
                f"entry already carries direction='{inverse.direction}' (left as "
                f"recorded; observed {verdict})"
            ),
        )
    if verdict == "out":
        return DirectionInference(
            seq=entry.seq,
            map_index=index,
            duplicate_entity_id=inverse.duplicate_entity_id,
            original_other_endpoint_id=inverse.original_other_endpoint_id,
            edge_type=edge_type,
            observed_canonical_to_other=probe.a_to_b,
            observed_other_to_canonical=probe.b_to_a,
            verdict=verdict,
            rule="geometric",
            applied_direction="out",
            fallback=False,
            reason=(
                f"canonical -[{edge_type}]-> other exists and the reverse does not: "
                "original was dup -> other (out)"
            ),
        )
    if verdict == "in":
        return DirectionInference(
            seq=entry.seq,
            map_index=index,
            duplicate_entity_id=inverse.duplicate_entity_id,
            original_other_endpoint_id=inverse.original_other_endpoint_id,
            edge_type=edge_type,
            observed_canonical_to_other=probe.a_to_b,
            observed_other_to_canonical=probe.b_to_a,
            verdict=verdict,
            rule="geometric",
            applied_direction="in",
            fallback=False,
            reason=(
                f"other -[{edge_type}]-> canonical exists and the forward does not: "
                "original was other -> dup (in)"
            ),
        )
    # Geometric both/unknown: second, provenance-based inference over the
    # loser's captured properties (chunk_index/source_page on the live edge).
    direction, evidence = provenance_direction(inverse, probe)
    if direction is not None:
        scoped = (
            "the direction-aware removal deletes only that direction "
            "(the canonical keeps its own reverse edge; census net 0)"
        )
        if direction == "out":
            reason = f"provenance: {evidence}: original was dup -> other (out); {scoped}"
        else:
            reason = f"provenance: {evidence}: original was other -> dup (in); {scoped}"
        return DirectionInference(
            seq=entry.seq,
            map_index=index,
            duplicate_entity_id=inverse.duplicate_entity_id,
            original_other_endpoint_id=inverse.original_other_endpoint_id,
            edge_type=edge_type,
            observed_canonical_to_other=probe.a_to_b,
            observed_other_to_canonical=probe.b_to_a,
            verdict=direction,
            rule="provenance",
            applied_direction=direction,
            fallback=False,
            reason=reason,
        )
    if verdict == "both":
        reason = (
            f"both -[{edge_type}]-> directions exist on the canonical (ambiguous): "
            "legacy both-ways restore will run (counted as 1 predicted mirror)"
        )
    else:
        reason = (
            f"no -[{edge_type}]-> edge remains on the canonical (edge gone): "
            "legacy both-ways restore will run (counted as 1 predicted mirror)"
        )
    return DirectionInference(
        seq=entry.seq,
        map_index=index,
        duplicate_entity_id=inverse.duplicate_entity_id,
        original_other_endpoint_id=inverse.original_other_endpoint_id,
        edge_type=edge_type,
        observed_canonical_to_other=probe.a_to_b,
        observed_other_to_canonical=probe.b_to_a,
        verdict=verdict,
        rule="geometric",
        applied_direction=None,
        fallback=True,
        reason=f"{reason}; provenance: {evidence}",
    )


def _predicted_census(
    inferences: list[DirectionInference],
    edge_inverse_map: list[EdgeInverseMap],
) -> PredictedCensus:
    """Census arithmetic: 1 edge per resolved direction, 2 per fallback (legacy)."""
    mentions = sum(1 for inv in edge_inverse_map if inv.edge_kind == "MENTIONS")
    related = sum(1 for inv in edge_inverse_map if inv.edge_kind == "RELATED")
    fallback_both = sum(1 for inf in inferences if inf.fallback and inf.verdict == "both")
    fallback_unknown = sum(1 for inf in inferences if inf.fallback and inf.verdict == "unknown")
    fallbacks = fallback_both + fallback_unknown
    directions_inferred = related - fallbacks
    # Inferred entries restore exactly one edge; the legacy both-ways fallback
    # runs both restore statements (dup->other AND other->dup).
    related_restored = directions_inferred + 2 * fallbacks
    return PredictedCensus(
        mentions_restored=mentions,
        related_restored=related_restored,
        directions_inferred=directions_inferred,
        fallback_both=fallback_both,
        fallback_unknown=fallback_unknown,
        predicted_mirrors=fallbacks,
    )


def _normalize_selection(
    entry: MergeLedgerEntry,
    selected_candidates: Sequence[str] | None,
) -> list[str] | None:
    """Validate the T8f candidate selection and return it in entry order.

    ``None`` means "the whole entry" and stays ``None``. Otherwise the
    selection must be a non-empty subset of ``entry.candidate_ids`` — an empty
    selection or any non-candidate id raises ``ValueError`` (fail-closed: a
    typo in ``--candidate`` must never widen or silently shrink the rollback).
    The returned order is the entry's own ``candidate_ids`` order so the same
    selection always yields the same plan (and the same fingerprint).

    Data-model limitation (fail closed): ``FoldedAlias`` records only
    ``from_entity_id`` + ``alias_value`` and the canonical's alias list keeps
    NO provenance of which candidate contributed a value, while
    ``Neo4jGraphMergeAdapter._ROLLBACK_REMOVE_ALIASES`` removes folded aliases
    from the canonical **by value**. If a selected candidate and a
    NON-selected candidate folded the same value, a partial rollback would
    delete the still-merged candidate's alias from the canonical (data loss),
    so such a selection is refused with ``ValueError``. The whole entry (or
    every candidate of it) removes exactly the values it folded and stays
    allowed.
    """
    if selected_candidates is None:
        return None
    requested = set(selected_candidates)
    if not requested:
        raise ValueError(
            f"selected_candidates for entry seq={entry.seq} must be non-empty "
            "(omit the selection to roll back the whole entry)"
        )
    unknown = requested - set(entry.candidate_ids)
    if unknown:
        raise ValueError(
            f"selected_candidates for entry seq={entry.seq} must be a subset of "
            f"its candidate_ids; not a candidate: {sorted(unknown)}"
        )
    selected_values: dict[str, list[str]] = {}
    other_values: dict[str, list[str]] = {}
    for alias in entry.aliases_folded:
        bucket = selected_values if alias.from_entity_id in requested else other_values
        bucket.setdefault(alias.alias_value, []).append(alias.from_entity_id)
    shared = sorted(set(selected_values) & set(other_values))
    if shared:
        details = "; ".join(
            f"{value!r} folded by selected {sorted(selected_values[value])} "
            f"and non-selected {sorted(other_values[value])}"
            for value in shared
        )
        raise ValueError(
            f"partial rollback of entry seq={entry.seq} refused: shared alias "
            f"value(s): {details}. The ledger records folded aliases by value "
            "only and Neo4jGraphMergeAdapter._ROLLBACK_REMOVE_ALIASES removes "
            "them from the canonical by value, so reviving the selected "
            "candidate would delete the non-selected candidate's alias from "
            "the canonical (data loss); roll back the whole entry instead"
        )
    return [candidate for candidate in entry.candidate_ids if candidate in requested]


def build_entry_plan(
    entry: MergeLedgerEntry,
    observations: Sequence[RelatedEdgeObservation],
    selected_candidates: Sequence[str] | None = None,
) -> RollbackEntryPlan:
    """Build the read-only plan for one entry (pure; never mutates ``entry``).

    ``observations`` are the probes taken with ``a`` = entry.canonical_id and
    ``b`` = each inverse entry's other endpoint (indexed by inverse-map
    position). MENTIONS entries are never directional and produce no
    inference; entries that already carry a direction are reported but kept as
    recorded. Missing observations behave as ``unknown`` (fail-safe: the
    legacy restore is kept and reported rather than guessed).

    ``selected_candidates`` (T8f) narrows the plan to a validated subset of
    ``entry.candidate_ids``: the inverse map is filtered by
    ``duplicate_entity_id``, the folded aliases by ``from_entity_id``, the
    ``inferred_entry`` carries the selected ``candidate_ids``, and the
    inferences, predicted census and affected entities cover ONLY the
    selection (directions are still inferred over the filtered RELATED
    entries — the provenance rule is unchanged). ``None`` keeps the whole
    entry behaviour bit-identical to the pre-T8f plan. Validation is
    fail-closed: unknown ids are refused, and so is a selection sharing a
    folded alias VALUE with a non-selected candidate (the adapter removes
    aliases by value — data-model limitation, see :func:`_normalize_selection`).
    """
    selected = _normalize_selection(entry, selected_candidates)
    selected_set: frozenset[str] = frozenset(selected) if selected is not None else frozenset()
    probes = {obs.map_index: obs.probe for obs in observations}
    inferences: list[DirectionInference] = []
    new_map: list[EdgeInverseMap] = []
    others: set[str] = set()
    for index, inverse in enumerate(entry.edge_inverse_map):
        if selected is not None and inverse.duplicate_entity_id not in selected_set:
            continue
        if inverse.edge_kind == "MENTIONS":
            new_map.append(inverse)
            continue
        others.add(inverse.original_other_endpoint_id)
        probe = probes.get(index, RelatedEdgeProbe(a_to_b=False, b_to_a=False))
        inference = _build_inference(entry, index, inverse, probe)
        inferences.append(inference)
        if inference.applied_direction == inverse.direction:
            new_map.append(inverse)
        else:
            new_map.append(inverse.model_copy(update={"direction": inference.applied_direction}))

    if selected is None:
        inferred_entry = entry.model_copy(update={"edge_inverse_map": new_map})
        affected_candidates = list(entry.candidate_ids)
    else:
        # Partial plan: the copy reverses exactly the selection — filtered
        # candidate_ids, filtered map and filtered aliases — so the later
        # compensating entry records the subset and the chain stays verifiable.
        inferred_entry = entry.model_copy(
            update={
                "edge_inverse_map": new_map,
                "candidate_ids": list(selected),
                "aliases_folded": [
                    alias for alias in entry.aliases_folded if alias.from_entity_id in selected_set
                ],
            }
        )
        affected_candidates = list(selected)
    affected = sorted({entry.canonical_id, *affected_candidates, *others})
    return RollbackEntryPlan(
        seq=entry.seq,
        entry=entry,
        inferred_entry=inferred_entry,
        inferences=inferences,
        predicted=_predicted_census(inferences, new_map),
        affected_entities=affected,
        selected_candidates=None if selected is None else list(selected),
    )


def compute_plan_fingerprint(entry_plans: Sequence[RollbackEntryPlan]) -> str:
    """sha256 over the ordered, canonical serialization of the plan."""
    payload = json.dumps(
        [plan.model_dump(mode="json") for plan in entry_plans],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_rollback_plan(entry_plans: Sequence[RollbackEntryPlan]) -> RollbackPlan:
    """Assemble the batch plan: summed census + fingerprint over the plans."""
    predicted = PredictedCensus(
        mentions_restored=sum(p.predicted.mentions_restored for p in entry_plans),
        related_restored=sum(p.predicted.related_restored for p in entry_plans),
        directions_inferred=sum(p.predicted.directions_inferred for p in entry_plans),
        fallback_both=sum(p.predicted.fallback_both for p in entry_plans),
        fallback_unknown=sum(p.predicted.fallback_unknown for p in entry_plans),
        predicted_mirrors=sum(p.predicted.predicted_mirrors for p in entry_plans),
    )
    return RollbackPlan(
        plans=list(entry_plans),
        predicted=predicted,
        fingerprint=compute_plan_fingerprint(entry_plans),
    )


def build_actual_outcomes(
    plan: RollbackPlan,
    observations: Sequence[RelatedEdgeObservation],
) -> list[ActualEntryOutcome]:
    """Measure restored edges and mirrors from post-apply probes (pure).

    Each probe is taken with ``a`` = duplicate, ``b`` = other endpoint, and
    the RELATED inverse entries are grouped by ``(other endpoint, edge
    type)``: a *bidirectional original* — the duplicate held the relation in
    BOTH directions, so the ledger carries two entries for the same pair with
    different ``chunk_index`` values — probes the same edge pair twice and
    must be counted ONCE PER DIRECTION, never twice and never as a mirror of
    itself (the production false positive this grouping fixed: two entries
    decided ``out`` + ``in`` measured 4 restored edges and 2 mirrors instead
    of 2 and 0).

    Per group:

    * every observed direction counts as one restored edge;
    * an observed direction that no entry's decided direction claims counts
      as a mirror (single-entry arithmetic: ``out`` restores ``dup -> other``,
      so a live ``other -> dup`` is the mirror);
    * a group holding a fallback entry (``direction is None``) keeps the
      documented legacy arithmetic: the first observed direction is the
      restore and each further one is a predicted mirror.
    """
    probes = {(obs.seq, obs.map_index): obs.probe for obs in observations}
    outcomes: list[ActualEntryOutcome] = []
    for entry_plan in plan.plans:
        related_restored = 0
        mirrors_created = 0
        # One group per (other endpoint, edge type): two ledger entries of a
        # bidirectional original share it and therefore share ONE edge pair.
        groups: dict[tuple[str, str], list[tuple[EdgeInverseMap, RelatedEdgeProbe]]] = {}
        for index, inverse in enumerate(entry_plan.inferred_entry.edge_inverse_map):
            if inverse.edge_kind != "RELATED":
                continue
            key = (inverse.original_other_endpoint_id, _edge_type(inverse))
            probe = probes.get(
                (entry_plan.seq, index),
                RelatedEdgeProbe(a_to_b=False, b_to_a=False),
            )
            groups.setdefault(key, []).append((inverse, probe))
        for members in groups.values():
            observed_out = any(probe.a_to_b for _, probe in members)
            observed_in = any(probe.b_to_a for _, probe in members)
            restored = int(observed_out) + int(observed_in)
            related_restored += restored
            if any(inverse.direction is None for inverse, _ in members):
                # Documented legacy fallback: first direction restores, each
                # further one is the predicted mirror.
                mirrors_created += max(0, restored - 1)
                continue
            claimed_out = any(inverse.direction == "out" for inverse, _ in members)
            claimed_in = any(inverse.direction == "in" for inverse, _ in members)
            # An observed direction no entry claims is a mirror; in a
            # bidirectional pair each direction is claimed by one entry, so
            # neither observed direction is a mirror.
            mirrors_created += int(observed_out and not claimed_out)
            mirrors_created += int(observed_in and not claimed_in)
        outcomes.append(
            ActualEntryOutcome(
                seq=entry_plan.seq,
                related_restored=related_restored,
                mirrors_created=mirrors_created,
            )
        )
    return outcomes


def compare_census(
    plan: RollbackPlan,
    census_before: EdgeCensus,
    measurement: RollbackMeasurement,
) -> RollbackComparison:
    """Compare the post-apply census and outcomes against the prediction.

    Every mismatch lands in ``drift`` (and fails the command): per-entry
    restored edges and mirrors, batch mentions/related totals, the
    ``merged_into`` delta (one cleared marker per rolled-back duplicate — for
    a T8f partial plan only the SELECTION's markers are expected to clear)
    and the db-wide totals (mentions only move; related grows by 2 only for each
    ``unknown`` fallback whose canonical edge was already gone — a
    provenance-decided entry nets 0 because the direction-aware removal
    deletes only the re-pointed direction and the restore recreates it).
    """
    drift: list[str] = []
    outcomes = {outcome.seq: outcome for outcome in measurement.outcomes}

    related_total = 0
    mirrors_total = 0
    for entry_plan in plan.plans:
        outcome = outcomes.get(entry_plan.seq)
        if outcome is None:
            drift.append(f"seq {entry_plan.seq}: no post-apply measurement")
            continue
        related_total += outcome.related_restored
        mirrors_total += outcome.mirrors_created
        if outcome.related_restored != entry_plan.predicted.related_restored:
            drift.append(
                f"seq {entry_plan.seq}: related restored {outcome.related_restored} "
                f"!= predicted {entry_plan.predicted.related_restored}"
            )
        if outcome.mirrors_created != entry_plan.predicted.predicted_mirrors:
            drift.append(
                f"seq {entry_plan.seq}: mirrors created {outcome.mirrors_created} "
                f"!= predicted {entry_plan.predicted.predicted_mirrors}"
            )

    candidates = sorted(
        {
            candidate
            for entry_plan in plan.plans
            for candidate in (
                entry_plan.selected_candidates
                if entry_plan.selected_candidates is not None
                else entry_plan.entry.candidate_ids
            )
        }
    )
    mentions_restored = sum(
        measurement.census.mentions_by_entity.get(candidate, 0)
        - census_before.mentions_by_entity.get(candidate, 0)
        for candidate in candidates
    )
    if mentions_restored != plan.predicted.mentions_restored:
        drift.append(
            f"mentions restored {mentions_restored} != predicted {plan.predicted.mentions_restored}"
        )
    if related_total != plan.predicted.related_restored:
        drift.append(
            f"related restored {related_total} != predicted {plan.predicted.related_restored}"
        )
    if mirrors_total != plan.predicted.predicted_mirrors:
        drift.append(
            f"mirrors created {mirrors_total} != predicted {plan.predicted.predicted_mirrors}"
        )

    expected_merged_into = census_before.merged_into_count - len(candidates)
    if measurement.census.merged_into_count != expected_merged_into:
        drift.append(
            f"merged_into count {measurement.census.merged_into_count} "
            f"!= expected {expected_merged_into}"
        )
    if measurement.census.total_mentions != census_before.total_mentions:
        drift.append(
            f"MENTIONS total {census_before.total_mentions} -> "
            f"{measurement.census.total_mentions} (mentions must only move)"
        )
    expected_related_total = census_before.total_related + 2 * plan.predicted.fallback_unknown
    # Plain arithmetic: a provenance-decided entry nets 0 (its direction-aware
    # removal deletes the re-pointed direction and the restore recreates it),
    # so no per-entry compensation is needed anymore.
    if measurement.census.total_related != expected_related_total:
        drift.append(
            f"RELATED total {census_before.total_related} -> "
            f"{measurement.census.total_related} != expected {expected_related_total}"
        )

    return RollbackComparison(
        passed=not drift,
        drift=drift,
        mentions_restored=mentions_restored,
        related_restored=related_total,
        mirrors_created=mirrors_total,
    )
