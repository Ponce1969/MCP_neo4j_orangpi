"""Tests for JsonFileQueryLoggerAdapter (REQ-07.7, REQ-07.8)."""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

import pytest

from book_graph_rag.config import Settings
from book_graph_rag.domain.mcp_security import QueryFingerprint
from book_graph_rag.domain.models import QueryLogEntry
from book_graph_rag.infrastructure.logging.json_query_logger_adapter import (
    JsonFileQueryLoggerAdapter,
    read_query_log,
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
        "schema_version",
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


# ── Retention coverage (T-G.3): zero-byte files + exact 7-day boundary ──────


class _FrozenClock:
    """Freeze ``datetime.now`` while delegating ``fromtimestamp`` to the real class."""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self, tz: tzinfo | None = None) -> datetime:
        return self._now

    def fromtimestamp(self, ts: float, tz: tzinfo | None = None) -> datetime:
        return datetime.fromtimestamp(ts, tz=tz)


async def test_init_deletes_zero_byte_expired_rotated_file(tmp_path: Path) -> None:
    """A zero-byte rotated file older than retention is removed (T-G.3)."""
    base_path = tmp_path / "mcp_queries.jsonl"
    base_path.touch()

    old_date = datetime.now(tz=UTC) - timedelta(days=8)
    rotated = tmp_path / f"{base_path.name}.{old_date.strftime('%Y-%m-%d')}"
    rotated.touch()  # zero bytes
    old_timestamp = old_date.timestamp()
    os.utime(rotated, (old_timestamp, old_timestamp))

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

    assert not rotated.exists()
    assert base_path.exists()


async def test_init_preserves_zero_byte_rotated_file_within_retention(
    tmp_path: Path,
) -> None:
    """A zero-byte rotated file within retention is preserved (T-G.3)."""
    base_path = tmp_path / "mcp_queries.jsonl"
    base_path.touch()

    recent_date = datetime.now(tz=UTC) - timedelta(days=3)
    rotated = tmp_path / f"{base_path.name}.{recent_date.strftime('%Y-%m-%d')}"
    rotated.touch()  # zero bytes

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

    assert rotated.exists()
    assert base_path.exists()


async def test_init_retention_boundary_exact_seven_days(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exactly 7 days old is kept; anything strictly older is removed (T-G.3)."""
    base_path = tmp_path / "mcp_queries.jsonl"
    base_path.touch()

    frozen_now = datetime(2026, 6, 22, 12, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(
        "book_graph_rag.infrastructure.logging.json_query_logger_adapter.datetime",
        _FrozenClock(frozen_now),
    )
    cutoff = frozen_now - timedelta(days=7)

    at_boundary = tmp_path / f"{base_path.name}.2026-06-15"
    at_boundary.touch()
    os.utime(at_boundary, (cutoff.timestamp(), cutoff.timestamp()))

    just_older = tmp_path / f"{base_path.name}.2026-06-14"
    just_older.touch()
    older_ts = (cutoff - timedelta(seconds=1)).timestamp()
    os.utime(just_older, (older_ts, older_ts))

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

    assert at_boundary.exists()
    assert not just_older.exists()
    assert base_path.exists()


# ── Log reader + v1→v2 migration (T-G.3) ────────────────────────────────────


def test_read_query_log_reads_v2_entries(tmp_path: Path) -> None:
    """read_query_log parses v2 lines into QueryLogEntry instances."""
    entry = _make_entry()
    path = tmp_path / "mcp_queries.jsonl"
    path.write_text(entry.model_dump_json() + "\n", encoding="utf-8")

    result = read_query_log(path)

    assert result.skipped_lines == 0
    assert len(result.entries) == 1
    assert result.entries[0].model_dump(mode="json") == entry.model_dump(mode="json")
    assert result.entries[0].schema_version == 2


def test_read_query_log_migrates_v1_lines(tmp_path: Path) -> None:
    """read_query_log migrates legacy v1 lines to the metadata-only schema."""
    v1 = json.dumps(
        {
            "timestamp": "2026-06-22T14:30:00Z",
            "tool_name": "find_entity",
            "query_type": "entity",
            "query_params": {"name": "MCP", "limit": 10},
            "result_count": 1,
            "zero_results": False,
            "entity_not_found": False,
            "duration_ms": 45.0,
            "error": "TimeoutError",
        }
    )
    path = tmp_path / "mcp_queries.jsonl"
    path.write_text(v1 + "\n", encoding="utf-8")

    result = read_query_log(path)

    assert result.skipped_lines == 0
    entry = result.entries[0]
    assert entry.error_code == "TimeoutError"
    assert entry.query_metadata == {}
    assert entry.query_fingerprint is None
    assert entry.prompt_fingerprint is None
    assert entry.schema_version == 2
    assert entry.tool_name == "find_entity"


def test_read_query_log_skips_malformed_lines_and_counts_them(tmp_path: Path) -> None:
    """Malformed lines are skipped and counted, never blocking the read."""
    path = tmp_path / "mcp_queries.jsonl"
    path.write_text(
        "not-json\n"
        + _make_entry().model_dump_json() + "\n"
        + "{invalid json\n"
        + "[]\n"
        + "\n",  # blank line is ignored silently
        encoding="utf-8",
    )

    result = read_query_log(path)

    assert result.skipped_lines == 3
    assert len(result.entries) == 1


def test_read_query_log_missing_file_returns_empty(tmp_path: Path) -> None:
    """A missing log file yields an empty result, not an error."""
    result = read_query_log(tmp_path / "does_not_exist.jsonl")

    assert result.entries == []
    assert result.skipped_lines == 0


def test_read_query_log_never_modifies_source(tmp_path: Path) -> None:
    """Reading is strictly read-only: the source bytes are unchanged (T-G.3)."""
    content = _make_entry().model_dump_json() + "\n"
    path = tmp_path / "mcp_queries.jsonl"
    path.write_text(content, encoding="utf-8")
    before = path.read_text(encoding="utf-8")

    read_query_log(path)

    assert path.read_text(encoding="utf-8") == before
