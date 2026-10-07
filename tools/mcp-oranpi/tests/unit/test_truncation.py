"""Unit tests for domain truncation logic."""

from __future__ import annotations

import json

from mcp_oranpi.domain.errors import VALID_PAYLOAD_EXCEEDED, ToolResult
from mcp_oranpi.domain.truncation import (
    DEFAULT_LOG_OUTPUT_BYTES,
    DEFAULT_MAX_PAYLOAD_BYTES,
    MAX_LIST_LENGTH,
    MAX_SINGLE_FIELD_BYTES,
    check_payload_size,
    truncate_field,
    truncate_list,
    truncate_text,
)


class TestTruncateText:
    """Tests for text truncation."""

    def test_no_truncation_needed(self) -> None:
        """Short text is returned unchanged."""
        text = "Hello, world!"
        result, meta = truncate_text(text, max_bytes=1024)
        assert result == text
        assert meta.truncated is False

    def test_truncate_text_drops_oldest_lines(self) -> None:
        """Text truncation drops lines from the top (oldest)."""
        # Each line is ~100 bytes, so 500 lines ≈ 50KB
        lines = [f"Line {i}: " + "x" * 90 for i in range(500)]
        text = "\n".join(lines)
        # 10KB budget should keep only the most recent ~100 lines
        result, meta = truncate_text(text, max_bytes=10000)
        assert meta.truncated is True
        assert meta.total_count == 500
        assert meta.returned_count is not None
        assert meta.returned_count < 500
        # Most recent lines should be preserved
        assert "Line 499" in result
        # Oldest lines should be dropped
        assert "Line 0" not in result

    def test_truncate_text_exactly_at_limit(self) -> None:
        """Text exactly at the byte limit is not truncated."""
        text = "A" * 50
        result, meta = truncate_text(text, max_bytes=50)
        assert meta.truncated is False
        assert result == text

    def test_truncate_empty_text(self) -> None:
        """Empty text is returned unchanged."""
        result, meta = truncate_text("", max_bytes=1024)
        assert result == ""
        assert meta.truncated is False

    def test_truncate_single_long_line(self) -> None:
        """A single line exceeding the limit is truncated."""
        long_line = "X" * 100000
        result, meta = truncate_text(long_line, max_bytes=50000)
        assert meta.truncated is True
        assert len(result.encode("utf-8")) <= 50000

    def test_default_max_bytes_is_50kb(self) -> None:
        """Default max_bytes for text truncation is 50 KB."""
        assert DEFAULT_LOG_OUTPUT_BYTES == 50 * 1024


class TestTruncateList:
    """Tests for list truncation."""

    def test_small_list_no_truncation(self) -> None:
        """Lists smaller than max_length are returned unchanged."""
        items = [1, 2, 3, 4, 5]
        result, meta = truncate_list(items, max_length=10)
        assert result == items
        assert meta.truncated is False
        assert meta.total_count == 5
        assert meta.returned_count == 5

    def test_large_list_truncated(self) -> None:
        """Lists exceeding max_length keep the most recent items."""
        items = list(range(1000))
        result, meta = truncate_list(items, max_length=500)
        assert meta.truncated is True
        assert meta.total_count == 1000
        assert meta.returned_count == 500
        # Most recent items preserved
        assert result[0] == 500
        assert result[-1] == 999

    def test_list_exactly_at_limit(self) -> None:
        """Lists exactly at max_length are not truncated."""
        items = list(range(500))
        result, meta = truncate_list(items, max_length=500)
        assert meta.truncated is False
        assert meta.total_count == 500
        assert meta.returned_count == 500

    def test_default_max_length_is_500(self) -> None:
        """Default max list length is 500 items."""
        assert MAX_LIST_LENGTH == 500

    def test_empty_list(self) -> None:
        """Empty list is returned unchanged."""
        result, meta = truncate_list([], max_length=10)  # type: ignore
        assert result == []
        assert meta.truncated is False
        assert meta.total_count == 0

    def test_truncated_keeps_tail(self) -> None:
        """Truncation keeps the tail (most recent) of the list."""
        items = list(range(20))
        result, meta = truncate_list(items, max_length=10)
        assert result == list(range(10, 20))
        assert meta.truncated is True
        assert meta.total_count == 20
        assert meta.returned_count == 10


class TestTruncateField:
    """Tests for single field truncation."""

    def test_short_field_no_truncation(self) -> None:
        """Short field values are returned unchanged."""
        result, meta = truncate_field("hello", max_bytes=1024)
        assert result == "hello"
        assert meta.truncated is False

    def test_long_field_truncated(self) -> None:
        """Long field values are truncated to the byte limit."""
        value = "A" * 200000
        result, meta = truncate_field(value, max_bytes=50000)
        assert meta.truncated is True
        assert len(result.encode("utf-8")) <= 50000

    def test_truncate_preserves_utf8_boundary(self) -> None:
        """Truncation cuts at UTF-8 character boundaries."""
        # Each emoji is 4 bytes in UTF-8, 10000 emojis = 40KB
        value = "\U0001f389" * 10000
        result, meta = truncate_field(value, max_bytes=5000)
        assert meta.truncated is True
        # The result should be valid UTF-8
        result.encode("utf-8").decode("utf-8")

    def test_default_max_bytes_is_100kb(self) -> None:
        """Default max field size is 100 KB."""
        assert MAX_SINGLE_FIELD_BYTES == 100 * 1024


class TestCheckPayloadSize:
    """Tests for payload size checking."""

    def test_small_payload_passes(self) -> None:
        """Small payloads return None (no error)."""
        data = {"containers": [], "truncated": False}
        result = check_payload_size(data, max_bytes=1024)
        assert result is None

    def test_oversized_payload_returns_error(self) -> None:
        """Oversized payloads return a ToolResult with VALID_PAYLOAD_EXCEEDED."""
        data = {"items": ["x" * 10000]}
        result = check_payload_size(data, max_bytes=100)  # type: ignore
        assert result is not None
        assert isinstance(result, ToolResult)
        assert result.error is not None
        assert result.error.code == VALID_PAYLOAD_EXCEEDED
        assert result.error.detail is not None
        assert "payload_bytes" in result.error.detail

    def test_exactly_at_limit_passes(self) -> None:
        """Payloads exactly at the limit pass (no error)."""
        data = {"key": "value"}
        payload_bytes = len(json.dumps(data).encode("utf-8"))
        result = check_payload_size(data, max_bytes=payload_bytes)  # type: ignore
        assert result is None

    def test_default_max_is_1mb(self) -> None:
        """Default max payload size is 1 MB."""
        assert DEFAULT_MAX_PAYLOAD_BYTES == 1024 * 1024
