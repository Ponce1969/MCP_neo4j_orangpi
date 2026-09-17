"""Tests for JsonFileQueryLoggerAdapter (REQ-07.7, REQ-07.8)."""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from book_graph_rag.config import Settings
from book_graph_rag.domain.mcp_security import QueryFingerprint
from book_graph_rag.domain.models import QueryLogEntry
from book_graph_rag.infrastructure.logging.json_query_logger_adapter import (
    JsonFileQueryLoggerAdapter,
)


def _make_entry(**overrides: Any) -> QueryLogEntry:
    defaults: dict[str, Any] = {
        "timestamp": datetime.now(tz=UTC),
        "tool_name": "find_entity",
        "query_type": "entity",
        "query_metadata": {"name_set": True, "param_count": 1},
        "result_count": 1,
        "zero_results": False,
        "entity_not_found": False,
        "duration_ms": 45.0,
        "error_code": None,
    }
    defaults.update(overrides)
    return QueryLogEntry(**defaults)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings.model_validate(
        {
            "neo4j_uri": "bolt://localhost",
            "neo4j_user": "neo4j",
            "neo4j_password": "secret",
            "mcp_log_path": tmp_path / "mcp_queries.jsonl",
            "mcp_log_retention_days": 7,
        }
    )


@pytest.fixture
def adapter(settings: Settings) -> JsonFileQueryLoggerAdapter:
    return JsonFileQueryLoggerAdapter(settings)


async def test_log_query_writes_json_line_with_all_fields(
    adapter: JsonFileQueryLoggerAdapter, settings: Settings
) -> None:
    entry = _make_entry()

    await adapter.log_query(entry)
    await adapter.close()

    lines = settings.mcp_log_path.read_text().strip().splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["timestamp"] == entry.model_dump(mode="json")["timestamp"]
    assert parsed["tool_name"] == "find_entity"
    assert parsed["query_type"] == "entity"
    assert parsed["query_metadata"] == {"name_set": True, "param_count": 1}
    assert parsed["result_count"] == 1
    assert parsed["zero_results"] is False
    assert parsed["entity_not_found"] is False
    assert parsed["duration_ms"] == 45.0
    assert parsed["error_code"] is None


async def test_multiple_log_queries_append_lines(
    adapter: JsonFileQueryLoggerAdapter, settings: Settings
) -> None:
    await adapter.log_query(_make_entry(result_count=1))
    await adapter.log_query(_make_entry(tool_name="count_entities", result_count=2))
    await adapter.close()

    lines = settings.mcp_log_path.read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["result_count"] == 1
    assert json.loads(lines[1])["tool_name"] == "count_entities"


async def test_log_line_is_valid_json_parsable_to_query_log_entry(
    adapter: JsonFileQueryLoggerAdapter, settings: Settings
) -> None:
    entry = _make_entry(
        tool_name="search_chunks",
        query_type="similarity",
        query_metadata={"query_set": True, "limit": 10, "param_count": 2},
        result_count=3,
        duration_ms=12.5,
    )

    await adapter.log_query(entry)
    await adapter.close()

    lines = settings.mcp_log_path.read_text().strip().splitlines()
    parsed = json.loads(lines[0])
    assert parsed == entry.model_dump(mode="json")


