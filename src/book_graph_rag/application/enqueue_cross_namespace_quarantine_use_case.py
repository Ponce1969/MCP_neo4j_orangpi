"""EnqueueCrossNamespaceQuarantineUseCase — the missing T6b producer.

Detects the cross-namespace candidate population exactly as the audit rule
``DUPLICATE_ENTITY_CROSS_NAMESPACE`` does (the Cypher lives in the review
adapter; the T9 unification of ``toLower(trim())`` vs Python ``normalize_key``
stays documented there), turns each group into its cross-namespace pairs and
appends pending ``band=medium`` records to the quarantine file. Reads the
graph, appends JSONL — **never mutates the graph**.

Decisions (T6b):

* **Ordering**: candidates sort by ``description_overlap`` **descending** —
  the primary signal of design §4.1 — so ``--limit N`` picks the most
  promising pairs first; ties break on ``(anchor_id, candidate_id)``.
* **Idempotence**: any pair that already has a record — pending, approved or
  rejected — is skipped. ``force`` is the documented escape hatch that
  re-enqueues such a pair anyway (a new pending record with a fresh seq).
* **Evidence**: the real domain scoring — ``s0_match`` builder,
  ``s2_type_gate`` and the three S3 overlaps — with ``s1=None`` because no
  model runs in this producer (cosine: not computed). The record band is the
  conservative human-review ``medium``; ``composite_score`` carries the
  computed composite for the reviewer, and the CLI reports
  ``cosine: not computed`` next to the counts.
* **Orientation**: the anchor of each pair is ``choose_canonical_id`` (shortest
  id, ties lexicographic — the project rule), so ``canonical_id`` equals the
  detected anchor and ``candidate_id`` can never equal the canonical: approving
  the record can never stage a self-merge.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict

from book_graph_rag.domain.duplicate_grouping import choose_canonical_id
from book_graph_rag.domain.quarantine_models import QuarantineDecision, QuarantineRecord
from book_graph_rag.domain.quarantine_review_models import (
    CrossNamespaceCandidateGroup,
    PairReviewFacts,
    is_generic_label,
)
from book_graph_rag.domain.resolution_models import ConfidenceBand, ResolutionEvidence
from book_graph_rag.domain.s0_normalization import namespace_from_id, s0_match
from book_graph_rag.domain.s2_type_gate import s2_type_gate
from book_graph_rag.domain.s3_context_scoring import (
    description_overlap,
    mentions_jaccard,
    related_jaccard,
    s3_context_score,
)
from book_graph_rag.domain.s4_band_assignment import composite_score
from book_graph_rag.ports.quarantine_review_port import QuarantineReviewPort
from book_graph_rag.ports.quarantine_writer_port import QuarantineWriterPort


class EnqueuedQuarantinePair(BaseModel):
    """One record the run wrote (or would write, under ``dry_run``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int
    anchor_id: str
    candidate_id: str
    canonical_id: str
    group_key: str
    description_overlap: float


class EnqueueCrossNamespaceResult(BaseModel):
    """Outcome of one ``quarantine enqueue --cross-namespace`` run.

    ``groups_found`` counts detection groups (the audit's decision unit) and
    ``pairs_found`` counts the cross-namespace pairs that survive the
    ``generic_only``/``namespace`` filters. ``skipped_existing`` pairs already
    carry a record; ``forced_existing`` counts re-enqueued pairs under
    ``force``. With ``limit``, pairs beyond the cap are left unselected —
    neither enqueued nor skipped. The CLI prints these counts plus the
    ``cosine: not computed`` statement.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    dry_run: bool = False
    force: bool = False
    cosine_computed: bool = False  # this producer never runs a model
    groups_found: int = 0
    pairs_found: int = 0
    skipped_existing: int = 0
    forced_existing: int = 0
    failed: int = 0
    enqueued: int = 0
    seq_first: int | None = None
    seq_last: int | None = None
    records: tuple[EnqueuedQuarantinePair, ...] = ()


class EnqueueCrossNamespaceQuarantineUseCase:
    """Produce pending quarantine records for cross-namespace candidates."""

    def __init__(
        self,
        quarantine: QuarantineWriterPort,
        review: QuarantineReviewPort,
    ) -> None:
        self._quarantine = quarantine
        self._review = review

    async def enqueue(
        self,
        *,
        limit: int | None = None,
        generic_only: bool = False,
        namespace: str | None = None,
        dry_run: bool = False,
        force: bool = False,
    ) -> EnqueueCrossNamespaceResult:
        """Detect, rank and append cross-namespace quarantine records.

        Reads the graph and appends JSONL only; ``dry_run`` reports the same
        outcome (counts, would-be seq range, per-pair payloads) without
        writing a single byte.
        """
        groups = await self._review.find_cross_namespace_candidate_groups()
        pairs = self._candidate_pairs(groups, generic_only=generic_only, namespace=namespace)

        descriptions = await self._review.read_descriptions(
            sorted({entity_id for pair in pairs for entity_id in pair[:2]})
        )
        scored: list[tuple[str, str, str, float]] = [
            (
                anchor_id,
                candidate_id,
                group_key,
                description_overlap(
                    descriptions.get(anchor_id, ""),
                    descriptions.get(candidate_id, ""),
                ),
            )
            for anchor_id, candidate_id, group_key in pairs
        ]
        # Primary signal first (design §4.1), deterministic tie-break.
        scored.sort(key=lambda item: (-item[3], item[0], item[1]))

        existing = {
            frozenset((record.anchor_id, record.candidate_id))
            for record in self._quarantine.read_all()
        }
        skipped = 0
        selected: list[tuple[str, str, str, float]] = []
        for item in scored:
            if not force and frozenset(item[:2]) in existing:
                skipped += 1
            else:
                selected.append(item)
        if limit is not None:
            selected = selected[:limit]

        seq_cursor = self._next_seq()
        seq_first: int | None = None
        seq_last: int | None = None
        written: list[EnqueuedQuarantinePair] = []
        failed = 0
        forced_existing = 0

        for anchor_id, candidate_id, group_key, overlap in selected:
            try:
                facts = await self._review.read_pair_facts(anchor_id, candidate_id)
            except LookupError:
                failed += 1  # entity with a legacy type or a missing node
                continue
            evidence = self._build_evidence(facts)
            canonical = choose_canonical_id([anchor_id, candidate_id])
            record = QuarantineRecord(
                seq=seq_cursor,
                anchor_id=anchor_id,
                candidate_id=candidate_id,
                canonical_id=canonical,
                band=ConfidenceBand.MEDIUM,
                evidence=evidence,
                created_at=datetime.now(UTC),
                decision=QuarantineDecision.PENDING,
            )
            if not dry_run:
                self._quarantine.append(record)
            if frozenset((anchor_id, candidate_id)) in existing:
                forced_existing += 1
            if seq_first is None:
                seq_first = seq_cursor
            seq_last = seq_cursor
            written.append(
                EnqueuedQuarantinePair(
                    seq=seq_cursor,
                    anchor_id=anchor_id,
                    candidate_id=candidate_id,
                    canonical_id=canonical,
                    group_key=group_key,
                    description_overlap=overlap,
                )
            )
            seq_cursor += 1

        return EnqueueCrossNamespaceResult(
            dry_run=dry_run,
            force=force,
            groups_found=len(groups),
            pairs_found=len(pairs),
            skipped_existing=skipped,
            forced_existing=forced_existing,
            failed=failed,
            enqueued=len(written),
            seq_first=seq_first,
            seq_last=seq_last,
            records=tuple(written),
        )

    @staticmethod
    def _candidate_pairs(
        groups: list[CrossNamespaceCandidateGroup],
        *,
        generic_only: bool,
        namespace: str | None,
    ) -> list[tuple[str, str, str]]:
        """Cross-namespace pairs per group, anchored by ``choose_canonical_id``.

        Same-namespace pairs never form (the group may span 3 namespaces);
        ``namespace`` keeps only pairs touching that namespace; ``generic_only``
        keeps only single-word labels (the high-risk batch).
        """
        pairs: list[tuple[str, str, str]] = []
        for group in groups:
            if generic_only and not is_generic_label(group.group_key):
                continue
            members = group.member_ids  # sorted deterministically by the model
            for index, first in enumerate(members):
                for second in members[index + 1 :]:
                    first_ns = namespace_from_id(first)
                    second_ns = namespace_from_id(second)
                    if first_ns == second_ns:
                        continue
                    if namespace is not None and namespace not in (first_ns, second_ns):
                        continue
                    anchor_id = choose_canonical_id([first, second])
                    candidate_id = second if anchor_id == first else first
                    pairs.append((anchor_id, candidate_id, group.group_key))
        return pairs

    @staticmethod
    def _build_evidence(facts: PairReviewFacts) -> ResolutionEvidence:
        """Real domain scoring for one pair: S0 + S2 + the three overlaps.

        ``s1`` stays ``None`` (no cosine is computed by this producer) and the
        band is the conservative ``medium`` — the S0 short-circuit ``exact``
        must never be claimed for a record nobody scored (design §4.1 / the
        lesson from the 302 forged merges).
        """
        anchor, candidate = facts.anchor.entity, facts.candidate.entity
        evidence = s0_match(anchor, candidate)
        gate = s2_type_gate(anchor.type, candidate.type)
        mentions_j, mentions_shared, mentions_union = mentions_jaccard(
            set(facts.anchor.mention_source_ids),
            set(facts.candidate.mention_source_ids),
        )
        related_j, related_shared, related_union = related_jaccard(
            set(facts.anchor.neighbor_ids),
            set(facts.candidate.neighbor_ids),
        )
        overlap = description_overlap(anchor.description, candidate.description)
        signals = s3_context_score(
            mentions_j,
            related_j,
            overlap,
            0.0,  # cosine argument documents "not computed", not a score
            mentions_source_count=mentions_union,
            mentions_shared_count=mentions_shared,
            related_neighbor_count=related_union,
            related_shared_count=related_shared,
        )
        return evidence.model_copy(
            update={
                "s2": gate,
                "s3": signals,
                "composite_score": composite_score(signals),
                "band": ConfidenceBand.MEDIUM,
            }
        )

    def _next_seq(self) -> int:
        existing = self._quarantine.read_all()
        if not existing:
            return 1
        return max(record.seq for record in existing) + 1
