"""Quarantine review sheet models and pure terminal formatting (T6).

Frozen, strict, no I/O: ``DecisionSheet`` is the data contract of the
``quarantine render`` decision sheet and ``format_sheet``/``format_list`` are
pure functions over it, so the terminal layout is unit-testable without ANSI,
colors or a TTY.

The reading applies the project thresholds (``BandThresholds()``: 0.50
``high_context`` / 0.10 ``conflict_floor``) to ``description_overlap`` — the
primary cross-namespace signal (design §4.1). It is evidence for the human,
never a routing decision: cross-namespace pairs are always quarantine
(spec 03 §2.4 / policy R6.2) and no model/cosine is computed here.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from book_graph_rag.domain.audit_models import normalize_key
from book_graph_rag.domain.models import Entity, EntityType
from book_graph_rag.domain.quarantine_models import QuarantineDecision
from book_graph_rag.domain.resolution_models import ConfidenceBand
from book_graph_rag.domain.s4_band_assignment import BandThresholds

#: Cap for the mention-context snippet printed on the sheet (characters,
#: newlines collapsed, ellipsis included). Short enough to decide in seconds.
MENTION_SNIPPET_CAP = 200

#: Top shared neighbours rendered on the sheet (the full list lives in JSON).
SHARED_NEIGHBOR_RENDER_LIMIT = 10

#: Constant routing note: the sheet never proposes the routing itself.
ROUTING_NOTE = "SIEMPRE cuarentena (spec 03 §2.4 / R6.2)"

_ROW_LABEL_WIDTH = 27  # table rows: values start at column 29
_ROW_VALUE_WIDTH = 38  # first value column width
_EVID_LABEL_WIDTH = 13  # evidence block: values start at column 15
_SIDE_LABEL_WIDTH = 25  # ledger/shared-neighbour lines: values at column 27


class EvidenceReading(StrEnum):
    """Strength of the cross-namespace evidence (NOT a routing decision)."""

    IDENTITY = "identity"
    UNDECIDED = "undecided"
    NO_SHARED_CONTEXT = "no_shared_context"


def reading_for(description_overlap: float, thresholds: BandThresholds) -> EvidenceReading:
    """Read the evidence strength from ``description_overlap`` alone.

    The S3 mean composite is structurally inapplicable cross-namespace (two of
    its three terms are pinned near zero before a merge — design §4.1), so the
    reading uses the primary signal against the project thresholds.
    """
    if description_overlap >= thresholds.high_context:
        return EvidenceReading.IDENTITY
    if description_overlap >= thresholds.conflict_floor:
        return EvidenceReading.UNDECIDED
    return EvidenceReading.NO_SHARED_CONTEXT


def is_generic_label(label: str) -> bool:
    """True when ``label`` is a single normalized word (generic collision risk)."""
    if not label.strip():
        return False
    return len(normalize_key(label).split()) == 1


def mention_snippet(text: str, cap: int = MENTION_SNIPPET_CAP) -> str:
    """Collapse whitespace/newlines and truncate ``text`` to ``cap`` chars.

    Deterministic: any run of whitespace becomes a single space; longer text
    is cut at ``cap`` and marked with an ellipsis (result never exceeds
    ``cap`` characters).
    """
    collapsed = " ".join(text.split())
    if len(collapsed) <= cap:
        return collapsed
    return collapsed[: cap - 1].rstrip() + "…"


class EntityReviewFacts(BaseModel):
    """Raw graph facts for one entity of a reviewed pair (port payload)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entity: Entity
    mention_source_ids: tuple[str, ...] = ()
    mention_chunk_count: int = 0
    neighbor_ids: tuple[str, ...] = ()
    mention_context: str = ""


class PriorMergeFacts(BaseModel):
    """A previous ledger merge involving both reviewed ids."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int
    applied_at: datetime
    rolled_back: bool = False


class PairReviewFacts(BaseModel):
    """Everything the review port reads for one anchor→candidate pair."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    anchor: EntityReviewFacts
    candidate: EntityReviewFacts
    shared_neighbor_ids: tuple[str, ...] = ()
    prior_merge: PriorMergeFacts | None = None


