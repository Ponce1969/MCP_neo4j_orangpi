"""Port for PDF text extraction with semantic chunking."""

from __future__ import annotations

import abc
from collections.abc import Iterator

from book_graph_rag.domain.models import KnowledgeGraphChunk


class PDFReaderPort(abc.ABC):
    """Contract for PDF text extraction with semantic chunking."""

    @abc.abstractmethod
    def extract_chunks(self, file_path: str) -> Iterator[KnowledgeGraphChunk]:
        """Read a PDF, return an iterator of chunks carrying text + editorial
        metadata (``Chapter``, ``Section``, ``PageRef``). Sync because file I/O
        is synchronous.
        """
        ...