async def test_rotation_creates_new_file_at_midnight(
    adapter: JsonFileQueryLoggerAdapter, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    await adapter.log_query(_make_entry(tool_name="find_entity"))

    future = time.time() + 2 * 24 * 3600 + 1
    monkeypatch.setattr(time, "time", lambda: future)

    await adapter.log_query(_make_entry(tool_name="count_entities"))
    await adapter.close()

    log_dir = settings.mcp_log_path.parent
    rotated_files = [p for p in log_dir.iterdir() if p != settings.mcp_log_path]
    assert len(rotated_files) == 1

    rotated_lines = rotated_files[0].read_text().strip().splitlines()
    assert len(rotated_lines) == 1
    assert json.loads(rotated_lines[0])["tool_name"] == "find_entity"

    base_lines = settings.mcp_log_path.read_text().strip().splitlines()
    assert len(base_lines) == 1
    assert json.loads(base_lines[0])["tool_name"] == "count_entities"


async def test_init_deletes_expired_rotated_files(tmp_path: Path) -> None:
    base_path = tmp_path / "mcp_queries.jsonl"
    base_path.touch()

    old_date = datetime.now(tz=UTC) - timedelta(days=8)
    old_rotated = tmp_path / f"{base_path.name}.{old_date.strftime('%Y-%m-%d')}"
    old_timestamp = old_date.timestamp()
    old_rotated.touch()
    os.utime(old_rotated, (old_timestamp, old_timestamp))

    recent_date = datetime.now(tz=UTC) - timedelta(days=3)
    recent_rotated = tmp_path / f"{base_path.name}.{recent_date.strftime('%Y-%m-%d')}"
    recent_rotated.touch()

    settings = Settings.model_validate(
        {
            "neo4j_uri": "bolt://localhost",
            "neo4j_user": "neo4j",
            "neo4j_password": "secret",
            "mcp_log_path": base_path,
            "mcp_log_retention_days": 7,
        }
    )

    adapter = JsonFileQueryLoggerAdapter(settings)
    await adapter.close()

    assert not old_rotated.exists()
    assert recent_rotated.exists()
    assert base_path.exists()


async def test_log_query_serializes_non_null_error_code(
    adapter: JsonFileQueryLoggerAdapter, settings: Settings
) -> None:
    entry = _make_entry(error_code="TimeoutError")

    await adapter.log_query(entry)
    await adapter.close()

    serialized = settings.mcp_log_path.read_text()
    parsed = json.loads(serialized.strip().splitlines()[0])
    assert parsed["error_code"] == "TimeoutError"
    assert "error" not in parsed
    assert "Neo4j connection timeout" not in serialized


async def test_close_prevents_further_writes(
    adapter: JsonFileQueryLoggerAdapter, settings: Settings
) -> None:
    await adapter.log_query(_make_entry())
    await adapter.close()

    with pytest.raises(RuntimeError, match="closed"):
        await adapter.log_query(_make_entry())

    lines = settings.mcp_log_path.read_text().strip().splitlines()
    assert len(lines) == 1


async def test_init_preserves_base_file_older_than_retention(tmp_path: Path) -> None:
    base_path = tmp_path / "mcp_queries.jsonl"
    old_timestamp = (datetime.now(tz=UTC) - timedelta(days=30)).timestamp()
    base_path.touch()
    os.utime(base_path, (old_timestamp, old_timestamp))

    settings = Settings.model_validate(
        {
            "neo4j_uri": "bolt://localhost",
            "neo4j_user": "neo4j",
            "neo4j_password": "secret",
            "mcp_log_path": base_path,
            "mcp_log_retention_days": 7,
        }
    )

    adapter = JsonFileQueryLoggerAdapter(settings)
    await adapter.close()

    assert base_path.exists()


async def test_init_deletes_multiple_expired_rotated_files(tmp_path: Path) -> None:
    base_path = tmp_path / "mcp_queries.jsonl"
    base_path.touch()
    old_files: list[Path] = []
    for days_ago in (8, 9, 15):
        old_date = datetime.now(tz=UTC) - timedelta(days=days_ago)
        old_rotated = tmp_path / f"{base_path.name}.{old_date.strftime('%Y-%m-%d')}"
        old_rotated.touch()
        old_timestamp = old_date.timestamp()
        os.utime(old_rotated, (old_timestamp, old_timestamp))
        old_files.append(old_rotated)

    settings = Settings.model_validate(
        {
            "neo4j_uri": "bolt://localhost",
            "neo4j_user": "neo4j",
            "neo4j_password": "secret",
            "mcp_log_path": base_path,
            "mcp_log_retention_days": 7,
        }
    )

    adapter = JsonFileQueryLoggerAdapter(settings)
    await adapter.close()

    assert all(not path.exists() for path in old_files)


def test_query_log_entry_schema_is_metadata_only() -> None:
    """QueryLogEntry must not expose raw query/prompt/error text fields (R5)."""
    fields = QueryLogEntry.model_fields
    assert "query_params" not in fields
    assert "error" not in fields
    assert "query_metadata" in fields
    assert "error_code" in fields
    assert "query_fingerprint" in fields
    assert "prompt_fingerprint" in fields


async def test_persisted_json_contains_no_raw_text_fields(
    adapter: JsonFileQueryLoggerAdapter, settings: Settings
) -> None:
    """The persisted line carries only metadata and fingerprint digests."""
    entry = _make_entry(
        query_metadata={"query_set": True, "limit": 10, "param_count": 2},
        query_fingerprint=QueryFingerprint(key_id="k1", fingerprint_hex="ab" * 32),
        prompt_fingerprint=QueryFingerprint(key_id="k1", fingerprint_hex="cd" * 32),
    )

    await adapter.log_query(entry)
    await adapter.close()

    serialized = settings.mcp_log_path.read_text()
    parsed = json.loads(serialized.strip().splitlines()[0])
    expected_keys = {
        "timestamp",
        "tool_name",
        "query_type",
        "query_metadata",
        "result_count",
        "zero_results",
        "entity_not_found",
        "duration_ms",
        "error_code",
        "query_fingerprint",
        "prompt_fingerprint",
    }
    assert set(parsed.keys()) == expected_keys
    assert parsed["query_fingerprint"]["fingerprint_hex"] == "ab" * 32
    assert parsed["prompt_fingerprint"]["fingerprint_hex"] == "cd" * 32
    assert parsed["query_fingerprint"]["algorithm"] == "hmac-sha256"
    # No raw free-text fields or values may appear anywhere in the persisted line.
    assert "query_params" not in serialized
    assert "secret prompt text" not in serialized
