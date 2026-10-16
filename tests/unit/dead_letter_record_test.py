"""Unit tests for the re-addressable dead-letter record."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from book_graph_rag.domain.checkpoint_models import (
    CheckpointStatus,
    FailedChunkRecord,
    parse_failed_chunk,
)
from book_graph_rag.domain.models import PageRef


@pytest.fixture
def valid_record() -> dict[str, Any]:
    return {
        "source_id": "agentic-patterns:test",
        "chunk_index": 42,
        "page_ref": {"start": 87, "end": 88},
        "source_version": "ab12cd34",
        "pipeline_version": "1.0.0",
        "model_version": "openai:gpt-4o-mini:2026-09-01",
        "schema_version": "1.0.0",
        "attempt": 3,
        "checkpoint_status": "FAILED",
        "error_type": "LLMExtractionError",
        "error_message": "model returned malformed JSON after retries",
        "timestamp": datetime.now(UTC).isoformat(),
    }


@pytest.mark.parametrize(
    "missing_field",
    [
        "source_id",
        "chunk_index",
        "page_ref",
        "source_version",
        "pipeline_version",
        "model_version",
        "schema_version",
        "attempt",
        "checkpoint_status",
        "error_type",
    ],
)
def test_failed_chunk_record_requires_all_fields(
    valid_record: dict[str, Any], missing_field: str
) -> None:
    """Omitting any required field must fail validation."""
    invalid = {k: v for k, v in valid_record.items() if k != missing_field}
    with pytest.raises(ValidationError):
        FailedChunkRecord.model_validate(invalid)


def test_failed_chunk_record_accepts_complete_shape(valid_record: dict[str, Any]) -> None:
    """The design §1.3 record shape round-trips through the model."""
    record = FailedChunkRecord.model_validate(valid_record)
    assert record.source_id == "agentic-patterns:test"
    assert record.chunk_index == 42
    assert record.page_ref == PageRef(start=87, end=88)
    assert record.checkpoint_status == CheckpointStatus.FAILED
    assert record.attempt == 3


def test_a_legacy_record_carrying_the_raw_message_still_reads(valid_record: dict[str, Any]) -> None:
    """R5 drops the message from new records, but an existing file must keep replaying."""
    record = FailedChunkRecord.model_validate(valid_record)

    assert record.error_message == "model returned malformed JSON after retries"
    assert parse_failed_chunk(valid_record) == parse_failed_chunk(
        {k: v for k, v in valid_record.items() if k != "error_message"}
    )


def test_the_redacted_shape_carries_no_raw_text(valid_record: dict[str, Any]) -> None:
    """What this code writes from now on: type, length and a keyed fingerprint only."""
    payload = {k: v for k, v in valid_record.items() if k != "error_message"} | {"error_length": 41}
    record = FailedChunkRecord.model_validate(payload)
    dumped = record.model_dump(mode="json")

    assert dumped["error_message"] is None
    assert dumped["error_length"] == 41
    assert dumped["error_fingerprint"] is None
    assert "model returned malformed JSON after retries" not in json.dumps(dumped)


def test_parse_failed_chunk_extracts_identity(valid_record: dict[str, Any]) -> None:
    """parse_failed_chunk returns only the information needed to replay."""
    replayable = parse_failed_chunk(valid_record)
    assert replayable.source_id == valid_record["source_id"]
    assert replayable.chunk_index == valid_record["chunk_index"]
    assert replayable.versions.source_version == valid_record["source_version"]
