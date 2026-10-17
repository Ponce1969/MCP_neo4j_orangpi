"""Pure domain models for resumable indexing checkpoints."""

from __future__ import annotations

import argparse
from contextlib import suppress
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from book_graph_rag.domain.mcp_security import QueryFingerprint, effective_token
from book_graph_rag.domain.models import PageRef

# argparse.ArgumentError is raised when exit_on_error=False and a mutually
# exclusive group is violated; we re-export it locally for cleaner handling.
_ReplayArgumentError = argparse.ArgumentError


class CheckpointStatus(StrEnum):
    """Lifecycle states of a chunk checkpoint."""

    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    PROCESSED = "PROCESSED"
    FAILED = "FAILED"
    STALE = "STALE"


class VersionDimensions(BaseModel):
    """The four version dimensions that decide whether a checkpoint is stale."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_version: str = Field(description="sha256(pdf_bytes) first 16 hex chars")
    pipeline_version: str = Field(description="chunker + prompt version")
    model_version: str = Field(description="provider:model:yyyy-mm-dd")
    schema_version: str = Field(description="graph schema revision")


class Checkpoint(BaseModel):
    """A checkpoint row keyed by (source_id, chunk_index)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str = Field(description="corpus:source namespace")
    chunk_index: int = Field(ge=0)
    status: CheckpointStatus
    attempt: int = Field(default=0, ge=0)
    versions: VersionDimensions
    leased_at: datetime | None = None
    processed_at: datetime | None = None
    failed_at: datetime | None = None
    error_type: str | None = None
    error_length: int | None = Field(default=None, ge=0)
    error_fingerprint: QueryFingerprint | None = None
    updated_at: datetime


class LeaseResult(BaseModel):
    """Result of acquiring or refreshing a checkpoint lease."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    checkpoint: Checkpoint
    is_new_lease: bool


class ReplayableChunk(BaseModel):
    """Minimal information needed to re-queue a failed chunk."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str
    chunk_index: int
    versions: VersionDimensions


class RedactedError(BaseModel):
    """What is safe to persist about a failure: class name, length, keyed fingerprint.

    R5 of the security register: the raw text of an exception is never stored, because an
    adapter's error can carry prompts, URLs, corpus text or credentials. ``error_type`` and
    ``error_length`` keep the failure classifiable and measurable, and ``error_fingerprint``
    correlates repetitions of the same message without revealing it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    error_type: str = Field(min_length=1)
    error_length: int = Field(ge=0)
    error_fingerprint: QueryFingerprint | None = None


_ERROR_LABEL_MAX_LENGTH = 80


def _sanitize_error_label(value: str, *, fallback: str = "", identifier_only: bool = False) -> str:
    """Keep emitted labels printable, whitespace-collapsed, and short.

    80 characters keeps report labels compact while bounding attacker-controlled exception and
    key names. ``identifier_only`` is used for the exception class name, which **is** an
    identifier by construction (``type(error).__name__`` cannot contain a space, a dash or an
    equals sign), so keeping only identifier characters is lossless there and strips the
    punctuation a secret would need to stay readable if a dynamically created class ever carried
    one. The key id is a label the operator reads (``mcp-log-v1``) and keeps its dashes.
    """
    printable = "".join(character if character.isprintable() else " " for character in value)
    collapsed = " ".join(printable.split())
    if identifier_only:
        collapsed = "".join(
            character for character in collapsed if character.isalnum() or character == "_"
        )
    return collapsed[:_ERROR_LABEL_MAX_LENGTH].strip() or fallback


def redacted_error(
    error: BaseException,
    *,
    key_id: str,
    secret: str | None,
) -> RedactedError:
    """Reduce a failure to type, length and a keyed fingerprint — never its text.

    One rule for every writer (the checkpoint transition, the dead-letter record and the legacy
    JSONL sink), so no path can keep persisting the message.

    The fingerprint uses the same key and the same shape rule as the MCP query log
    (``MCP_HMAC_KEY`` through :func:`~book_graph_rag.domain.mcp_security.effective_token`), which
    is what keeps it from being decorative: a plain digest of guessable driver text would be
    reversible. When the key is missing, weak or unnamed the fingerprint is simply absent — the
    text is dropped either way, and an indexing run is never aborted for want of a logging key
    (unlike the MCP log path, whose key is validated at boot).
    """
    try:
        raw = str(error)
    except BaseException:  # noqa: BLE001 - exception stringification is untrusted
        raw = ""

    error_type = _sanitize_error_label(
        type(error).__name__, fallback="UnknownError", identifier_only=True
    )
    safe_key_id = _sanitize_error_label(key_id)
    key = effective_token(secret)
    fingerprint: QueryFingerprint | None = None
    if key and safe_key_id:
        with suppress(Exception):
            fingerprint = QueryFingerprint.from_canonical(
                safe_key_id,
                key,
                {"error_message": raw},
            )
    return RedactedError(
        error_type=error_type,
        error_length=len(raw),
        error_fingerprint=fingerprint,
    )


def format_redacted_error(
    error: BaseException,
    *,
    key_id: str,
    secret: str | None,
) -> str:
    """Render safe metadata with printable, whitespace-collapsed labels capped at 80 chars."""
    redacted = redacted_error(error, key_id=key_id, secret=secret)
    details = [f"message_length={redacted.error_length}"]
    if redacted.error_fingerprint is not None:
        fingerprint = redacted.error_fingerprint
        details.append(f"fingerprint={fingerprint.key_id}:{fingerprint.fingerprint_hex}")
    return f"{redacted.error_type} ({', '.join(details)})"


class FailedChunkRecord(BaseModel):
    """Re-addressable dead-letter record for a failed chunk.

    ``error_message`` is legacy: records written before R5 carry the raw exception text and are
    still read (and replayed) unchanged, but this code never writes it again — the error is
    described by ``error_type``, ``error_length`` and ``error_fingerprint``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str
    chunk_index: int = Field(ge=0)
    page_ref: PageRef
    source_version: str
    pipeline_version: str
    model_version: str
    schema_version: str
    attempt: int = Field(ge=0)
    checkpoint_status: CheckpointStatus
    error_type: str
    error_length: int | None = Field(default=None, ge=0)
    error_fingerprint: QueryFingerprint | None = None
    error_message: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


