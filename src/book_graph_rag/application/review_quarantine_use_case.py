"""ReviewQuarantineUseCase — quarantine queue inspection (T6).

Read-only: builds the ``quarantine list`` rows and the ``quarantine render``
decision sheet from the quarantine JSONL store plus the graph facts behind
``QuarantineReviewPort``. All scoring reuses the domain functions
(``s0_match``, ``s2_type_gate``, ``mentions_jaccard``, ``related_jaccard``,
``description_overlap``, ``composite_score``, ``BandThresholds``); the routing
decision itself is never recomputed — cross-namespace pairs are always
quarantine (spec 03 §2.4 / policy R6.2) and no model is called.
"""

from __future__ import annotations

from book_graph_rag.domain.audit_models import normalize_key
from book_graph_rag.domain.quarantine_models import QuarantineRecord
from book_graph_rag.domain.quarantine_review_models import (
    DecisionSheet,
    EntityReviewFacts,
    LabelRisk,
    QuarantineListRow,
    RiskLevel,
    SharedNeighbor,
    SheetEvidence,
    SheetMember,
    SiblingRecord,
    approve_command_for,
    is_generic_label,
    label_risk,
    language_note,
    mention_snippet,
    reading_for,
    record_groups,
)
from book_graph_rag.domain.resolution_errors import ResolutionError
from book_graph_rag.domain.resolution_models import ConfidenceBand
from book_graph_rag.domain.s0_normalization import namespace_from_id, s0_match
from book_graph_rag.domain.s2_type_gate import s2_type_gate
from book_graph_rag.domain.s3_context_scoring import (
    description_overlap,
    mentions_jaccard,
    related_jaccard,
    s3_context_score,
)
from book_graph_rag.domain.s4_band_assignment import BandThresholds, composite_score
from book_graph_rag.ports.quarantine_review_port import QuarantineReviewPort
from book_graph_rag.ports.quarantine_writer_port import QuarantineWriterPort


