"""PDFReaderPort implementation using PyMuPDF.

The adapter performs semantic chunking driven by the PDF table of contents.
Each leaf of the TOC tree becomes one chunk (or several sub-chunks when the
section text exceeds ``pdf_max_chunk_size``), carrying editorial metadata
(``Book``, ``Chapter``, ``Section``, ``PageRef``).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import fitz

from book_graph_rag.config import Settings
from book_graph_rag.domain.models import Book, Chapter, KnowledgeGraphChunk, PageRef, Section
from book_graph_rag.ports.pdf_port import PDFReaderPort

# Titles that identify front-matter L1 entries. Stems rooted at these entries
# are skipped because they are not numbered chapters.
_FRONT_MATTER_KEYWORDS: frozenset[str] = frozenset(
    {
        "foreword",
        "preface",
        "contributors",
        "about",
        "table of contents",
        "cover",
        "join our",
        "share your",
        "free benefits",
        "get in touch",
        "how to unlock",
        "conventions",
        "download the example code files",
        "download the color images",
        "who this book is for",
        "what this book covers",
        "to get the most out of this book",
        "proven design patterns and practices",
    }
)

# Regexes used to detect numbered chapters.
_CHAPTER_NUMBER_ONLY_RE = re.compile(r"^\s*(\d+)\s*$")
_CHAPTER_DOT_TITLE_RE = re.compile(r"^\s*(\d+)\.\s+(.+)$")
_CHAPTER_SPACE_TITLE_RE = re.compile(r"^\s*(\d+)\s+(\w.*)$")


class _TocNode:
    """Internal node for the hierarchical TOC tree."""

    __slots__ = ("level", "title", "page_number", "children", "parent")

    def __init__(self, level: int, title: str, page_number: int) -> None:
        self.level: int = level
        self.title: str = title
        self.page_number: int = page_number
        self.children: list[_TocNode] = []
        self.parent: _TocNode | None = None


def chunk_text(text: str, chunk_size: int, overlap: int) -> list[str]:
    """Sub-divide ``text`` into overlapping chunks of at most ``chunk_size``.

    ``step = chunk_size - overlap``. The guard discards empty chunks that
    appear when ``len(text)`` is an exact multiple of ``step``.
    """
    chunks: list[str] = []
    step = chunk_size - overlap
    if step <= 0:
        raise ValueError(
            "step (chunk_size - overlap) debe ser > 0; "
            f"recibido chunk_size={chunk_size}, overlap={overlap}"
        )
    for start in range(0, len(text), step):
        if start >= len(text):
            break
        chunks.append(text[start : start + chunk_size])
    return chunks


class PDFAdapter(PDFReaderPort):
    """PyMuPDF-based implementation of ``PDFReaderPort``."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def extract_chunks(self, file_path: str) -> Iterator[KnowledgeGraphChunk]:
        doc: Any = fitz.open(file_path)
        page_count: int = cast(int, doc.page_count)

        metadata = cast(dict[str, Any], doc.metadata)
        title = metadata.get("title")
        author = metadata.get("author")
        book_title = title if isinstance(title, str) and title.strip() else Path(file_path).stem
        book_author = author if isinstance(author, str) else ""
        book = Book(
            id=self._slugify(book_title),
            title=book_title,
            author=book_author,
            pdf_path=file_path,
            page_count=page_count,
        )

        raw_toc = cast(list[tuple[int, str, int]], doc.get_toc())
        toc = self._preprocess_toc(raw_toc)

        chunk_index = 0

        if not toc:
            # Fallback: no TOC available → chunk the whole book as one block.
            full_text = "\n".join(
                cast(str, doc[page_idx].get_text()) for page_idx in range(page_count)
            )
            fallback_page_ref = PageRef(start=1, end=page_count)
            for sub_chunk in chunk_text(
                full_text,
                self._settings.pdf_max_chunk_size,
                self._settings.pdf_chunk_overlap,
            ):
                yield KnowledgeGraphChunk(
                    text=sub_chunk,
                    chunk_index=chunk_index,
                    book=book,
                    chapter=None,
                    section=None,
                    page_ref=fallback_page_ref,
                )
                chunk_index += 1
            doc.close()
            return

        root = self._build_toc_tree(toc)
        for leaf, page_start, page_end in self._iter_leaf_ranges(root, page_count):
            root_l1 = self._find_root_l1(leaf)
            if root_l1 is not None and self._is_front_matter(root_l1.title):
                continue

            chapter_node = self._find_chapter_ancestor(leaf)
            if chapter_node is None:
                # No numbered chapter owns this leaf (e.g. an orphan Part node).
                continue

            chapter_number, chapter_title = self._parse_chapter(chapter_node.title)
            chapter = Chapter(
                number=chapter_number,
                title=chapter_title,
                page_start=chapter_node.page_number,
            )
            section = self._build_section(leaf, chapter_number)

            page_start_idx = page_start - 1
            page_end_idx = page_end - 1
            text = "\n".join(
                cast(str, doc[page_idx].get_text())
                for page_idx in range(page_start_idx, page_end_idx + 1)
            )

            if len(text) > self._settings.pdf_max_chunk_size:
                sub_chunks = chunk_text(
                    text,
                    self._settings.pdf_max_chunk_size,
                    self._settings.pdf_chunk_overlap,
                )
            else:
                sub_chunks = [text]

            page_ref = PageRef(start=page_start, end=page_end)
            for sub_chunk in sub_chunks:
                yield KnowledgeGraphChunk(
                    text=sub_chunk,
                    chunk_index=chunk_index,
                    book=book,
                    chapter=chapter,
                    section=section,
                    page_ref=page_ref,
                )
                chunk_index += 1

        doc.close()

    @staticmethod
    def _slugify(text: str) -> str:
        normalized = re.sub(r"[^a-z0-9]+", "-", text.lower().strip())
        return normalized.strip("-")

    @staticmethod
    def _is_front_matter(title: str) -> bool:
        lower = title.lower()
        return any(keyword in lower for keyword in _FRONT_MATTER_KEYWORDS)

    @staticmethod
    def _is_just_number(title: str) -> bool:
        return _CHAPTER_NUMBER_ONLY_RE.match(title) is not None

    @staticmethod
    def _parse_chapter(title: str) -> tuple[int | None, str]:
        title = title.strip()

        match = _CHAPTER_NUMBER_ONLY_RE.match(title)
        if match:
            return int(match.group(1)), title

        match = _CHAPTER_DOT_TITLE_RE.match(title)
        if match:
            return int(match.group(1)), title

        match = _CHAPTER_SPACE_TITLE_RE.match(title)
        if match:
            return int(match.group(1)), title

        return None, title

    def _preprocess_toc(self, toc: list[tuple[int, str, int]]) -> list[tuple[int, str, int]]:
        """Merge consecutive L1 entries where the first is just a chapter number.

        Real book PDFs often emit the chapter number and the chapter title as
        two consecutive L1 entries on the same page (e.g. ``1`` followed by
        ``GenAI in the Enterprise...``). This pre-processing collapses them
        into a single entry whose title contains both the number and the title.
        """
        processed: list[tuple[int, str, int]] = []
        i = 0
        while i < len(toc):
            level, title, page = toc[i]
            if level == 1 and self._is_just_number(title) and i + 1 < len(toc):
                next_level, next_title, next_page = toc[i + 1]
                if (
                    next_level == level
                    and next_page == page
                    and not self._is_just_number(next_title)
                ):
                    merged_title = f"{title.strip()} {next_title.strip()}"
                    processed.append((level, merged_title, page))
                    i += 2
                    continue
            processed.append((level, title, page))
            i += 1
        return processed

    @staticmethod
    def _build_toc_tree(toc: list[tuple[int, str, int]]) -> _TocNode:
        """Build a hierarchical TOC tree under a virtual level-0 root."""
        root = _TocNode(level=0, title="__root__", page_number=1)
        stack: list[_TocNode] = []

        for level, title, page_number in toc:
            node = _TocNode(level=level, title=title, page_number=page_number)
            # Find the most recent ancestor with a smaller level.
            while stack and stack[-1].level >= level:
                stack.pop()
            parent = stack[-1] if stack else root
            parent.children.append(node)
            node.parent = parent
            stack.append(node)

        return root

    @staticmethod
    def _iter_leaf_ranges(node: _TocNode, parent_end: int) -> Iterator[tuple[_TocNode, int, int]]:
        """Yield ``(leaf, page_start, page_end)`` for every leaf under ``node``."""
        if not node.children:
            yield node, node.page_number, parent_end
            return

        for idx, child in enumerate(node.children):
            child_end = (
                node.children[idx + 1].page_number - 1
                if idx + 1 < len(node.children)
                else parent_end
            )
            yield from PDFAdapter._iter_leaf_ranges(child, child_end)

    @staticmethod
    def _find_root_l1(node: _TocNode) -> _TocNode | None:
        current: _TocNode | None = node
        while current is not None:
            if current.level == 1:
                return current
            current = current.parent
        return None

    @classmethod
    def _find_chapter_ancestor(cls, node: _TocNode) -> _TocNode | None:
        """Return the nearest ancestor (or self) that represents a numbered chapter."""
        current: _TocNode | None = node
        while current is not None:
            number, _ = cls._parse_chapter(current.title)
            if number is not None:
                return current
            current = current.parent
        return None

    @classmethod
    def _build_section(cls, leaf: _TocNode, chapter_number: int | None) -> Section | None:
        """Build the deepest ``Section`` for a leaf, or ``None`` if the leaf is the chapter."""
        if leaf.level <= 1:
            return None

        parent_section_title: str | None = None
        if leaf.parent is not None and leaf.parent.level > 1:
            parent_section_title = leaf.parent.title

        return Section(
            chapter_number=chapter_number,
            level=leaf.level,
            title=leaf.title,
            page_start=leaf.page_number,
            parent_section_title=parent_section_title,
        )
