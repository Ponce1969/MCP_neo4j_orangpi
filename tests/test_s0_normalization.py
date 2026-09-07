"""Tests for S0 deterministic normalization (Slice B)."""

from __future__ import annotations

from book_graph_rag.domain.models import Entity
from book_graph_rag.domain.s0_normalization import (
    _compact,
    _normalize_text,
    _tokens,
    normalize_form,
    s0_match,
)


def test_normalize_text_nfkc_combining_vs_precomposed() -> None:
    """NFKC collapses combining and precomposed accented forms."""
    combining = "Cafe\u0301"  # e + combining acute
    precomposed = "Caf\u00e9"
    assert _normalize_text(combining) == _normalize_text(precomposed)
    assert _normalize_text(precomposed) == "café"


def test_normalize_text_casefold() -> None:
    assert _normalize_text("LANG GRAPH") == "lang graph"


def test_normalize_text_whitespace_collapse() -> None:
    assert _normalize_text("Lang  Graph") == _normalize_text("lang graph")
    assert _normalize_text("  a \t b \n c  ") == "a b c"


def test_normalize_text_fullwidth_latin() -> None:
    """Fullwidth latin capital letters collapse to lowercase ASCII."""
    assert _normalize_text("\uff21\uff22\uff23") == "abc"


def test_normalize_form_is_deterministic_and_exposes_fields() -> None:
    form = normalize_form("Café")
    assert form.original == "Café"
    assert form.nfkc == "café"
    assert form.casefold == "café"
    assert form.compact == "café"
    assert form.tokens == ("café",)

    # Re-running produces an identical value.
    assert normalize_form("Café") == form


def test_compact_preserves_special_chars() -> None:
    """Compact strips whitespace/hyphen/underscore but keeps +, #, .."""
    assert _compact("C++ #1.0") == "C++#1.0"
    assert _compact("foo-bar_baz qux") == "foobarbazqux"


def test_tokens_split_on_separators() -> None:
    assert _tokens("foo-bar_baz qux") == ("foo", "bar", "baz", "qux")
    assert _tokens("  single  ") == ("single",)


def test_s0_match_id_path() -> None:
    anchor = Entity(id="corp:src:slug-concept", name="X", type="concept")
    candidate = Entity(id="corp:src:slug-concept", name="Y", type="concept")
    ev = s0_match(anchor, candidate)
    assert ev.s0_matched_field == "id"
    assert ev.band.value == "exact"


def test_s0_match_canonical_path() -> None:
    anchor = Entity(
        id="corp:src:a-concept",
        name="LangGraph",
        type="concept",
        canonical_name="Lang Graph",
    )
    candidate = Entity(
        id="corp:src:b-concept",
        name="Lang Graph",
        type="concept",
    )
    ev = s0_match(anchor, candidate)
    assert ev.s0_matched_field == "canonical"
    assert ev.band.value == "exact"


def test_s0_match_alias_path() -> None:
    anchor = Entity(
        id="corp:src:a-concept",
        name="X",
        type="concept",
        aliases=["LangGraph"],
    )
    candidate = Entity(
        id="corp:src:b-concept",
        name="LangGraph",
        type="concept",
    )
    ev = s0_match(anchor, candidate)
    assert ev.s0_matched_field == "alias"
    assert ev.band.value == "exact"


def test_s0_match_no_match() -> None:
    anchor = Entity(id="corp:src:foo-concept", name="Foo", type="concept")
    candidate = Entity(id="corp:src:bar-concept", name="Bar", type="concept")
    ev = s0_match(anchor, candidate)
    assert ev.s0_matched_field == "none"


def test_s0_match_namespace_extraction() -> None:
    anchor = Entity(id="corp:src:foo-concept", name="Foo", type="concept")
    candidate = Entity(id="other:src:bar-concept", name="Foo", type="concept")
    ev = s0_match(anchor, candidate)
    assert ev.anchor_namespace == "corp:src"
    assert ev.candidate_namespace == "other:src"
    assert ev.cross_namespace is True
