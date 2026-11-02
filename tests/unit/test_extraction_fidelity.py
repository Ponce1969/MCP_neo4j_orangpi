"""Synthetic contract tests transcribed from extraction-labeling-protocol.md."""

from __future__ import annotations

from collections.abc import Iterable

import pytest
from pydantic import ValidationError

from book_graph_rag.domain.extraction_fidelity import (
    Prediction,
    calculate_extraction_fidelity,
    predictions_from_entities,
)
from book_graph_rag.domain.extraction_labels import (
    LabelConfidence,
    LabeledChunk,
    LabeledEntity,
    key_compact,
    load_labeled_chunks,
    normalize_surface,
)
from book_graph_rag.domain.models import Entity, EntityType


def _label(
    *,
    canonical: str,
    surfaces: list[str] | None = None,
    entity_type: EntityType = "pattern",
    confidence: LabelConfidence = "high",
) -> LabeledEntity:
    return LabeledEntity(
        surfaces=surfaces if surfaces is not None else [canonical],
        canonical=canonical,
        type=entity_type,
        confidence=confidence,
    )


def _chunk(
    chunk_id: str,
    *entities: LabeledEntity,
    book_id: str = "book-a",
    chunk_index: int = 1,
) -> LabeledChunk:
    return LabeledChunk(
        chunk_id=chunk_id,
        book_id=book_id,
        chunk_index=chunk_index,
        entities=list(entities),
        notes="",
    )


def _prediction(
    entity_id: str,
    name: str,
    *,
    canonical_name: str | None = None,
    aliases: list[str] | None = None,
    entity_type: EntityType = "pattern",
    merged_into: str | None = None,
) -> Prediction:
    return Prediction(
        id=entity_id,
        name=name,
        canonical_name=canonical_name,
        aliases=aliases or [],
        type=entity_type,
        merged_into=merged_into,
    )


def test_word_normalization_keeps_the_protocol_steps_literal() -> None:
    assert normalize_surface("GraphRAG") == "graphrag"
    assert normalize_surface("graph-rag") == "graph rag"
    assert normalize_surface("Graph RAG") == "graph rag"
    assert key_compact("GraphRAG") == key_compact("graph-rag")
    assert key_compact("graph-rag") == key_compact("Graph RAG")
    assert normalize_surface("  Café ＆ Tea!  ") == "cafe and tea"
    assert normalize_surface("A&B") == "aandb"
    assert normalize_surface("OpenAI") == "openai"
    assert normalize_surface("PyMuPDF") == "pymupdf"


def test_separator_insensitive_matching_only_ignores_separators() -> None:
    for chunk_index, label_surface, prediction_surface in (
        (1, "GraphRAG", "graph-rag"),
        (2, "abc", "a b c"),
        (3, "R&D", "R and D"),
        (4, "Café", "CAFE"),
    ):
        chunk_id = f"book-a:{chunk_index}"
        chunk = _chunk(
            chunk_id,
            _label(canonical=label_surface),
            chunk_index=chunk_index,
        )
        score = calculate_extraction_fidelity(
            [chunk],
            {chunk_id: [_prediction(f"entity-{chunk_index}", prediction_surface)]},
            {},
        )
        assert score.matched == 1

    protocol_chunk = _chunk("book-a:4", _label(canonical="Model Context Protocol"), chunk_index=4)
    distinct_forms = calculate_extraction_fidelity(
        [protocol_chunk],
        {"book-a:4": [_prediction("mcp", "MCP")]},
        {},
    )
    assert distinct_forms.matched == 0


def test_exact_surface_match_counts_as_one_correct_entity() -> None:
    chunk = _chunk("book-a:1", _label(canonical="ReAct"))
    score = calculate_extraction_fidelity(
        [chunk], {"book-a:1": [_prediction("react", "ReAct")]}, {}
    )

    assert (score.labels, score.predictions, score.matched, score.matching_types) == (
        1,
        1,
        1,
        1,
    )
    assert (score.precision, score.recall, score.f1, score.type_agreement) == (
        1.0,
        1.0,
        1.0,
        1.0,
    )


def test_alias_only_match_uses_any_labeled_surface_form() -> None:
    chunk = _chunk(
        "book-a:1",
        _label(
            canonical="Model Context Protocol",
            surfaces=["MCP", "Model Context Protocol"],
            entity_type="mcp",
        ),
    )
    score = calculate_extraction_fidelity(
        [chunk], {"book-a:1": [_prediction("mcp", "MCP", entity_type="mcp")]}, {}
    )

    assert score.matched == 1
    assert score.matching_types == 1


def test_accent_case_and_punctuation_variants_match() -> None:
    chunk = _chunk("book-a:1", _label(canonical="GraphRAG"))
    score = calculate_extraction_fidelity(
        [chunk],
        {"book-a:1": [_prediction("graph-rag", "graph-rag", aliases=["Graph RAG"])]},
        {},
    )

    assert score.matched == 1


