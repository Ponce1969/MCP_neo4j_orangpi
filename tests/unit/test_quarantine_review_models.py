"""Pure tests for the quarantine review sheet models and formatter (T6).

No Neo4j, no filesystem, no ANSI: the reading thresholds, the mention
snippet, the generic-label detection, the deterministic ordering and the
terminal formatter are all pure functions over frozen domain models.
"""

from __future__ import annotations

from datetime import UTC, datetime

from book_graph_rag.domain.quarantine_models import QuarantineDecision
from book_graph_rag.domain.quarantine_review_models import (
    DecisionSheet,
    EvidenceReading,
    LanguageRelation,
    PriorMergeFacts,
    QuarantineListRow,
    RiskLevel,
    SharedNeighbor,
    SheetEvidence,
    SheetMember,
    SiblingRecord,
    approve_command_for,
    detect_language,
    format_list,
    format_risk_marker,
    format_sheet,
    is_generic_label,
    label_risk,
    language_note,
    mention_snippet,
    reading_for,
)
from book_graph_rag.domain.resolution_models import ConfidenceBand
from book_graph_rag.domain.s0_normalization import namespace_from_id
from book_graph_rag.domain.s3_context_scoring import description_overlap
from book_graph_rag.domain.s4_band_assignment import BandThresholds

# ── Synthetic corpora ────────────────────────────────────────────────────────

_ES_DUPLICATE_A = (
    "Representación vectorial de texto que permite buscar documentos por "
    "significado y medir similitud semántica."
)
_ES_DUPLICATE_B = (
    "Representación vectorial de texto que permite buscar documentos por "
    "significado y comparar similitud semántica."
)
_EN_UNRELATED = (
    "A dense vector representation of text used for nearest neighbour search in retrieval systems."
)
_GENERIC_FRAMING_ES = "Entidad que utiliza herramientas y planificación para cumplir un objetivo."
_GENERIC_FRAMING_EN = (
    "An AI agent whose capabilities are defined and measured by benchmark evaluation."
)

_THRESHOLD = BandThresholds()

_ANCHOR_ID = "knowledge:graphrag-agentic:embeddings-concept"
_CANDIDATE_ID = "knowledge:essential-graphrag:embeddings-concept"


def _member(
    entity_id: str,
    *,
    name: str,
    description: str,
    mention_context: str,
    source_page: int,
    alias_count: int,
    mention_count: int,
    related_count: int,
) -> SheetMember:
    return SheetMember(
        entity_id=entity_id,
        namespace=namespace_from_id(entity_id),
        name=name,
        entity_type="concept",
        source_page=source_page,
        alias_count=alias_count,
        description=description,
        mention_context=mention_snippet(mention_context),
        mention_count=mention_count,
        mention_sources=(namespace_from_id(entity_id),),
        related_count=related_count,
    )


def _evidence() -> SheetEvidence:
    return SheetEvidence(
        description_overlap=0.636,
        mentions_jaccard=0.333,
        mentions_shared=1,
        mentions_union=3,
        related_jaccard=0.048,
        related_shared=1,
        related_union=21,
        composite=0.339,
        reading=EvidenceReading.IDENTITY,
        s0_matched_field="canonical",
        s2_type_gate_passed=True,
        s2_type_gate_reason="anchor type concept matches candidate type concept",
    )


def _sheet(
    *,
    siblings: tuple[SiblingRecord, ...] = (),
    approve_command: str | None = None,
) -> DecisionSheet:
    anchor = _member(
        _ANCHOR_ID,
        name="Embeddings",
        description=_ES_DUPLICATE_A,
        mention_context=(
            "Embeddings\nRepresentación vectorial de texto\npermite buscar por significado."
        ),
        source_page=122,
        alias_count=2,
        mention_count=3,
        related_count=7,
    )
    candidate = _member(
        _CANDIDATE_ID,
        name="Embeddings",
        description=_ES_DUPLICATE_B,
        mention_context="Dense vector representation of embeddings in the book.",
        source_page=41,
        alias_count=1,
        mention_count=5,
        related_count=9,
    )
    return DecisionSheet(
        seq=1462,
        band=ConfidenceBand.MEDIUM,
        decision=QuarantineDecision.PENDING,
        created_at=datetime(2026, 10, 3, 18, 22, tzinfo=UTC),
        label="embeddings",
        entity_type="concept",
        generic_label=True,
        cross_namespace=True,
        # The sheet's marker is corpus-fed; the helper pins the common
        # single-word-in-two-namespaces level (→ "⚠ media (genérica)").
        risk=label_risk(single_word=True, namespace_count=2),
        members=(anchor, candidate),
        evidence=_evidence(),
        # Deliberately out of order: the model must impose deterministic order.
        shared_neighbors=(
            SharedNeighbor(entity_id="knowledge:beta:zeta", namespace="knowledge:beta"),
            SharedNeighbor(entity_id="knowledge:alpha:alfa", namespace="knowledge:alpha"),
        ),
        siblings=siblings,
        approve_command=approve_command,
    )


