"""Tests for PDFAdapter and the chunk_text helper (AC-03.1, AC-03.2, AC-03.3)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import fitz
import pytest

from book_graph_rag.config import Settings
from book_graph_rag.domain.models import PageRef
from book_graph_rag.infrastructure.pdf_adapter import PDFAdapter, chunk_text


def _make_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> Settings:
    """Build Settings in a hermetic tmp directory without external env vars."""
    monkeypatch.chdir(tmp_path)
    for var in ("NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    data = {
        "neo4j_uri": "bolt://localhost:7687",
        "neo4j_user": "neo4j",
        "neo4j_password": "secret",
    }
    data.update(overrides)
    return Settings.model_validate(data)


def _make_pdf(
    tmp_path: Path,
    toc: list[tuple[int, str, int]],
    page_texts: list[str],
) -> Path:
    """Create a synthetic PDF with the given page text and optional TOC."""
    path = tmp_path / "test.pdf"
    doc = fitz.open()
    for body in page_texts:
        page = doc.new_page()
        # Use insert_textbox so long strings wrap and remain extractable.
        text_rect = fitz.Rect(72, 72, page.rect.width - 72, page.rect.height - 72)
        page.insert_textbox(text_rect, body)
    if toc:
        doc.set_toc(toc)
    doc.save(str(path))
    doc.close()
    return path


def test_chunk_text_happy_path() -> None:
    """Spec example: 1000 chars, chunk_size=500, overlap=100 → 3 chunks."""
    text = "a" * 1000
    chunks = chunk_text(text, chunk_size=500, overlap=100)
    assert len(chunks) == 3
    assert len(chunks[0]) == 500
    assert len(chunks[1]) == 500
    assert len(chunks[2]) == 200


def test_chunk_text_edge_case_multiplo() -> None:
    """len=800, chunk_size=500, overlap=100 → step=400 → guard discards empty chunk."""
    text = "a" * 800
    chunks = chunk_text(text, chunk_size=500, overlap=100)
    assert len(chunks) == 2
    assert len(chunks[0]) == 500
    assert len(chunks[1]) == 400


def test_chunk_text_empty_text() -> None:
    """Empty input yields no chunks."""
    chunks = chunk_text("", chunk_size=500, overlap=100)
    assert chunks == []


def test_pdf_adapter_requires_settings() -> None:
    """AC-03.1: PDFAdapter requires Settings to construct."""
    with pytest.raises(TypeError):
        PDFAdapter()  # type: ignore[call-arg]


def test_extract_chunks_with_toc_yields_chunks_with_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-03.2: TOC-driven chunks carry Chapter, Section, Book and PageRef."""
    settings = _make_settings(tmp_path, monkeypatch, pdf_max_chunk_size=2000)
    page_texts = [f"Page {i + 1} " + "x" * 200 for i in range(15)]
    toc = [
        (1, "1", 1),
        (2, "Section 1.1", 1),
        (2, "Section 1.2", 4),
        (1, "2", 6),
        (2, "Section 2.1", 6),
        (2, "Section 2.2", 9),
        (1, "3", 11),
        (2, "Section 3.1", 11),
        (2, "Section 3.2", 14),
    ]
    pdf_path = _make_pdf(tmp_path, toc, page_texts)

    chunks = list(PDFAdapter(settings).extract_chunks(str(pdf_path)))

    assert len(chunks) == 6
    for chunk in chunks:
        assert chunk.book is not None
        assert chunk.book.title
        assert chunk.chapter is not None
        assert chunk.chapter.number in {1, 2, 3}
        assert chunk.section is not None
        assert chunk.section.level in (2, 3)
        assert chunk.page_ref is not None
        assert chunk.page_ref.start <= chunk.page_ref.end


def test_extract_chunks_without_toc_fallback_to_char_chunking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-03.3: PDFs without TOC fallback to pure char chunking, no editorial metadata."""
    settings = _make_settings(
        tmp_path,
        monkeypatch,
        pdf_max_chunk_size=1500,
        pdf_chunk_overlap=150,
    )
    # 5 pages of ~500 chars each = 2500 chars total.
    page_texts = [f"Page {i + 1} " + "x" * 493 for i in range(5)]
    pdf_path = _make_pdf(tmp_path, [], page_texts)

    chunks = list(PDFAdapter(settings).extract_chunks(str(pdf_path)))

    assert len(chunks) == 2
    for chunk in chunks:
        assert chunk.chapter is None
        assert chunk.section is None
        assert chunk.page_ref == PageRef(start=1, end=5)


def test_extract_chunks_section_too_long_triggers_subdivision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-03.2: sections longer than pdf_max_chunk_size are subdivided while keeping metadata."""
    settings = _make_settings(
        tmp_path,
        monkeypatch,
        pdf_max_chunk_size=1500,
        pdf_chunk_overlap=150,
    )
    # 10 pages of 500 chars = 5000 chars. step=1350 → 4 chunks.
    page_texts = [f"Page {i + 1} " + "x" * 493 for i in range(10)]
    toc = [
        (1, "1", 1),
        (2, "Long Section", 1),
    ]
    pdf_path = _make_pdf(tmp_path, toc, page_texts)

    chunks = list(PDFAdapter(settings).extract_chunks(str(pdf_path)))

    assert len(chunks) == 4
    chapter_ids = {id(chunk.chapter) for chunk in chunks}
    section_ids = {id(chunk.section) for chunk in chunks}
    assert len(chapter_ids) == 1
    assert len(section_ids) == 1
    assert chunks[0].section is not None
    assert chunks[0].section.title == "Long Section"


def test_extract_chunks_front_matter_skipped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Front-matter L1 entries produce no chunks; numbered chapters do."""
    settings = _make_settings(tmp_path, monkeypatch, pdf_max_chunk_size=2000)
    page_texts = [f"Page {i + 1} " + "x" * 200 for i in range(8)]
    toc = [
        (1, "Foreword", 1),
        (2, "Acknowledgments", 1),
        (1, "Preface", 3),
        (2, "Welcome", 3),
        (1, "Contributors", 5),
        (1, "1", 7),
        (2, "Real Chapter", 7),
    ]
    pdf_path = _make_pdf(tmp_path, toc, page_texts)

    chunks = list(PDFAdapter(settings).extract_chunks(str(pdf_path)))

    assert any(chunk.chapter is not None and chunk.chapter.number == 1 for chunk in chunks)
    for chunk in chunks:
        assert chunk.chapter is None or chunk.chapter.title not in {
            "Foreword",
            "Preface",
            "Contributors",
        }


def test_extract_chunks_merges_number_and_title_l1_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two consecutive L1 entries (number then title) on the same page become one chapter."""
    settings = _make_settings(tmp_path, monkeypatch, pdf_max_chunk_size=2000)
    page_texts = [f"Page {i + 1} " + "x" * 200 for i in range(5)]
    toc = [
        (1, "1", 1),
        (1, "GenAI in the Enterprise", 1),
        (2, "Landscape", 1),
        (2, "Maturity", 3),
    ]
    pdf_path = _make_pdf(tmp_path, toc, page_texts)

    chunks = list(PDFAdapter(settings).extract_chunks(str(pdf_path)))

    assert len(chunks) == 2
    assert all(chunk.chapter is not None and chunk.chapter.number == 1 for chunk in chunks)
    assert chunks[0].chapter.title == "1 GenAI in the Enterprise"
