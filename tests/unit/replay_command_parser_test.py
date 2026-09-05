"""Pure parser tests for replay/admin CLI flag combinations."""

from __future__ import annotations

import pytest

from book_graph_rag.domain.checkpoint_models import parse_replay_command


def test_empty_args_defaults_to_resume() -> None:
    """No flags means a normal resumable index run."""
    command = parse_replay_command([])
    assert command.mode == "resume"
    assert command.force_reprocess is False
    assert command.limit is None
    assert command.source_id is None
    assert command.dry_run is False


def test_explicit_resume_is_allowed() -> None:
    """--resume is the default and may be passed explicitly."""
    command = parse_replay_command(["--resume"])
    assert command.mode == "resume"


def test_no_resume_legacy_mode() -> None:
    """--no-resume selects the legacy one-shot flush path."""
    command = parse_replay_command(["--no-resume"])
    assert command.mode == "no_resume"


def test_replay_dead_letter_with_limit_and_source() -> None:
    """--replay-dead-letter accepts optional --limit and --source-id."""
    command = parse_replay_command(
        [
            "--replay-dead-letter",
            "--limit",
            "10",
            "--source-id",
            "corpus:source",
        ]
    )
    assert command.mode == "replay_dead_letter"
    assert command.limit == 10
    assert command.source_id == "corpus:source"
    assert command.dry_run is False


def test_backfill_checkpoints_with_source_and_dry_run() -> None:
    """--backfill-checkpoints accepts optional --source-id and --dry-run."""
    command = parse_replay_command(
        [
            "--backfill-checkpoints",
            "--source-id",
            "corpus:source",
            "--dry-run",
        ]
    )
    assert command.mode == "backfill_checkpoints"
    assert command.source_id == "corpus:source"
    assert command.dry_run is True


def test_force_reprocess_can_be_set() -> None:
    """--force-reprocess is allowed in any mode."""
    assert parse_replay_command(["--force-reprocess"]).force_reprocess is True
    assert (
        parse_replay_command(["--replay-dead-letter", "--force-reprocess"]).force_reprocess
        is True
    )
    assert (
        parse_replay_command(["--backfill-checkpoints", "--force-reprocess"]).force_reprocess
        is True
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["--replay-dead-letter", "--backfill-checkpoints"],
        ["--replay-dead-letter", "--no-resume"],
        ["--backfill-checkpoints", "--no-resume"],
        ["--backfill-checkpoints", "--limit", "5"],
        ["--replay-dead-letter", "--dry-run"],
        ["--no-resume", "--dry-run"],
        ["--replay-dead-letter", "--limit", "0"],
        ["--replay-dead-letter", "--limit", "-1"],
    ],
)
def test_mutually_exclusive_or_context_invalid_combos_raise(argv: list[str]) -> None:
    """Invalid flag combinations are rejected with a clear ValueError."""
    with pytest.raises(ValueError, match=r"^(Invalid replay command flags|--)"):
        parse_replay_command(argv)