def _row(
    seq: int,
    label: str,
    *,
    generic: bool = False,
    namespace_count: int = 2,
) -> QuarantineListRow:
    return QuarantineListRow(
        seq=seq,
        band=ConfidenceBand.MEDIUM,
        entity_type="concept",
        label=label,
        # Deliberately out of order: the model must sort namespaces.
        namespaces=("knowledge:beta", "knowledge:alpha"),
        description_overlap=0.636,
        anchor_id=f"knowledge:alpha:{label}",
        candidate_id=f"knowledge:beta:{label}",
        generic=generic,
        risk=label_risk(single_word=generic, namespace_count=namespace_count),
        decision=QuarantineDecision.PENDING,
    )


# ── Reading thresholds (description_overlap vs BandThresholds) ───────────────


def test_reading_is_identity_evidence_for_same_language_duplicate() -> None:
    """A same-language duplicate clears high_context -> identity evidence."""
    overlap = description_overlap(_ES_DUPLICATE_A, _ES_DUPLICATE_B)
    assert overlap >= _THRESHOLD.high_context
    assert reading_for(overlap, _THRESHOLD) is EvidenceReading.IDENTITY


def test_reading_is_no_shared_context_for_cross_language_duplicate() -> None:
    """A true cross-language duplicate has lexical overlap 0 -> no shared context."""
    overlap = description_overlap(_ES_DUPLICATE_A, _EN_UNRELATED)
    assert overlap < _THRESHOLD.conflict_floor
    assert reading_for(overlap, _THRESHOLD) is EvidenceReading.NO_SHARED_CONTEXT


def test_reading_is_no_shared_context_for_zero_overlap_generic_label() -> None:
    """The generic-label collision class scores ~0 -> no shared context."""
    overlap = description_overlap(_GENERIC_FRAMING_ES, _GENERIC_FRAMING_EN)
    assert overlap == 0.0
    assert reading_for(overlap, _THRESHOLD) is EvidenceReading.NO_SHARED_CONTEXT


def test_reading_is_undecided_between_floor_and_high_context() -> None:
    """An in-between overlap stays undecided under the project thresholds."""
    overlap = description_overlap(
        "alpha beta gamma delta epsilon",
        "alpha beta gamma zeta eta theta",
    )
    assert _THRESHOLD.conflict_floor <= overlap < _THRESHOLD.high_context
    assert reading_for(overlap, _THRESHOLD) is EvidenceReading.UNDECIDED


# ── T8b: conservative language signal (bilingual-corpus detector) ─────────

#: A mixed English/Spanish sentence: both function-word sets fire, so the
#: detector must refuse to call a language (never a guess).
_MIXED_EN_ES = (
    "The knowledge graph stores entities de forma que el agente recupera documentos for retrieval."
)

#: Technical prose without function words in either language: no evidence.
_ZERO_EVIDENCE_TECHNICAL = "GraphRAG hybrid retrieval benchmark pipeline"

#: Above the length floor but with a single English function word: weak
#: evidence, so the detector must return ``unknown``.
_SINGLE_HIT_EN = "Retrieval augmented systems ranked by cosine vectors"


def test_detect_language_reads_clear_english_and_spanish() -> None:
    """Unambiguous function-word evidence in ONE language names that language."""
    assert detect_language(_EN_UNRELATED) == "en"
    assert detect_language(_GENERIC_FRAMING_EN) == "en"
    assert detect_language(_ES_DUPLICATE_A) == "es"
    assert detect_language(_GENERIC_FRAMING_ES) == "es"


def test_detect_language_returns_unknown_for_mixed_text() -> None:
    """Both languages firing is ambiguous: never a guess, always unknown."""
    assert detect_language(_MIXED_EN_ES) == "unknown"