def test_merged_prediction_is_scored_at_its_canonical_target() -> None:
    chunk = _chunk("book-a:1", _label(canonical="ReAct"))
    score = calculate_extraction_fidelity(
        [chunk],
        {
            "book-a:1": [
                _prediction("old-react", "ReAct", merged_into="canonical-react"),
                _prediction("react-root", "ReAct"),
            ]
        },
        {"canonical-react": "react-root"},
    )

    assert score.predictions == 1
    assert score.matched == 1


def test_predictions_resolving_to_same_canonical_id_count_once() -> None:
    chunk = _chunk("book-a:1", _label(canonical="ReAct"))
    score = calculate_extraction_fidelity(
        [chunk],
        {
            "book-a:1": [
                _prediction("react", "ReAct"),
                _prediction("old-react", "ReAct", merged_into="react"),
            ]
        },
        {},
    )

    assert (score.predictions, score.matched, score.precision, score.recall) == (
        1,
        1,
        1.0,
        1.0,
    )


def test_wrong_type_changes_only_type_agreement() -> None:
    chunk = _chunk("book-a:1", _label(canonical="ReAct", entity_type="pattern"))
    score = calculate_extraction_fidelity(
        [chunk], {"book-a:1": [_prediction("react", "ReAct", entity_type="tool")]}, {}
    )

    assert (score.labels, score.predictions, score.matched) == (1, 1, 1)
    assert (score.precision, score.recall, score.f1) == (1.0, 1.0, 1.0)
    assert (score.matching_types, score.type_agreement) == (0, 0.0)


def test_one_to_one_arbitration_prefers_more_shared_surface_forms() -> None:
    chunk = _chunk(
        "book-a:1",
        _label(
            canonical="Model Context Protocol",
            surfaces=["MCP", "Model Context Protocol"],
            entity_type="pattern",
        ),
    )
    score = calculate_extraction_fidelity(
        [chunk],
        {
            "book-a:1": [
                _prediction("z-weak", "MCP", entity_type="tool"),
                _prediction(
                    "a-strong",
                    "MCP",
                    aliases=["Model Context Protocol"],
                    entity_type="pattern",
                ),
            ]
        },
        {},
    )

    assert (score.predictions, score.matched, score.matching_types) == (2, 1, 1)
    assert score.precision == 0.5
    assert score.type_agreement == 1.0


def test_one_to_one_tie_breaks_by_smallest_canonical_id() -> None:
    chunk = _chunk(
        "book-a:1",
        _label(canonical="MCP", surfaces=["MCP", "Model Context Protocol"]),
    )
    score = calculate_extraction_fidelity(
        [chunk],
        {
            "book-a:1": [
                _prediction("zeta", "MCP", entity_type="tool"),
                _prediction("alpha", "MCP", entity_type="pattern"),
            ]
        },
        {},
    )

    assert score.matched == 1
    assert score.matching_types == 1


def test_low_confidence_labels_are_kept_in_headline_and_dropped_for_stability() -> None:
    chunk = _chunk(
        "book-a:1",
        _label(canonical="GraphRAG", confidence="low"),
        _label(canonical="ReAct"),
    )
    score = calculate_extraction_fidelity(
        [
            chunk,
        ],
        {
            "book-a:1": [
                _prediction("react", "ReAct"),
                _prediction("graphrag", "GraphRAG"),
            ]
        },
        {},
    )

    assert (score.labels, score.predictions, score.matched) == (2, 2, 2)
    assert score.f1 == 1.0
    assert score.excluding_low_confidence.labels == 1
    assert score.excluding_low_confidence.predictions == 2
    assert score.excluding_low_confidence.matched == 1
    assert score.excluding_low_confidence.precision == 0.5
    assert score.excluding_low_confidence.recall == 1.0
    assert score.excluding_low_confidence.f1 == pytest.approx(2 / 3)


def test_rates_are_none_exactly_when_their_denominators_are_zero() -> None:
    label = _chunk("book-a:1", _label(canonical="ReAct"))
    no_predictions = calculate_extraction_fidelity([label], {}, {})
    assert no_predictions.precision is None
    assert no_predictions.recall == 0.0
    assert no_predictions.f1 == 0.0
    assert no_predictions.type_agreement is None

    empty_labeled_chunk = _chunk("book-a:1")
    no_labels = calculate_extraction_fidelity(
        [empty_labeled_chunk],
        {
            "book-a:1": [_prediction("react", "ReAct")],
            "book-a:2": [_prediction("unscored", "Not in label file")],
        },
        {},
    )
    assert no_labels.labels == 0
    assert no_labels.predictions == 1
    assert no_labels.precision == 0.0
    assert no_labels.recall is None
    assert no_labels.f1 == 0.0
    assert no_labels.type_agreement is None

    empty = calculate_extraction_fidelity([], {}, {})
    assert empty.precision is None
    assert empty.recall is None
    assert empty.f1 is None
    assert empty.type_agreement is None


