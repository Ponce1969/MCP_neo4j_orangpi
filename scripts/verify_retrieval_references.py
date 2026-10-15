"""Verify the retrieval dataset's references against the live graph (read-only).

A label that does not exist, is not in the fulltext index, or is unreachable by the query the
retrieval layer runs makes every precision number untrustworthy in a way no reranker fixes.
This checks the three levels per reference and **fails loudly** on the first two: a missing or
unindexed reference is a defect to fix, while an existing but unreachable one is a measurement
to interpret (lexical mismatch, or a label that does not belong to its question).

Usage::

    uv run python scripts/verify_retrieval_references.py
    NEO4J_URI=bolt://localhost:17687 uv run python scripts/verify_retrieval_references.py

The query it replays is the one the layer uses: ``Neo4jQueryAdapter.search_chunks`` with the
same limits the gate evaluates (``retrieval_top_k`` and the approved over-fetch).
"""

from __future__ import annotations

import argparse
import asyncio
import unicodedata
from typing import Any

from neo4j import AsyncGraphDatabase

from book_graph_rag.config import Settings
from book_graph_rag.infrastructure.evaluation_dataset_loader import (
    JsonlManifestEvaluationDatasetLoader,
)
from book_graph_rag.infrastructure.neo4j_query_adapter import Neo4jQueryAdapter

# ``Chunk`` stores ``book_id`` and ``chunk_index``; the ``{book_id}:{chunk_index}`` identity the
# dataset uses is composed by ``Neo4jQueryAdapter._chunk_payload``, not stored as a property.
_EXISTENCE_CYPHER = (
    "MATCH (c:Chunk {book_id: $book_id, chunk_index: $chunk_index}) RETURN c.text AS text LIMIT 1"
)
_RANGE_CYPHER = (
    "MATCH (c:Chunk {book_id: $book_id}) "
    "RETURN count(c) AS chunks, min(c.chunk_index) AS lowest, max(c.chunk_index) AS highest"
)


def _split_reference(reference: str) -> tuple[str, int] | None:
    """Split ``{book_id}:{chunk_index}`` into its parts, or ``None`` if it is malformed."""
    book_id, _, index_text = reference.rpartition(":")
    if not book_id or not index_text.isdigit():
        return None
    return book_id, int(index_text)


def _index_fingerprint(text: str, words: int = 3) -> str:
    """Return a Lucene-safe phrase taken from the chunk's own text.

    Used to ask the index whether it can find this chunk at all: an unindexed chunk is
    unreachable for every question, which no amount of ranking can repair.
    """
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    tokens = [token.strip(".,;:()[]\"'`") for token in folded.split()]
    longest = sorted({token for token in tokens if len(token) >= 5}, key=len, reverse=True)
    return " AND ".join(longest[:words])


async def _reference_status(
    session: Any,
    *,
    reference: str,
) -> tuple[bool, str, str]:
    """Return ``(exists, text, note)`` for one reference, without writing anything.

    The identity rule is the adapter's: ``Chunk`` stores ``book_id`` and ``chunk_index``, and
    ``{book_id}:{chunk_index}`` is composed by ``Neo4jQueryAdapter``. This first asked for a
    ``chunk_id`` property, which does not exist, and made three healthy references look
    missing.
    """
    parts = _split_reference(reference)
    if parts is None:
        return False, "", "malformed id (expected {book_id}:{chunk_index})"
    book_id, chunk_index = parts

    record = await session.run(_EXISTENCE_CYPHER, book_id=book_id, chunk_index=chunk_index)
    row = await record.single()
    if row is None:
        range_record = await session.run(_RANGE_CYPHER, book_id=book_id)
        range_row = await range_record.single()
        if range_row is None or not range_row["chunks"]:
            return False, "", f"book {book_id} has no chunks at all"
        highest = range_row["highest"]
        if highest is None:
            return (
                False,
                "",
                f"not in the graph (book has {range_row['chunks']} chunks, none with a "
                "chunk_index)",
            )
        where = "beyond the last index" if chunk_index > int(highest) else "inside the range"
        return (
            False,
            "",
            f"not in the graph ({where}: book has {range_row['chunks']} chunks, "
            f"indices {range_row['lowest']}..{highest})",
        )
    text = str(row["text"] or "")
    return True, text, f"{len(text)} chars"