def test_detect_language_returns_unknown_for_short_and_empty_text() -> None:
    """Empty or very short text carries no usable function-word evidence."""
    for text in ("", "   ", "agent", "Text2Cypher"):
        assert detect_language(text) == "unknown", text


def test_detect_language_returns_unknown_for_technical_single_word() -> None:
    """A technical single-word (or zero-hit) description cannot be judged."""
    assert detect_language("graphrag-agentic") == "unknown"
    assert detect_language(_ZERO_EVIDENCE_TECHNICAL) == "unknown"


def test_detect_language_rejects_weak_single_hit_evidence() -> None:
    """One function word above the length floor is too weak to call."""
    assert len(_SINGLE_HIT_EN) > 40  # long enough that only the hits decide
    assert detect_language(_SINGLE_HIT_EN) == "unknown"


def test_language_note_covers_same_different_and_unknown() -> None:
    """The note qualifies the pair: same / different / unknown relation."""
    same = language_note(_ES_DUPLICATE_A, _ES_DUPLICATE_B)
    assert same.relation is LanguageRelation.SAME_LANGUAGE
    assert (same.anchor, same.candidate) == ("es", "es")

    cross = language_note(_EN_UNRELATED, _ES_DUPLICATE_A)
    assert cross.relation is LanguageRelation.DIFFERENT_LANGUAGES
    assert (cross.anchor, cross.candidate) == ("en", "es")

    one_side_unknown = language_note(_ES_DUPLICATE_A, "graphrag")
    assert one_side_unknown.relation is LanguageRelation.UNKNOWN
    assert (one_side_unknown.anchor, one_side_unknown.candidate) == ("es", "unknown")

    both_unknown = language_note("", _MIXED_EN_ES)
    assert both_unknown.relation is LanguageRelation.UNKNOWN


def test_format_sheet_flags_bilingual_pairs_on_the_lectura_line() -> None:
    """Different languages: the lectura line stops the zero overlap from
    being read as "not the same concept" (T8b caveat), layout otherwise kept."""
    bilingual_evidence = SheetEvidence(
        description_overlap=0.0,
        mentions_jaccard=0.0,
        mentions_shared=0,
        mentions_union=0,
        related_jaccard=0.0,
        related_shared=0,
        related_union=0,
        composite=0.0,
        reading=EvidenceReading.NO_SHARED_CONTEXT,
        s0_matched_field="canonical",
        s2_type_gate_passed=True,
        s2_type_gate_reason="anchor type concept matches candidate type concept",
        language_note=language_note(_EN_UNRELATED, _ES_DUPLICATE_A),
    )
    bilingual = _sheet().model_copy(update={"evidence": bilingual_evidence})
    text = format_sheet(bilingual)
    lectura_line = next(line for line in text.splitlines() if line.startswith("  lectura"))
    assert lectura_line == (
        "  lectura      no_shared_context · idiomas distintos: "
        "el número léxico no decide · description_overlap 0.000 vs high_context 0.50 "
        "/ conflict_floor 0.10 · evidencia, NO enrutamiento"
    )

    # A same-language pair (and the default unknown note) print the original
    # lectura line: the caveat only fires for genuinely bilingual pairs.
    same_language_evidence = bilingual_evidence.model_copy(
        update={"language_note": language_note(_ES_DUPLICATE_A, _ES_DUPLICATE_B)}
    )
    same_language = _sheet().model_copy(update={"evidence": same_language_evidence})
    assert "idiomas distintos" not in format_sheet(same_language)
    assert "idiomas distintos" not in format_sheet(_sheet())


# ── Mention snippet ─────────────────────────────────────────────────────────


def test_mention_snippet_collapses_newlines_and_whitespace() -> None:
    """Newlines and runs of whitespace collapse into single spaces."""
    assert mention_snippet("linea uno\nlinea dos\r\n\ttercera") == ("linea uno linea dos tercera")


def test_mention_snippet_truncates_at_documented_cap() -> None:
    """The snippet is capped at 200 chars with an ellipsis and no newlines."""
    long_text = "palabra " * 100
    snippet = mention_snippet(long_text)
    assert len(snippet) <= 200
    assert snippet.endswith("…")
    assert "\n" not in snippet


def test_mention_snippet_keeps_short_text_verbatim_after_collapsing() -> None:
    """A short snippet survives unchanged apart from the whitespace collapse."""
    assert mention_snippet("hola\nmundo") == "hola mundo"
    assert mention_snippet("") == ""