def test_metrics_include_a_per_book_breakdown() -> None:
    chunks = [
        _chunk("book-a:1", _label(canonical="ReAct"), book_id="book-a"),
        _chunk(
            "book-b:1",
            _label(canonical="MCP", entity_type="mcp"),
            book_id="book-b",
        ),
    ]
    score = calculate_extraction_fidelity(
        chunks,
        {
            "book-a:1": [_prediction("react", "ReAct")],
            "book-b:1": [_prediction("mcp", "MCP", entity_type="mcp")],
        },
        {},
    )

    assert set(score.per_book) == {"book-a", "book-b"}
    assert score.per_book["book-a"].f1 == 1.0
    assert score.per_book["book-b"].type_agreement == 1.0


def test_label_surfaces_must_be_nonempty_and_unique_after_normalization() -> None:
    with pytest.raises(ValidationError):
        LabeledEntity(surfaces=[], canonical="ReAct", type="pattern")

    with pytest.raises(ValidationError, match="duplicate"):
        LabeledEntity(
            surfaces=["GraphRAG", "graph-rag"],
            canonical="GraphRAG",
            type="pattern",
        )


def test_label_entities_must_be_sorted_by_normalized_canonical() -> None:
    with pytest.raises(ValidationError, match="sorted"):
        LabeledChunk(
            chunk_id="book-a:1",
            book_id="book-a",
            chunk_index=1,
            entities=[_label(canonical="Zebra"), _label(canonical="Agent")],
            notes="",
        )


def test_loader_rejects_duplicate_chunk_ids_and_inconsistent_chunk_identity() -> None:
    row = {
        "chunk_id": "book-a:1",
        "book_id": "book-a",
        "chunk_index": 1,
        "entities": [],
        "notes": "",
    }
    with pytest.raises(ValueError, match="duplicate chunk_id"):
        load_labeled_chunks([row, row])

    with pytest.raises(ValidationError, match="chunk_id"):
        load_labeled_chunks(
            [
                {
                    **row,
                    "chunk_id": "book-a:2",
                }
            ]
        )


def test_label_loader_accepts_already_parsed_jsonl_rows() -> None:
    chunks = load_labeled_chunks(
        [
            {
                "chunk_id": "knowledge:book-a:7",
                "book_id": "knowledge:book-a",
                "chunk_index": 7,
                "entities": [
                    {
                        "surfaces": ["ReAct", "Reasoning and Acting"],
                        "canonical": "ReAct",
                        "type": "pattern",
                        "confidence": "high",
                    }
                ],
                "notes": "",
            }
        ]
    )

    assert len(chunks) == 1
    assert chunks[0].chunk_id == "knowledge:book-a:7"
    assert chunks[0].entities[0].canonical == "ReAct"


# ── include_detail: the pairing behind the counted numbers ──────────────────


def test_detail_names_the_pairing_that_produced_the_counts() -> None:
    """Maximum-cardinality match under arbitration: detail = the counted pairing.

    Two predictions compete for one label; the winner is decided by shared surface
    forms (then canonical id) exactly as the counts were. The detail must name that
    winner, park the loser under ``unmatched_predictions`` and reconcile with the
    numbers from the same pass — a detail that disagreed with ``matched`` would be
    a second measurement.
    """
    chunk = _chunk(
        "book-a:1",
        _label(
            canonical="Model Context Protocol",
            surfaces=["MCP", "Model Context Protocol"],
            entity_type="pattern",
        ),
    )
    score = calculate_extraction_fidelity(
        [chunk],
        {
            "book-a:1": [
                _prediction("z-weak", "MCP", entity_type="tool"),
                _prediction(
                    "a-strong",
                    "MCP",
                    aliases=["Model Context Protocol"],
                    entity_type="pattern",
                ),
            ]
        },
        {},
        include_detail=True,
    )

    assert score.detail is not None
    (record,) = score.detail
    assert record.chunk_id == "book-a:1"
    (pair,) = record.matched
    assert pair.label_canonical == "Model Context Protocol"
    assert pair.prediction_id == "a-strong"
    assert pair.prediction_canonical == "a-strong"
    assert pair.type_matches is True
    (loser,) = record.unmatched_predictions
    assert loser.id == "z-weak"
    assert loser.type == "tool"
    assert record.unmatched_labels == ()
    # Reconciliation with the counted numbers of the SAME pass.
    assert len(record.matched) == score.matched == 1
    assert len(record.unmatched_predictions) == score.predictions - score.matched == 1
    assert sum(1 for matched_pair in record.matched if matched_pair.type_matches) == (
        score.matching_types
    )


