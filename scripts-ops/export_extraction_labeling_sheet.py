"""Export the deterministic, blind extraction ground-truth labeling sheet.

Blindness is enforced by the query: it reads only ``:Chunk`` fields (book id,
index, and text), never entity nodes, prediction properties, or ``:MENTIONS``
relationships. That makes it impossible for the exporter to show the labeler
what the extractor predicted for a chunk.

Default mode prints the human sheet to stdout. ``--json`` prints only the
sampled ``(book_id, chunk_index)`` pairs; ``--emit-skeleton PATH`` creates the
empty protocol-v1 JSONL file and its ``.sample.json`` companion without
overwriting either existing file. The companion is the committed record of the
sample required by the labeling protocol. ``--texts PATH`` writes a JSON mapping
from chunk id to text, also without overwriting. ``--book BOOK_ID`` (repeatable)
restricts the frame to the named books before sampling so a per-book pilot
can be drawn; every output then describes that filtered sample. Without
``--book`` the whole-corpus guard still requires exactly ``ACTIVE_BOOK_COUNT``
books.

``--sample-size N`` changes the per-book allocation away from the
``SAMPLE_SIZE_PER_BOOK`` default; the four-book whole-corpus frame is defined by
that constant, not by a flag, so the size is only valid together with
``--book``. ``--exclude-labeled PATH`` reads a label JSONL and removes every
``chunk_id`` it names from the candidate frame *before* selection, so a round
cannot overlap chunks whose predictions were already seen (the pilot), with a
post-selection disjointness assertion as a second, independent guard.
``--sample-version`` selects the BLAKE2b suffix explicitly; neither the size nor
the exclusion ever changes it, so a non-default round must pass its version on
purpose (``extraction-fidelity-v3``). All graph access is through managed read
transactions; no graph writes are issued.
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


def select_sample(
    chunks: Sequence[Chunk],
    version: str = SAMPLE_VERSION,
    size: int = SAMPLE_SIZE_PER_BOOK,
) -> list[Chunk]:
    """Select ``size`` chunks per book using the protocol's stable BLAKE2b ordering.

    The output is ordered by ``(book_id, chunk_index)`` for readable, stable
    presentation; selection itself is determined only by the versioned digest
    and the protocol's chunk-index tie break. ``size`` is the per-book
    allocation; when it is smaller than the frame left after exclusion the
    book is named in the error together with both counts.
    """
    if size < 1:
        raise ValueError(f"Sample size must be at least 1, not {size}")

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
        if len(book_chunks) < size:
            raise ValueError(
                f"Book {book_id!r} has {len(book_chunks)} chunks; at least {size} are required"
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
        selected.extend(ranked[:size])

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


def render_sample_companion(chunks: Sequence[Chunk], version: str = SAMPLE_VERSION) -> bytes:
    """Render the committed sample record using stable pair ordering and LF."""
    pairs = sorted(
        [[book_id, chunk_index] for book_id, chunk_index, _text in chunks],
        key=lambda pair: (pair[0], pair[1]),
    )
    record = {"sample_version": version, "pairs": pairs}
    return (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _load_excluded_chunk_ids(path: Path) -> set[str]:
    """Read the ``chunk_id`` of every record in a label JSONL file."""
    excluded: set[str] = set()
    with path.open(encoding="utf-8") as label_file:
        for line_number, line in enumerate(label_file, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {error}") from error
            if not isinstance(record, dict):
                raise ValueError(f"{path}:{line_number}: expected a JSON object")
            chunk_id = record.get("chunk_id")
            if not isinstance(chunk_id, str) or not chunk_id:
                raise ValueError(f"{path}:{line_number}: expected a non-empty string chunk_id")
            excluded.add(chunk_id)
    return excluded


def _filter_labeled_chunks(chunks: Sequence[Chunk], excluded_ids: set[str]) -> list[Chunk]:
    """Drop already-labeled chunks from the candidate frame before selection."""
    return [chunk for chunk in chunks if f"{chunk[0]}:{chunk[1]}" not in excluded_ids]


def _assert_sample_disjoint(sample: Sequence[Chunk], excluded_ids: set[str]) -> None:
    """Fail if selection returned a chunk the exclusion set had removed.

    The pre-filter protects against a bug in this file's own filtering; this
    assertion protects against a reused companion or a mismatched id format.
    They are independent guards, so both are checked.
    """
    if not excluded_ids:
        return
    offenders = sorted(
        f"{book_id}:{chunk_index}"
        for book_id, chunk_index, _text in sample
        if f"{book_id}:{chunk_index}" in excluded_ids
    )
    if offenders:
        raise ValueError(
            "Selected sample overlaps the excluded labeled chunk(s): " + ", ".join(offenders)
        )


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
    parser.add_argument(
        "--book",
        action="append",
        metavar="BOOK_ID",
        help="restrict the sample frame to BOOK_ID before sampling (repeatable; "
        "full book_id, e.g. knowledge:ai-engineering-huyen)",
    )
    parser.add_argument(
        "--sample-size",
        type=int,
        metavar="N",
        default=None,
        help=f"select N chunks per book instead of SAMPLE_SIZE_PER_BOOK "
        f"({SAMPLE_SIZE_PER_BOOK}); only valid together with --book",
    )
    parser.add_argument(
        "--exclude-labeled",
        type=Path,
        metavar="PATH",
        help="label JSONL whose chunk_id values are removed from the candidate "
        "frame before selection (e.g. the pilot dataset)",
    )
    parser.add_argument(
        "--sample-version",
        metavar="VERSION",
        default=SAMPLE_VERSION,
        help="BLAKE2b selection suffix (default: %(default)s); neither --sample-size "
        "nor --exclude-labeled changes it",
    )
    args = parser.parse_args(argv)
    if args.sample_size is not None:
        if args.sample_size < 1:
            raise ValueError(f"--sample-size must be at least 1, not {args.sample_size}")
        if not args.book:
            raise ValueError(
                "--sample-size requires a --book filter: the four-book whole-corpus "
                "frame is defined by SAMPLE_SIZE_PER_BOOK, not by a flag"
            )
    return args


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

    info_stream = sys.stderr if args.json or args.emit_skeleton else sys.stdout
    requested: list[str] | None = None
    if args.book:
        requested = sorted(set(args.book))
        missing = sorted(set(requested) - set(census))
        if missing:
            raise ValueError(
                f"Requested book(s) not observed: {', '.join(missing)}; "
                f"observed books: {', '.join(sorted(census))}"
            )
        requested_set = set(requested)
        chunks = [chunk for chunk in chunks if chunk[0] in requested_set]
        census = {book_id: census[book_id] for book_id in requested}

    _print_census(census, file=info_stream)

    if requested is None and len(census) != ACTIVE_BOOK_COUNT:
        raise ValueError(
            f"Expected the {ACTIVE_BOOK_COUNT}-book active frame; observed {len(census)} books"
        )

    excluded_ids: set[str] = set()
    if args.exclude_labeled is not None:
        excluded_ids = _load_excluded_chunk_ids(args.exclude_labeled)
        excluded_counts: dict[str, int] = defaultdict(int)
        for book_id, chunk_index, _text in chunks:
            if f"{book_id}:{chunk_index}" in excluded_ids:
                excluded_counts[book_id] += 1
        print("Excluded labeled chunks per book:", file=info_stream)
        for book_id in sorted(census):
            print(f"  {book_id}: {excluded_counts.get(book_id, 0)}", file=info_stream)
        chunks = _filter_labeled_chunks(chunks, excluded_ids)

    size = SAMPLE_SIZE_PER_BOOK if args.sample_size is None else args.sample_size
    sample = select_sample(chunks, version=args.sample_version, size=size)
    _assert_sample_disjoint(sample, excluded_ids)
    if requested is not None:
        print(
            f"Effective selection: {len(sample)} chunks from {len(requested)} book(s): "
            + ", ".join(requested),
            file=info_stream,
        )
    companion_path = (
        args.emit_skeleton.with_suffix(".sample.json") if args.emit_skeleton is not None else None
    )
    if companion_path is not None and companion_path.exists():
        raise FileExistsError(f"Sample companion already exists: {companion_path}")

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
        assert companion_path is not None
        with companion_path.open("xb") as companion_file:
            companion_file.write(render_sample_companion(sample, args.sample_version))
        print(f"Empty labeling skeleton written to {args.emit_skeleton}")
    else:
        print(render_sheet(sample, census))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