# ── Generic single-word label detection ─────────────────────────────────────


def test_generic_label_detects_single_word_labels() -> None:
    """Single-word labels (the high-risk batch) are generic."""
    for label in ("agent", "embeddings", "  LLM  ", "fine-tuning", "Evaluation"):
        assert is_generic_label(label) is True, label


def test_generic_label_rejects_multi_word_and_empty_labels() -> None:
    """Multi-word or empty labels are not the generic-collision class."""
    for label in ("Semantic Kernel", "Text2Cypher Pattern", "", "   "):
        assert is_generic_label(label) is False, label


# ── Deterministic ordering ──────────────────────────────────────────────────


def test_list_rows_sort_namespaces_on_construction() -> None:
    """Namespaces normalize to a sorted tuple regardless of input order."""
    assert _row(1, "embeddings").namespaces == ("knowledge:alpha", "knowledge:beta")


def test_sheet_sorts_shared_neighbors_on_construction() -> None:
    """Shared neighbours order deterministically by entity id."""
    sheet = _sheet()
    assert [n.entity_id for n in sheet.shared_neighbors] == [
        "knowledge:alpha:alfa",
        "knowledge:beta:zeta",
    ]


def test_format_list_orders_rows_by_seq_regardless_of_input_order() -> None:
    """The printed list is ordered by seq even when rows arrive shuffled."""
    text = format_list([_row(3, "pipeline"), _row(1, "embeddings"), _row(2, "agent")])
    assert text.index("embeddings") < text.index("agent") < text.index("pipeline")


def test_format_list_marks_only_rows_at_some_risk() -> None:
    """Rows without a risk stay clean; risky rows carry level AND reason."""
    text = format_list(
        [
            _row(1, "embeddings"),  # two words, 2 namespaces → none
            _row(2, "agent", generic=True),  # single word, 2 namespaces → media
        ]
    )
    assert text.count("⚠ media (genérica)") == 1
    assert "⚠ alta" not in text
    embeddings_line = next(line for line in text.splitlines() if "embeddings" in line)
    assert "⚠" not in embeddings_line
    assert "knowledge:alpha|knowledge:beta" in text


def test_format_list_of_empty_queue_reports_no_records() -> None:
    """An empty queue still prints honestly."""
    assert "(sin registros)" in format_list([])


# ── Formatter: approved sheet layout and field order ────────────────────────


def test_format_sheet_follows_approved_field_order() -> None:
    """Every approved field appears, in the approved order."""
    text = format_sheet(_sheet())
    markers = [
        "seq 1462",
        'label "embeddings"',
        "entity id",
        "name / type",
        "source page",
        "aliases",
        "description",
        "mention context",
        "MENTIONS",
        "RELATED neighbours",
        "evidencia",
        "description_overlap",
        "mentions_jaccard",
        "related_jaccard",
        "composite",
        "lectura",
        "vecinos compartidos",
        "ledger",
        "decidir",
    ]
    positions = [text.index(marker) for marker in markers]
    assert positions == sorted(positions), text


def test_format_sheet_header_carry_routing_band_and_cosine() -> None:
    """The header states routing R6.2, the record band and that no model ran."""
    text = format_sheet(_sheet())
    lines = text.splitlines()
    assert lines[0] == (
        "  seq 1462 · band medium · cross-namespace · PENDING · created 2026-10-03T18:22Z"
    )
    assert lines[1] == (
        '  label "embeddings" | type concept | '
        "routing: SIEMPRE cuarentena (spec 03 §2.4 / R6.2) | cosine: no computado"
    )
    assert "band claimed" not in text


def test_format_sheet_labels_primary_and_structural_evidence() -> None:
    """description_overlap is primary; both jaccards are labelled structural."""
    text = format_sheet(_sheet())
    assert "<- primaria (mismo idioma)" in text
    assert text.count("(estructural: sólo != 0 después de un merge)") == 2
    assert "(0.50 = high_context, umbral intra-namespace)" in text