class ReviewQuarantineUseCase:
    """Inspect the human-review queue: one row per record, one sheet per pair."""

    def __init__(
        self,
        quarantine: QuarantineWriterPort,
        review: QuarantineReviewPort,
        thresholds: BandThresholds | None = None,
    ) -> None:
        self._quarantine = quarantine
        self._review = review
        self._thresholds = thresholds if thresholds is not None else BandThresholds()

    async def list_records(
        self,
        *,
        show_all: bool = False,
        band: ConfidenceBand | None = None,
        namespace: str | None = None,
        limit: int | None = None,
        generic_only: bool = False,
        risk: RiskLevel | None = None,
    ) -> list[QuarantineListRow]:
        """Build the list rows (pending records, or all with ``show_all``).

        Ordering is deterministic (ascending ``seq``); ``limit`` applies after
        the filters. ``description_overlap`` is recomputed from the live graph
        so the primary signal reflects the current descriptions.

        ``generic_only`` keeps its T6a meaning (single-word labels);
        ``risk`` filters the T6c combined marker (single word × namespace
        count). The namespace count is a **corpus** property read from the
        graph in one batched query per call, so no queue filter (``--limit``,
        a partial enqueue) can change a row's level; labels the graph read
        does not match fall back to the record's own pair (T9 caveat).
        """
        records = self._quarantine.read_all() if show_all else self._quarantine.read_pending()

        candidates: list[tuple[QuarantineRecord, str]] = []
        for record in records:
            if band is not None and record.band is not band:
                continue
            record_namespaces = {
                record.evidence.anchor_namespace,
                record.evidence.candidate_namespace,
            }
            if namespace is not None and namespace not in record_namespaces:
                continue
            label = normalize_key(record.evidence.anchor_normalized.original)
            if generic_only and not is_generic_label(label):
                continue
            candidates.append((record, label))

        # Risk marker = corpus property: ONE batched graph read for every
        # label shown; the quarantine file's grouping never feeds the risk.
        corpus_counts = await self._review.count_label_namespaces(
            sorted({label for _, label in candidates})
        )

        picked: list[tuple[QuarantineRecord, str, LabelRisk]] = []
        for record, label in candidates:
            row_risk = label_risk(
                single_word=is_generic_label(label),
                namespace_count=corpus_counts.get(
                    label,
                    len(
                        {
                            record.evidence.anchor_namespace,
                            record.evidence.candidate_namespace,
                        }
                    ),
                ),
            )
            if risk is not None and row_risk.level is not risk:
                continue
            picked.append((record, label, row_risk))
        picked.sort(key=lambda item: item[0].seq)
        if limit is not None:
            picked = picked[:limit]

        descriptions = await self._review.read_descriptions(
            [
                entity_id
                for record, _, _ in picked
                for entity_id in (record.anchor_id, record.candidate_id)
            ]
        )
        rows: list[QuarantineListRow] = []
        for record, label, row_risk in picked:
            anchor_description = descriptions.get(record.anchor_id)
            candidate_description = descriptions.get(record.candidate_id)
            overlap = (
                description_overlap(anchor_description, candidate_description)
                if anchor_description is not None and candidate_description is not None
                else None
            )
            rows.append(
                QuarantineListRow(
                    seq=record.seq,
                    band=record.band,
                    entity_type=record.evidence.anchor_type,
                    label=label,
                    namespaces=tuple(
                        sorted(
                            {
                                record.evidence.anchor_namespace,
                                record.evidence.candidate_namespace,
                            }
                        )
                    ),
                    description_overlap=overlap,
                    anchor_id=record.anchor_id,
                    candidate_id=record.candidate_id,
                    generic=is_generic_label(label),
                    risk=row_risk,
                    decision=record.decision,
                )
            )
        return rows

    async def render_sheet(
        self,
        *,
        seq: int | None = None,
        pair: tuple[str, str] | None = None,
    ) -> DecisionSheet:
        """Build the decision sheet for a quarantine record or an entity pair.

        Exactly one of ``seq``/``pair`` must be given. ``pair`` supports the
        retro-audit of already-applied merges and now also reports the
        record(s) that already exist for that exact pair (T6c) instead of
        always reading ``sin registro``.
        """
        if (seq is None) == (pair is None):
            raise ResolutionError("Provide exactly one of seq or pair")

        all_records = self._quarantine.read_all()
        record: QuarantineRecord | None = None
        if seq is not None:
            record = next((r for r in all_records if r.seq == seq), None)
            if record is None:
                raise ResolutionError(f"Quarantine record with seq={seq} not found")
            anchor_id, candidate_id = record.anchor_id, record.candidate_id
        else:
            assert pair is not None  # guarded by the check above
            anchor_id, candidate_id = pair
            wanted = frozenset((anchor_id, candidate_id))
            matching = sorted(
                (
                    existing
                    for existing in all_records
                    if frozenset((existing.anchor_id, existing.candidate_id)) == wanted
                ),
                key=lambda existing: existing.seq,
            )
            record = matching[0] if matching else None

        facts = await self._review.read_pair_facts(anchor_id, candidate_id)
        anchor, candidate = facts.anchor.entity, facts.candidate.entity

        evidence = s0_match(anchor, candidate)
        gate = s2_type_gate(anchor.type, candidate.type)
        mentions_j, mentions_shared, mentions_union = mentions_jaccard(
            set(facts.anchor.mention_source_ids), set(facts.candidate.mention_source_ids)
        )
        related_j, related_shared, related_union = related_jaccard(
            set(facts.anchor.neighbor_ids), set(facts.candidate.neighbor_ids)
        )
        overlap = description_overlap(anchor.description, candidate.description)
        signals = s3_context_score(
            mentions_j,
            related_j,
            overlap,
            0.0,
            mentions_source_count=mentions_union,
            mentions_shared_count=mentions_shared,
            related_neighbor_count=related_union,
            related_shared_count=related_shared,
        )
        label = normalize_key(anchor.name)

        # Risk marker = corpus property: one batched read for this sheet's
        # label. The file-based grouping below feeds `siblings` ONLY; if the
        # graph read does not match the label, fall back to this pair.
        pair_namespaces = {evidence.anchor_namespace, evidence.candidate_namespace}
        corpus_counts = await self._review.count_label_namespaces([label])
        sheet_risk = label_risk(
            single_word=is_generic_label(label),
            namespace_count=corpus_counts.get(label, len(pair_namespaces)),
        )

        # T6c: sibling records of the same group (same normalized label +
        # type, namespaces overlapping the group's) — decide them together.
        groups = record_groups(all_records)
        if record is not None:
            matched_seqs = set(groups[record.seq].seqs)
        else:
            pair_namespaces = {namespace_from_id(anchor_id), namespace_from_id(candidate_id)}
            pair_labels = {label, normalize_key(candidate.name)}
            matched_seqs = {
                member_seq
                for group in set(groups.values())
                if group.label in pair_labels and set(group.namespaces) & pair_namespaces
                for member_seq in group.seqs
            }
        sibling_records = sorted(
            (
                sibling
                for sibling in all_records
                if sibling.seq in matched_seqs and (record is None or sibling.seq != record.seq)
            ),
            key=lambda sibling: sibling.seq,
        )
        sibling_descriptions: dict[str, str] = {}
        if sibling_records:
            sibling_descriptions = await self._review.read_descriptions(
                [
                    entity_id
                    for sibling in sibling_records
                    for entity_id in (sibling.anchor_id, sibling.candidate_id)
                ]
            )
        siblings = tuple(
            SiblingRecord(
                seq=sibling.seq,
                decision=sibling.decision,
                description_overlap=self._pair_overlap(
                    sibling_descriptions, sibling.anchor_id, sibling.candidate_id
                ),
                anchor_id=sibling.anchor_id,
                candidate_id=sibling.candidate_id,
            )
            for sibling in sibling_records
        )

        return DecisionSheet(
            seq=record.seq if record is not None else None,
            band=record.band if record is not None else None,
            decision=record.decision if record is not None else None,
            created_at=record.created_at if record is not None else None,
            label=label,
            entity_type=anchor.type,
            generic_label=is_generic_label(label),
            cross_namespace=evidence.cross_namespace,
            risk=sheet_risk,
            members=(self._member(facts.anchor), self._member(facts.candidate)),
            evidence=SheetEvidence(
                description_overlap=overlap,
                mentions_jaccard=mentions_j,
                mentions_shared=mentions_shared,
                mentions_union=mentions_union,
                related_jaccard=related_j,
                related_shared=related_shared,
                related_union=related_union,
                composite=composite_score(signals),
                reading=reading_for(overlap, self._thresholds),
                # T8b: separate language annotation (shared model) — qualifies
                # how the overlap may be read, never changes ``reading``.
                language_note=language_note(anchor.description, candidate.description),
                s0_matched_field=evidence.s0_matched_field,
                s2_type_gate_passed=gate.passed,
                s2_type_gate_reason=gate.reason,
            ),
            shared_neighbors=tuple(
                SharedNeighbor(
                    entity_id=neighbor_id,
                    namespace=namespace_from_id(neighbor_id),
                )
                for neighbor_id in facts.shared_neighbor_ids
            ),
            prior_merge=facts.prior_merge,
            siblings=siblings,
            approve_command=approve_command_for(sorted(matched_seqs)) if siblings else None,
            thresholds=self._thresholds,
        )

    @staticmethod
    def _pair_overlap(
        descriptions: dict[str, str], anchor_id: str, candidate_id: str
    ) -> float | None:
        """Fresh description_overlap for a sibling pair (None if unreadable)."""
        anchor_description = descriptions.get(anchor_id)
        candidate_description = descriptions.get(candidate_id)
        if anchor_description is None or candidate_description is None:
            return None
        return description_overlap(anchor_description, candidate_description)

    @staticmethod
    def _member(facts: EntityReviewFacts) -> SheetMember:
        entity = facts.entity
        return SheetMember(
            entity_id=entity.id,
            namespace=namespace_from_id(entity.id),
            name=entity.name,
            entity_type=entity.type,
            source_page=entity.source_page,
            alias_count=len(entity.aliases),
            description=entity.description,
            mention_context=mention_snippet(facts.mention_context),
            mention_count=facts.mention_chunk_count,
            mention_sources=facts.mention_source_ids,
            related_count=len(facts.neighbor_ids),
        )