def test_detail_tie_breaks_by_smallest_canonical_id_like_the_counts() -> None:
    """A pure tie (same shared surfaces) goes to the smallest canonical id — in the detail too."""
    chunk = _chunk("book-a:1", _label(canonical="MCP", surfaces=["MCP", "Model Context Protocol"]))
    score = calculate_extraction_fidelity(
        [chunk],
        {
            "book-a:1": [
                _prediction("zeta", "MCP", entity_type="tool"),
                _prediction("alpha", "MCP", entity_type="pattern"),
            ]
        },
        {},
        include_detail=True,
    )

    assert (score.matched, score.matching_types) == (1, 1)
    assert score.detail is not None
    (record,) = score.detail
    (pair,) = record.matched
    assert pair.prediction_id == "alpha"  # the tie-break the counts applied
    (loser,) = record.unmatched_predictions
    assert loser.id == "zeta"


def test_detail_keeps_unmatched_labels_and_merged_predictions_visible() -> None:
    """An unmatched label carries its surfaces; a merged prediction keeps ``merged_into``."""
    labeled = _chunk(
        "book-a:1",
        _label(canonical="ReAct", surfaces=["ReAct", "Reasoning and Acting"]),
    )
    no_predictions = calculate_extraction_fidelity(
        [labeled], {"book-a:1": []}, {}, include_detail=True
    )
    assert no_predictions.detail is not None
    (record,) = no_predictions.detail
    assert record.matched == ()
    assert record.unmatched_predictions == ()
    (unmatched_label,) = record.unmatched_labels
    assert unmatched_label.canonical == "ReAct"
    assert unmatched_label.type == "pattern"
    assert unmatched_label.surfaces == ("ReAct", "Reasoning and Acting")
    assert len(record.unmatched_labels) == no_predictions.labels - no_predictions.matched

    merged = calculate_extraction_fidelity(
        [labeled],
        {
            "book-a:1": [
                _prediction("react", "ReAct"),
                _prediction("old-graphrag", "GraphRAG", merged_into="graphrag-root"),
            ]
        },
        {},
        include_detail=True,
    )
    assert (merged.predictions, merged.matched) == (2, 1)
    assert merged.detail is not None
    (merged_record,) = merged.detail
    (unmatched_prediction,) = merged_record.unmatched_predictions
    assert unmatched_prediction.id == "old-graphrag"
    assert unmatched_prediction.name == "GraphRAG"
    assert unmatched_prediction.merged_into == "graphrag-root"
    assert len(merged_record.unmatched_predictions) == merged.predictions - merged.matched


def test_include_detail_false_leaves_the_result_identical_to_the_default() -> None:
    """``include_detail=False`` (and the default) must reproduce today's result exactly."""
    chunks = [
        _chunk("book-a:1", _label(canonical="ReAct")),
        _chunk("book-a:2", _label(canonical="MCP", entity_type="mcp"), chunk_index=2),
    ]
    predictions = {
        "book-a:1": [_prediction("react", "ReAct")],
        "book-a:2": [],
    }
    default = calculate_extraction_fidelity(chunks, predictions, {})
    explicit_off = calculate_extraction_fidelity(chunks, predictions, {}, include_detail=False)
    explicit_on = calculate_extraction_fidelity(chunks, predictions, {}, include_detail=True)

    assert default.detail is None
    assert explicit_off.detail is None
    # Field-for-field equality: counts, rates, per-book and the stability view untouched.
    assert explicit_off == default
    assert explicit_on.detail is not None
    assert (
        explicit_on.precision,
        explicit_on.recall,
        explicit_on.f1,
        explicit_on.type_agreement,
        explicit_on.labels,
        explicit_on.predictions,
        explicit_on.matched,
        explicit_on.matching_types,
        explicit_on.per_book,
        explicit_on.excluding_low_confidence,
    ) == (
        default.precision,
        default.recall,
        default.f1,
        default.type_agreement,
        default.labels,
        default.predictions,
        default.matched,
        default.matching_types,
        default.per_book,
        default.excluding_low_confidence,
    )


def test_prediction_adapter_copies_domain_entity_fields() -> None:
    source: Iterable[Entity] = [
        Entity(
            id="react",
            name="ReAct",
            type="pattern",
            aliases=["Reasoning and Acting"],
            canonical_name="ReAct",
        )
    ]

    predictions = predictions_from_entities(source)

    assert predictions == [
        Prediction(
            id="react",
            name="ReAct",
            canonical_name="ReAct",
            aliases=["Reasoning and Acting"],
            type="pattern",
            merged_into=None,
        )
    ]