def test_format_sheet_member_rows_align_both_columns() -> None:
    """Both members' facts render side by side in the approved column layout."""
    sheet = _sheet()
    text = format_sheet(sheet)
    anchor, candidate = sheet.members
    id_line = next(line for line in text.splitlines() if line.startswith("  entity id"))
    # Ids longer than the column keep their tail (the slug), mockup-style.
    assert "…" + _ANCHOR_ID[-36:] in id_line  # left column capped one short
    assert "…" + _CANDIDATE_ID[-37:] in id_line
    assert f"  {'source page'.ljust(27)}{'122'.ljust(38)}41" in text
    assert f"  {'aliases'.ljust(27)}{'2'.ljust(38)}1" in text
    right_mentions = "5 chunks · knowledge:essential-graphrag"
    expected_mentions = (
        f"  {'MENTIONS'.ljust(27)}"
        f"{'3 chunks · knowledge:graphrag-agentic'.ljust(38)}"
        f"…{right_mentions[-37:]}"
    )
    assert expected_mentions in text
    assert f"  {'RELATED neighbours'.ljust(27)}{'7'.ljust(38)}9" in text
    # The mention context is collapsed before it reaches the sheet.
    assert "\n" not in anchor.mention_context
    assert anchor.mention_context == (
        "Embeddings Representación vectorial de texto permite buscar por significado."
    )


def test_format_sheet_evidence_block_layout() -> None:
    """The evidence block keeps the approved labels, widths and reading line."""
    text = format_sheet(_sheet())
    assert "  evidencia    description_overlap 0.636  <- primaria (mismo idioma)" in text
    assert (
        "               mentions_jaccard    0.333  "
        "(estructural: sólo != 0 después de un merge)" in text
    )
    assert "               related_jaccard     0.048" in text
    assert (
        "               composite           0.339  "
        "(0.50 = high_context, umbral intra-namespace)" in text
    )
    assert (
        "  lectura      identity · description_overlap 0.636 vs high_context 0.50 "
        "/ conflict_floor 0.10 · evidencia, NO enrutamiento" in text
    )


def test_format_sheet_shared_neighbors_and_ledger_lines() -> None:
    """Shared neighbours show namespaces; the ledger line is honest both ways."""
    text = format_sheet(_sheet())
    assert "  vecinos compartidos (2)  knowledge:alpha/alfa · knowledge:beta/zeta" in text
    assert "sin merge previo entre estos dos ids" in text
    assert "  decidir      identity (merge)  ·  label collision (separar)  ·  deferir" in text

    merged = _sheet().model_copy(
        update={
            "prior_merge": PriorMergeFacts(
                seq=7,
                applied_at=datetime(2026, 9, 30, 10, 5, tzinfo=UTC),
            )
        }
    )
    merged_text = format_sheet(merged)
    assert "seq 7 · 2026-09-30T10:05Z" in merged_text
    assert "sin merge previo" not in merged_text

    rolled = _sheet().model_copy(
        update={
            "prior_merge": PriorMergeFacts(
                seq=7,
                applied_at=datetime(2026, 9, 30, 10, 5, tzinfo=UTC),
                rolled_back=True,
            )
        }
    )
    assert "seq 7 · 2026-09-30T10:05Z · rollback" in format_sheet(rolled)


def test_format_sheet_pair_without_record_reports_no_record() -> None:
    """``render --pair`` renders an honest sheet: no seq, no band, no decision."""
    pair_sheet = _sheet().model_copy(
        update={"seq": None, "band": None, "decision": None, "created_at": None}
    )
    assert format_sheet(pair_sheet).splitlines()[0] == (
        "  seq — · band — · cross-namespace · sin registro · created —"
    )


# ── T6c: combined risk (single word × namespace coverage) ───────────────────


def test_risk_single_word_in_two_namespaces_is_medium_generic() -> None:
    """A generic label in only two namespaces is medium, reason 'genérica'."""
    risk = label_risk(single_word=True, namespace_count=2)
    assert risk.level is RiskLevel.MEDIUM
    assert risk.single_word is True
    assert risk.namespace_count == 2
    assert risk.reason == "genérica"
    assert format_risk_marker(risk) == "⚠ media (genérica)"


def test_risk_two_word_label_in_three_namespaces_is_medium_wide() -> None:
    """A specific label spanning three namespaces is medium on coverage alone."""
    risk = label_risk(single_word=False, namespace_count=3)
    assert risk.level is RiskLevel.MEDIUM
    assert risk.reason == "3 nss"
    assert format_risk_marker(risk) == "⚠ media (3 nss)"


