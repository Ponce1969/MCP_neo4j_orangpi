"""Integration tests for the failed-chunk dead-letter writer."""

from __future__ import annotations

from pathlib import Path

import pytest

from book_graph_rag.infrastructure.dead_letter import JSONLDeadLetter


@pytest.mark.neo4j_integration
async def test_write_failed_chunk_appends_re_addressable_record(tmp_path: Path) -> None:
    """write_failed_chunk appends the design §1.3 record shape as JSONL."""
    path = tmp_path / "dead_letter_chunks.jsonl"
    dead_letter = JSONLDeadLetter(path)

    record = {
        "source_id": "agentic-patterns:agentic-architectural-patterns",
        "chunk_index": 42,
        "page_ref": {"start": 87, "end": 88},
        "source_version": "abcd1234abcd1234",
        "pipeline_version": "1.0.0",
        "model_version": "openai:gpt-4o-mini:2026-09-01",
        "schema_version": "1.0.0",
        "attempt": 3,
        "checkpoint_status": "FAILED",
        "error_type": "LLMExtractionError",
        "error_message": "model returned malformed JSON after retries",
    }

    await dead_letter.write_failed_chunk(record)

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    parsed = dead_letter._parse_line(lines[0])
    assert parsed["source_id"] == record["source_id"]
    assert parsed["chunk_index"] == record["chunk_index"]
    assert parsed["checkpoint_status"] == "FAILED"
    assert "timestamp" in parsed

    # A second write appends, not overwrites.
    await dead_letter.write_failed_chunk({**record, "chunk_index": 43})
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2


@pytest.mark.neo4j_integration
async def test_write_failed_chunk_keeps_orphan_path_separate(tmp_path: Path) -> None:
    """The same adapter instance can write to two different JSONL paths."""
    chunk_path = tmp_path / "chunks.jsonl"
    orphan_path = tmp_path / "orphans.jsonl"
    dead_letter = JSONLDeadLetter(orphan_path, failed_chunk_path=chunk_path)

    orphan = {"reason": "orphan_endpoint", "missing_endpoint": "target"}
    chunk = {"source_id": "x:y", "chunk_index": 1, "checkpoint_status": "FAILED"}

    await dead_letter.write_orphan_relationship(orphan)
    await dead_letter.write_failed_chunk(chunk)

    assert chunk_path.exists()
    assert orphan_path.exists()
    chunk_lines = chunk_path.read_text(encoding="utf-8").strip().splitlines()
    orphan_lines = orphan_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(chunk_lines) == 1
    assert len(orphan_lines) == 1