class SheetMember(BaseModel):
    """One column of the decision sheet: one member of the pair."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entity_id: str
    namespace: str
    name: str
    entity_type: EntityType
    source_page: int | None = None
    alias_count: int = 0
    description: str = ""
    mention_context: str = ""
    mention_count: int = 0
    mention_sources: tuple[str, ...] = ()
    related_count: int = 0


class SheetEvidence(BaseModel):
    """Labelled evidence block of the sheet (design §4.1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    description_overlap: float = Field(ge=0.0, le=1.0)
    mentions_jaccard: float = Field(ge=0.0, le=1.0)
    mentions_shared: int = Field(ge=0)
    mentions_union: int = Field(ge=0)
    related_jaccard: float = Field(ge=0.0, le=1.0)
    related_shared: int = Field(ge=0)
    related_union: int = Field(ge=0)
    composite: float = Field(ge=0.0, le=1.0)
    reading: EvidenceReading
    s0_matched_field: str
    s2_type_gate_passed: bool
    s2_type_gate_reason: str

    # Evidence labels: description_overlap is the primary signal; the two
    # jaccards are structural (≈0 until a merge connects the neighbourhoods).
    primary_signal: str = "description_overlap"
    structural_signals: tuple[str, ...] = ("mentions_jaccard", "related_jaccard")
    cosine_computed: bool = False
    s4_band_claimed: bool = False


class SharedNeighbor(BaseModel):
    """A neighbour related to both members, with its namespace."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entity_id: str
    namespace: str


class DecisionSheet(BaseModel):
    """The full decision sheet rendered by ``quarantine render``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int | None = None
    band: ConfidenceBand | None = None
    decision: QuarantineDecision | None = None
    created_at: datetime | None = None
    label: str
    entity_type: EntityType
    generic_label: bool
    cross_namespace: bool
    routing: str = ROUTING_NOTE
    members: tuple[SheetMember, SheetMember]
    evidence: SheetEvidence
    shared_neighbors: tuple[SharedNeighbor, ...] = ()
    prior_merge: PriorMergeFacts | None = None
    thresholds: BandThresholds = BandThresholds()

    @field_validator("shared_neighbors")
    @classmethod
    def _sort_shared_neighbors(
        cls, value: tuple[SharedNeighbor, ...]
    ) -> tuple[SharedNeighbor, ...]:
        """Deterministic render order independent of read order."""
        return tuple(sorted(value, key=lambda neighbor: neighbor.entity_id))


class QuarantineListRow(BaseModel):
    """One row of ``quarantine list``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int
    band: ConfidenceBand
    entity_type: EntityType
    label: str
    namespaces: tuple[str, ...]
    description_overlap: float | None = None
    anchor_id: str
    candidate_id: str
    generic: bool = False
    decision: QuarantineDecision

    @field_validator("namespaces")
    @classmethod
    def _sort_namespaces(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Deterministic namespace order regardless of evidence order."""
        return tuple(sorted(set(value)))


# ── Pure terminal formatting ────────────────────────────────────────────────


def _fit(value: str, width: int) -> str:
    """Tail-keep ``value`` to ``width`` chars (the id slug is the tail)."""
    if len(value) <= width:
        return value
    return "…" + value[-(width - 1) :]


def _fit_quoted(value: str, width: int) -> str:
    """Quote ``value`` so the rendered token fits in ``width`` chars."""
    if len(value) + 2 <= width:
        return f'"{value}"'
    return '"' + value[: width - 3].rstrip() + '…"'


def _stamp(value: datetime | None) -> str:
    """Render ``value`` as ``YYYY-MM-DDTHH:MMZ`` (UTC), or an em dash."""
    if value is None:
        return "—"
    return value.strftime("%Y-%m-%dT%H:%MZ")


def format_sheet(sheet: DecisionSheet) -> str:
    """Render the decision sheet as plain fixed-width text (no colors)."""
    decision = sheet.decision.value.upper() if sheet.decision is not None else "sin registro"
    scope = "cross-namespace" if sheet.cross_namespace else "same-namespace"
    lines = [
        (
            f"  seq {sheet.seq if sheet.seq is not None else '—'} · "
            f"band {sheet.band.value if sheet.band is not None else '—'} · "
            f"{scope} · {decision} · created {_stamp(sheet.created_at)}"
        ),
        (
            f'  label "{sheet.label}" | type {sheet.entity_type} | '
            f"routing: {sheet.routing} | cosine: no computado"
        ),
        "",
    ]

    left, right = sheet.members
    lines.append(
        "  ".ljust(_ROW_LABEL_WIDTH + 2)
        + _fit(left.namespace, _ROW_VALUE_WIDTH).ljust(_ROW_VALUE_WIDTH)
        + _fit(right.namespace, _ROW_VALUE_WIDTH)
    )
    rows = [
        ("entity id", left.entity_id, right.entity_id),
        (
            "name / type",
            f"{left.name} / {left.entity_type}",
            f"{right.name} / {right.entity_type}",
        ),
        (
            "source page",
            str(left.source_page) if left.source_page is not None else "—",
            str(right.source_page) if right.source_page is not None else "—",
        ),
        ("aliases", str(left.alias_count), str(right.alias_count)),
        (
            "description",
            _fit_quoted(left.description, _ROW_VALUE_WIDTH - 1),
            _fit_quoted(right.description, _ROW_VALUE_WIDTH),
        ),
        (
            "mention context",
            _fit_quoted(left.mention_context, _ROW_VALUE_WIDTH - 1),
            _fit_quoted(right.mention_context, _ROW_VALUE_WIDTH),
        ),
        (
            "MENTIONS",
            f"{left.mention_count} chunks · {', '.join(left.mention_sources)}",
            f"{right.mention_count} chunks · {', '.join(right.mention_sources)}",
        ),
        ("RELATED neighbours", str(left.related_count), str(right.related_count)),
    ]
    for label, left_value, right_value in rows:
        # The left column is capped one char short of the column width so a
        # full-width value never butts against the right column (mockup gap).
        lines.append(
            "  "
            + label.ljust(_ROW_LABEL_WIDTH)
            + _fit(left_value, _ROW_VALUE_WIDTH - 1).ljust(_ROW_VALUE_WIDTH)
            + _fit(right_value, _ROW_VALUE_WIDTH)
        )

    evidence = sheet.evidence
    pad = "  " + "".ljust(_EVID_LABEL_WIDTH)
    lines.extend(
        [
            "",
            (
                "  "
                + "evidencia".ljust(_EVID_LABEL_WIDTH)
                + f"description_overlap {evidence.description_overlap:.3f}"
                + "  <- primaria (mismo idioma)"
            ),
            pad
            + f"{'mentions_jaccard':<20}{evidence.mentions_jaccard:.3f}"
            + "  (estructural: sólo != 0 después de un merge)",
            pad
            + f"{'related_jaccard':<20}{evidence.related_jaccard:.3f}"
            + "  (estructural: sólo != 0 después de un merge)",
            pad
            + f"{'composite':<20}{evidence.composite:.3f}"
            + f"  ({sheet.thresholds.high_context:.2f} = high_context, umbral intra-namespace)",
            (
                "  "
                + "lectura".ljust(_EVID_LABEL_WIDTH)
                + f"{evidence.reading.value} · description_overlap "
                f"{evidence.description_overlap:.3f} vs high_context "
                f"{sheet.thresholds.high_context:.2f} / conflict_floor "
                f"{sheet.thresholds.conflict_floor:.2f} · evidencia, NO enrutamiento"
            ),
            "",
        ]
    )

    shown = sheet.shared_neighbors[:SHARED_NEIGHBOR_RENDER_LIMIT]
    neighbor_text = " · ".join(
        f"{neighbor.namespace}/{neighbor.entity_id.rsplit(':', 1)[-1]}" for neighbor in shown
    )
    if len(sheet.shared_neighbors) > SHARED_NEIGHBOR_RENDER_LIMIT:
        neighbor_text += " · …"
    lines.append(
        "  "
        + f"vecinos compartidos ({len(sheet.shared_neighbors)})".ljust(_SIDE_LABEL_WIDTH)
        + (neighbor_text or "(ninguno)")
    )

    if sheet.prior_merge is None:
        ledger_text = "sin merge previo entre estos dos ids"
    else:
        ledger_text = f"seq {sheet.prior_merge.seq} · {_stamp(sheet.prior_merge.applied_at)}"
        if sheet.prior_merge.rolled_back:
            ledger_text += " · rollback"
    lines.append("  " + "ledger".ljust(_SIDE_LABEL_WIDTH) + ledger_text)
    lines.extend(
        [
            "",
            "  "
            + "decidir".ljust(_EVID_LABEL_WIDTH)
            + "identity (merge)  ·  label collision (separar)  ·  deferir",
        ]
    )
    return "\n".join(lines)


def format_list(rows: Sequence[QuarantineListRow]) -> str:
    """Render the ``quarantine list`` table as plain text, ordered by seq."""

    def _pad(value: str, width: int) -> str:
        """Left-align to ``width``; an overflowing cell keeps a one-space gap."""
        return value.ljust(width) if len(value) < width else value + " "

    header = (
        "  "
        + "seq".rjust(5)
        + "  "
        + "band".ljust(7)
        + "type".ljust(10)
        + "label".ljust(16)
        + "namespaces".ljust(56)
        + "evid".rjust(6)
        + "  "
        + "members"
    )
    lines = [header]
    if not rows:
        lines.append("  (sin registros)")
        return "\n".join(lines)
    for row in sorted(rows, key=lambda item: item.seq):
        evid = f"{row.description_overlap:.3f}" if row.description_overlap is not None else "—"
        marker = "  ⚠ genérica" if row.generic else ""
        lines.append(
            "  "
            + str(row.seq).rjust(5)
            + "  "
            + _pad(row.band.value, 7)
            + _pad(row.entity_type, 10)
            + _pad(row.label, 16)
            + _pad("|".join(row.namespaces), 56)
            + evid.rjust(6)
            + "  "
            + f"{row.anchor_id} → {row.candidate_id}"
            + marker
        )
    return "\n".join(lines)