def parse_failed_chunk(record: dict[str, Any]) -> ReplayableChunk:
    """Extract the replayable identity from a failed-chunk dead-letter record."""
    parsed = FailedChunkRecord.model_validate(record)
    return ReplayableChunk(
        source_id=parsed.source_id,
        chunk_index=parsed.chunk_index,
        versions=VersionDimensions(
            source_version=parsed.source_version,
            pipeline_version=parsed.pipeline_version,
            model_version=parsed.model_version,
            schema_version=parsed.schema_version,
        ),
    )


class ReplayCommand(BaseModel):
    """Normalized representation of the index sub-command flag combinations."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: Literal["resume", "no_resume", "replay_dead_letter", "backfill_checkpoints"]
    force_reprocess: bool = False
    limit: int | None = Field(default=None, ge=1)
    source_id: str | None = None
    dry_run: bool = False


def parse_replay_command(argv: list[str] | None = None) -> ReplayCommand:
    """Parse replay/admin flag combinations into a typed command object.

    Raises ``ValueError`` for mutually-exclusive or context-invalid combinations.
    """
    parser = argparse.ArgumentParser(prog="book-graph-rag index", exit_on_error=False)
    parser.add_argument("--resume", dest="resume", action="store_true", default=True)
    parser.add_argument("--no-resume", dest="resume", action="store_false")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--replay-dead-letter",
        dest="mode",
        action="store_const",
        const="replay_dead_letter",
    )
    mode_group.add_argument(
        "--backfill-checkpoints",
        dest="mode",
        action="store_const",
        const="backfill_checkpoints",
    )
    parser.add_argument("--force-reprocess", action="store_true", default=False)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--source-id", default=None)
    parser.add_argument("--dry-run", action="store_true", default=False)

    try:
        parsed = parser.parse_args(argv)
    except (SystemExit, _ReplayArgumentError) as exc:
        raise ValueError(f"Invalid replay command flags: {exc}") from exc

    # Map the boolean resume flag into the mode namespace.
    if parsed.resume:
        mode: Literal["resume", "no_resume", "replay_dead_letter", "backfill_checkpoints"] = (
            parsed.mode or "resume"
        )
    else:
        if parsed.mode is not None:
            raise ValueError(
                "--no-resume cannot be combined with --replay-dead-letter or --backfill-checkpoints"
            )
        mode = "no_resume"

    # Context-sensitive validation that argparse does not express natively.
    if parsed.limit is not None and mode != "replay_dead_letter":
        raise ValueError("--limit is only valid with --replay-dead-letter")
    if parsed.dry_run and mode != "backfill_checkpoints":
        raise ValueError("--dry-run is only valid with --backfill-checkpoints")
    if parsed.limit is not None and parsed.limit < 1:
        raise ValueError("--limit must be a positive integer")

    return ReplayCommand(
        mode=mode,
        force_reprocess=parsed.force_reprocess,
        limit=parsed.limit,
        source_id=parsed.source_id,
        dry_run=parsed.dry_run,
    )
