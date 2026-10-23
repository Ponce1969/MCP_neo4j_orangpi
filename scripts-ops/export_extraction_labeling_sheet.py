"""Export the deterministic, blind extraction ground-truth labeling sheet.

Blindness is enforced by the query: it reads only ``:Chunk`` fields (book id,
index, and text), never entity nodes, prediction properties, or ``:MENTIONS``
relationships. That makes it impossible for the exporter to show the labeler
what the extractor predicted for a chunk.

Default mode prints the human sheet to stdout. ``--json`` prints only the
sampled ``(book_id, chunk_index)`` pairs; ``--emit-skeleton PATH`` creates the
empty protocol-v1 JSONL file without overwriting an existing file; ``--texts
PATH`` writes a JSON mapping from chunk id to text, also without overwriting.
All graph access is through managed read transactions; no graph writes are
issued.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TextIO

from book_graph_rag.config import Settings
from book_graph_rag.infrastructure.neo4j_query_adapter import Neo4jQueryAdapter

SAMPLE_VERSION = "extraction-fidelity-v2"
SAMPLE_SIZE_PER_BOOK = 6
ACTIVE_BOOK_COUNT = 4

Chunk = tuple[str, int, str]

_CHUNK_QUERY = """
MATCH (c:Chunk)
RETURN c.book_id AS book_id, c.chunk_index AS chunk_index, c.text AS text
ORDER BY book_id, chunk_index, elementId(c)
SKIP $offset
LIMIT $limit
""".strip()


def select_sample(chunks: Sequence[Chunk], version: str = SAMPLE_VERSION) -> list[Chunk]:
    """Select 6 chunks per book using the protocol's stable BLAKE2b ordering.

    The output is ordered by ``(book_id, chunk_index)`` for readable, stable
    presentation; selection itself is determined only by the versioned digest
    and the protocol's chunk-index tie break.
    """
    chunks_by_book: dict[str, list[Chunk]] = defaultdict(list)
    seen_ids: set[tuple[str, int]] = set()
    for chunk in chunks:
        book_id, chunk_index, _text = chunk
        chunk_id = (book_id, chunk_index)
        if chunk_id in seen_ids:
            raise ValueError(f"Duplicate chunk identity: {book_id}:{chunk_index}")
        seen_ids.add(chunk_id)
        chunks_by_book[book_id].append(chunk)

    selected: list[Chunk] = []
    for book_id in sorted(chunks_by_book):
        book_chunks = chunks_by_book[book_id]
        if len(book_chunks) < SAMPLE_SIZE_PER_BOOK:
            raise ValueError(
                f"Book {book_id!r} has {len(book_chunks)} chunks; "
                f"at least {SAMPLE_SIZE_PER_BOOK} are required"
            )
        ranked = sorted(
            book_chunks,
            key=lambda chunk: (
                hashlib.blake2b(
                    f"{chunk[0]}:{chunk[1]}:{version}".encode(), digest_size=8
                ).digest(),
                chunk[1],
            ),
        )
        selected.extend(ranked[:SAMPLE_SIZE_PER_BOOK])

    return sorted(selected, key=lambda chunk: (chunk[0], chunk[1]))


def render_skeleton(chunks: Sequence[Chunk]) -> str:
    """Render empty label records in protocol §5 field order as JSONL."""
    lines = [
        json.dumps(
            {
                "chunk_id": f"{book_id}:{chunk_index}",
                "book_id": book_id,
                "chunk_index": chunk_index,
                "entities": [],
                "notes": "",
                "completed": False,
            },
            ensure_ascii=False,
        )
        for book_id, chunk_index, _text in chunks
    ]
    return "\n".join(lines) + ("\n" if lines else "")


def render_texts(chunks: Sequence[Chunk]) -> str:
    """Render only sampled chunk text, keyed by id, in the sample's stable order."""
    texts = {f"{book_id}:{chunk_index}": text for book_id, chunk_index, text in chunks}
    return json.dumps(texts, ensure_ascii=False, indent=2) + "\n"