def test_risk_single_word_in_three_namespaces_is_high() -> None:
    """Both signals together (the dangerous homonymy) is high."""
    risk = label_risk(single_word=True, namespace_count=3)
    assert risk.level is RiskLevel.HIGH
    assert risk.reason == "genérica · 3 nss"
    assert format_risk_marker(risk) == "⚠ alta (genérica · 3 nss)"


def test_risk_two_word_label_in_two_namespaces_is_none() -> None:
    """Neither signal → no risk, no reason, no marker."""
    risk = label_risk(single_word=False, namespace_count=2)
    assert risk.level is RiskLevel.NONE
    assert risk.reason == ""
    assert format_risk_marker(risk) == ""


def test_risk_reason_prints_the_real_namespace_count() -> None:
    """The reason carries the actual count, never a hardcoded '3'."""
    assert label_risk(single_word=True, namespace_count=4).reason == "genérica · 4 nss"
    assert label_risk(single_word=False, namespace_count=5).reason == "5 nss"
    assert format_risk_marker(label_risk(single_word=False, namespace_count=5)) == "⚠ media (5 nss)"


def test_format_list_prints_level_and_reason_per_row() -> None:
    """Each row's marker states the level and why (genérica / N nss / both)."""
    text = format_list(
        [
            _row(1, "embeddings"),  # none: no marker
            _row(2, "agent", generic=True),  # medium: genérica
            _row(3, "llm", generic=True, namespace_count=3),  # high: both
        ]
    )
    assert "⚠ media (genérica)" in text
    assert "⚠ alta (genérica · 3 nss)" in text
    embeddings_line = next(line for line in text.splitlines() if "embeddings" in line)
    assert "⚠" not in embeddings_line


# ── T6c: sibling records + the joint approve command ────────────────────────


def test_approve_command_sorts_and_dedupes_seqs() -> None:
    """The composed command is ready to run: sorted, unique, gated flags."""
    assert approve_command_for([17, 4, 19, 4]) == (
        "quarantine approve --seq 4 --seq 17 --seq 19 --backup … --approval …"
    )


def _sheet_with_siblings() -> DecisionSheet:
    # Deliberately out of order: the model must sort siblings by seq.
    return _sheet(
        siblings=(
            SiblingRecord(
                seq=19,
                decision=QuarantineDecision.REJECTED,
                description_overlap=0.377,
                anchor_id="knowledge:alpha:embeddings-concept",
                candidate_id="knowledge:gamma:embeddings-concept",
            ),
            SiblingRecord(
                seq=17,
                decision=QuarantineDecision.PENDING,
                description_overlap=0.412,
                anchor_id="knowledge:alpha:embeddings-concept",
                candidate_id="knowledge:delta:embeddings-concept",
            ),
        ),
        approve_command=approve_command_for([1462, 17, 19]),
    )


def test_sheet_sorts_siblings_by_seq_on_construction() -> None:
    """Sibling order is deterministic regardless of read order."""
    sheet = _sheet_with_siblings()
    assert [sibling.seq for sibling in sheet.siblings] == [17, 19]


def test_format_sheet_lists_siblings_with_decision_evidence_and_command() -> None:
    """The sheet cross-references the group's other records and how to decide
    them together (seq, decision, evidence, composed approve command)."""
    text = format_sheet(_sheet_with_siblings())
    lines = text.splitlines()
    assert "hermanos (mismo grupo)" in text
    assert "seq 17 · PENDING · evid 0.412" in text
    assert "seq 19 · REJECTED · evid 0.377" in text
    command = "quarantine approve --seq 17 --seq 19 --seq 1462 --backup … --approval …"
    assert command in text
    hermanos_index = next(i for i, line in enumerate(lines) if "hermanos" in line)
    command_index = next(i for i, line in enumerate(lines) if "decidir juntos" in line)
    decidir_index = next(i for i, line in enumerate(lines) if "decidir      identity" in line)
    assert hermanos_index < command_index < decidir_index
    # Sibling lines print in seq order.
    seq17_index = next(i for i, line in enumerate(lines) if "seq 17 ·" in line)
    seq19_index = next(i for i, line in enumerate(lines) if "seq 19 ·" in line)
    assert seq17_index < seq19_index


def test_format_sheet_without_siblings_omits_the_section() -> None:
    """A sheet with no group mates prints no siblings section at all."""
    text = format_sheet(_sheet())
    assert "hermanos" not in text
    assert "decidir juntos" not in text
    assert "decidir      identity" in text
