"""Truncation logic for MCP OranPi tool responses.

Implements the global output truncation policy defined in spec 005.
All truncation happens at the application layer, not the transport layer.

Three truncation modes:

1. **Text truncation** — for log/journal output. Drops oldest lines first.
2. **List truncation** — for containers, ports, services. Keeps most recent.
3. **Field truncation** — for any single field that exceeds the byte limit.

If the total serialized JSON exceeds ``max_payload_bytes``, the tool
must return a ``VALID_PAYLOAD_EXCEEDED`` error instead of partial data.
"""

from __future__ import annotations

from mcp_oranpi.domain.errors import VALID_PAYLOAD_EXCEEDED, ToolError, ToolResult
from mcp_oranpi.domain.models import TruncationMeta

# ── Constants ────────────────────────────────────────────────────────────────

MAX_LIST_LENGTH: int = 500
MAX_SINGLE_FIELD_BYTES: int = 100 * 1024  # 100 KB
DEFAULT_MAX_PAYLOAD_BYTES: int = 1024 * 1024  # 1 MB
DEFAULT_LOG_OUTPUT_BYTES: int = 50 * 1024  # 50 KB


# ── Text Truncation ──────────────────────────────────────────────────────────


def truncate_text(
    text: str,
    max_bytes: int = DEFAULT_LOG_OUTPUT_BYTES,
) -> tuple[str, TruncationMeta]:
    """Truncate text output to a byte budget.

    Drops lines from the top (oldest) to stay within the byte limit.
    This preserves the most recent output, which is typically more relevant
    for log tailing scenarios.

    Args:
        text: The raw text output (e.g., docker logs, journal output).
        max_bytes: Maximum byte budget (default: 50 KB).

    Returns:
        A tuple of (truncated_text, TruncationMeta).
    """
    encoded = text.encode("utf-8")

    if len(encoded) <= max_bytes:
        return text, TruncationMeta(truncated=False)

    # Split into lines and keep as many from the bottom as fit.
    lines = text.splitlines(keepends=True)
    kept_lines: list[str] = list()  # built dynamically below
    current_bytes = 0

    # Walk backwards to keep the most recent lines.
    for line in reversed(lines):
        line_bytes = len(line.encode("utf-8"))
        if current_bytes + line_bytes > max_bytes:
            break
        kept_lines.append(line)
        current_bytes += line_bytes

    kept_lines.reverse()
    truncated_text = "".join(kept_lines)

    original_count = len(lines)
    returned_count = len(kept_lines)

    return truncated_text, TruncationMeta(
        truncated=True,
        total_count=original_count,
        returned_count=returned_count,
        bytes_limit=max_bytes,
    )


# ── List Truncation ──────────────────────────────────────────────────────────


def truncate_list[T](
    items: list[T],
    max_length: int = MAX_LIST_LENGTH,
) -> tuple[list[T], TruncationMeta]:
    """Truncate a list to a maximum item count.

    Keeps the last ``max_length`` items (most recent).

    Args:
        items: The full list of items.
        max_length: Maximum number of items to return (default: 500).

    Returns:
        A tuple of (truncated_list, TruncationMeta).
    """
    total_count = len(items)

    if total_count <= max_length:
        return items, TruncationMeta(
            truncated=False,
            total_count=total_count,
            returned_count=total_count,
        )

    truncated = items[-max_length:]

    return truncated, TruncationMeta(
        truncated=True,
        total_count=total_count,
        returned_count=max_length,
        bytes_limit=0,
    )


# ── Field Truncation ─────────────────────────────────────────────────────────


def truncate_field(
    value: str,
    max_bytes: int = MAX_SINGLE_FIELD_BYTES,
) -> tuple[str, TruncationMeta]:
    """Truncate a single field value to a byte budget.

    Cuts at the last complete UTF-8 character boundary before the limit.

    Args:
        value: The field value string.
        max_bytes: Maximum byte budget (default: 100 KB).

    Returns:
        A tuple of (truncated_value, TruncationMeta).
    """
    encoded = value.encode("utf-8")

    if len(encoded) <= max_bytes:
        return value, TruncationMeta(truncated=False)

    # Find the last safe UTF-8 boundary at or before max_bytes.
    cut_point = max_bytes
    while cut_point > 0 and (encoded[cut_point] & 0xC0) == 0x80:
        cut_point -= 1

    truncated_bytes = encoded[:cut_point]
    truncated_value = truncated_bytes.decode("utf-8", errors="replace")

    return truncated_value, TruncationMeta(
        truncated=True,
        bytes_limit=max_bytes,
    )


# ── Payload Guard ────────────────────────────────────────────────────────────


def check_payload_size(
    data: dict[str, object],
    max_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
) -> ToolResult | None:
    """Check if a payload exceeds the size budget.

    If the serialized JSON exceeds ``max_bytes``, returns a
    ``VALID_PAYLOAD_EXCEEDED`` error wrapped in a ``ToolResult``.
    The caller should return this directly instead of the successful data.

    If the payload is within budget, returns ``None`` — the caller
    should proceed normally.

    Args:
        data: The response data dict to check.
        max_bytes: Maximum allowed payload size in bytes (default: 1 MB).

    Returns:
        A ToolResult with VALID_PAYLOAD_EXCEEDED error if oversized,
        or None if the payload fits.
    """
    import json

    try:
        payload_bytes = len(json.dumps(data).encode("utf-8"))
    except (TypeError, ValueError):
        # If data can't be serialized, let it fail downstream
        # where a proper serialization error will surface.
        return None

    if payload_bytes > max_bytes:
        error = ToolError(
            code=VALID_PAYLOAD_EXCEEDED,
            message=(
                f"Response payload ({payload_bytes} bytes) exceeds "
                f"the maximum allowed size ({max_bytes} bytes). "
                "Narrow the query (e.g., smaller tail, narrower port range)."
            ),
            detail={
                "payload_bytes": payload_bytes,
                "max_bytes": max_bytes,
            },
        )
        return ToolResult(data=None, error=error)

    return None