async def _run(settings: Settings, limits: tuple[int, ...]) -> int:
    dataset = JsonlManifestEvaluationDatasetLoader(settings.evaluation_manifest_path).load(
        "retrieval_dataset"
    )
    query_adapter = Neo4jQueryAdapter(settings)
    password = (
        settings.neo4j_password.get_secret_value()
        if hasattr(settings.neo4j_password, "get_secret_value")
        else str(settings.neo4j_password)
    )
    driver = AsyncGraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_user, password))

    total = reachable = missing = unindexed = 0
    try:
        for record in dataset.records:
            question_id = str(record.get("question_id", ""))
            question = str(record.get("question", ""))
            corpus = str(record.get("corpus", ""))
            references = [str(ref) for ref in (record.get("reference_context_ids") or [])]
            hits_by_limit: dict[int, list[str]] = {}
            for limit in limits:
                hits = await query_adapter.search_chunks(question, limit=limit)
                hits_by_limit[limit] = [str(hit.get("chunk_id")) for hit in hits]

            per_limit = "  ".join(
                f"@{limit} {sum(1 for ref in references if ref in hits_by_limit[limit])}"
                f"/{len(references)}"
                for limit in limits
            )
            unseen = [
                ref
                for ref in references
                if not any(ref in hits_by_limit[limit] for limit in limits)
            ]
            notes: list[str] = []
            async with driver.session() as session:
                for ref in unseen:
                    exists, text, note = await _reference_status(session, reference=ref)
                    indexed = False
                    if exists:
                        # Index membership through the very query the layer runs, so the id is
                        # composed by the adapter instead of being rebuilt here.
                        fingerprint = _index_fingerprint(text)
                        if fingerprint:
                            hits = await query_adapter.search_chunks(fingerprint, limit=50)
                            indexed = any(str(hit.get("chunk_id")) == ref for hit in hits)
                        note = f"{note}, reachable for its own text={indexed}"
                    notes.append(f"{ref} -> exists={exists} ({note})")
                    if not exists:
                        missing += 1
                    elif not indexed:
                        unindexed += 1

            if corpus != "current":
                print(
                    f"{question_id}: corpus={corpus} (not scored by the gate), "
                    f"refs={len(references)}"
                )
                continue
            total += len(references)
            reachable += sum(1 for ref in references if ref not in unseen)
            print(f"{question_id}: refs={len(references)}  {per_limit}")
            for note in notes:
                print(f"    never returned: {note}")

        print(
            f"\nsummary (current corpus only): references={total} reachable={reachable} "
            f"never-returned={total - reachable} missing-from-graph={missing} "
            f"exists-but-not-indexed={unindexed}"
        )
    finally:
        await query_adapter.close()
        await driver.close()

    structural = missing + unindexed
    if structural:
        print(
            "FAIL: a reference that does not exist or is not indexed can never be retrieved; "
            "fix the dataset or the indexing before reading any precision number."
        )
        return 1
    print(
        "OK: every reference exists and is indexed. The ones that never return are a ranking "
        "or label question, not a structural one."
    )
    return 0


def main() -> int:
    """Entrypoint: verify the committed retrieval labels against the reachable graph."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limits",
        type=int,
        nargs="+",
        default=None,
        help="Fetch limits to replay (default: retrieval_top_k, the approved over-fetch, 50).",
    )
    args = parser.parse_args()
    settings = Settings.model_validate({})
    limits = tuple(args.limits) if args.limits else (settings.retrieval_top_k, 40, 50)
    return asyncio.run(_run(settings, limits))


if __name__ == "__main__":
    raise SystemExit(main())