def render_sheet(chunks: Sequence[Chunk], census: dict[str, int]) -> str:
    """Render census, blind text blocks, then the committed-sample pair list."""
    lines = ["Observed :Chunk census per book:"]
    lines.extend(f"  {book_id}: {census[book_id]}" for book_id in sorted(census))
    lines.extend(["", "BLIND LABELING SHEET", ""])

    for book_id, chunk_index, text in chunks:
        lines.extend(
            [
                f"chunk_id: {book_id}:{chunk_index}",
                "---",
                text,
                "",
            ]
        )

    lines.append("Sample pairs (book_id, chunk_index):")
    lines.extend(
        f"  ({json.dumps(book_id, ensure_ascii=False)}, {chunk_index})"
        for book_id, chunk_index, _text in chunks
    )
    return "\n".join(lines)


def _parse_chunk_row(row: dict[str, Any]) -> Chunk:
    book_id = row.get("book_id")
    chunk_index = row.get("chunk_index")
    text = row.get("text")
    if not isinstance(book_id, str) or not book_id:
        raise ValueError("A :Chunk row has no non-empty string book_id")
    if isinstance(chunk_index, bool) or not isinstance(chunk_index, int):
        raise ValueError(f"Chunk {book_id!r} has no integer chunk_index")
    if not isinstance(text, str):
        raise ValueError(f"Chunk {book_id}:{chunk_index} has no string text")
    return book_id, chunk_index, text


async def _read_all_chunks(adapter: Neo4jQueryAdapter, page_size: int) -> list[Chunk]:
    """Read the complete :Chunk frame in bounded, managed read transactions."""
    if page_size < 1:
        raise ValueError("neo4j_read_max_rows must be at least 1")

    chunks: list[Chunk] = []
    offset = 0
    while True:
        rows = await adapter.execute_read(_CHUNK_QUERY, {"offset": offset, "limit": page_size})
        page = [_parse_chunk_row(row) for row in rows]
        if len(page) > page_size:
            raise RuntimeError("The read adapter returned more rows than the requested page")
        chunks.extend(page)
        if len(page) < page_size:
            return chunks
        offset += len(page)


def _print_census(census: dict[str, int], *, file: TextIO) -> None:
    print("Observed :Chunk census per book:", file=file)
    for book_id in sorted(census):
        print(f"  {book_id}: {census[book_id]}", file=file)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    output = parser.add_mutually_exclusive_group()
    output.add_argument(
        "--emit-skeleton",
        type=Path,
        metavar="PATH",
        help="create empty protocol-v1 label JSONL at PATH (refuses to overwrite)",
    )
    output.add_argument(
        "--json",
        action="store_true",
        help="print only the deterministic sampled [book_id, chunk_index] pairs as JSON",
    )
    parser.add_argument(
        "--texts",
        type=Path,
        metavar="PATH",
        help="write only the sampled {chunk_id: text} mapping to PATH (refuses to overwrite)",
    )
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = Settings.model_validate({})
    adapter = Neo4jQueryAdapter(settings)
    try:
        chunks = await _read_all_chunks(adapter, settings.neo4j_read_max_rows)
    finally:
        await adapter.close()

    census: dict[str, int] = defaultdict(int)
    for book_id, _chunk_index, _text in chunks:
        census[book_id] += 1
    _print_census(census, file=sys.stderr if args.json or args.emit_skeleton else sys.stdout)

    if len(census) != ACTIVE_BOOK_COUNT:
        raise ValueError(
            f"Expected the {ACTIVE_BOOK_COUNT}-book active frame; observed {len(census)} books"
        )

    sample = select_sample(chunks)
    if args.texts is not None:
        with args.texts.open("xb") as output_file:
            output_file.write(render_texts(sample).encode("utf-8"))
        print(f"Chunk texts written to {args.texts}", file=sys.stderr)

    if args.json:
        pairs = [[book_id, chunk_index] for book_id, chunk_index, _text in sample]
        print(json.dumps(pairs, ensure_ascii=False, indent=2))
    elif args.emit_skeleton is not None:
        with args.emit_skeleton.open("x", encoding="utf-8", newline="\n") as output_file:
            output_file.write(render_skeleton(sample))
        print(f"Empty labeling skeleton written to {args.emit_skeleton}")
    else:
        print(render_sheet(sample, census))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
