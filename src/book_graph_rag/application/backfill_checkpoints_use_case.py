"""Backfill :Checkpoint rows for legacy graphs that pre-date Phase 2.

The use case supports a dry-run mode that reports which chunks would be marked
``PROCESSED`` and an approval-gated apply mode that actually writes the
checkpoints plus an audit evidence bundle.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from book_graph_rag.domain.checkpoint_models import VersionDimensions
from book_graph_rag.ports.checkpoint_port import CheckpointPort
from book_graph_rag.ports.graph_db_port import GraphDatabasePort


class BackfillReport(BaseModel):
    """Structured result of a backfill dry-run or apply execution."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str
    dry_run: bool
    candidate_chunk_indices: list[int] = Field(default_factory=list)
    processed_count: int = 0
    current_versions: VersionDimensions
    approval_path: Path | None = None
    evidence_bundle_path: Path | None = None


class BackfillCheckpointsUseCase:
    """Dry-run and approval-gated apply for legacy checkpoint backfill.

    The use case only creates ``:Checkpoint`` rows; it never mutates existing
    ``:Chunk``, ``:Entity`` or ``:MENTIONS`` data. The version dimensions used
    are the caller-supplied current dimensions, because the legacy run did not
    record per-chunk versions.
    """

    def __init__(
        self,
        checkpoint_port: CheckpointPort,
        graph_db_port: GraphDatabasePort,
        versions: VersionDimensions,
        run_id: str,
        evidence_dir: Path = Path("evidence-bundles"),
    ) -> None:
        self._checkpoint_port = checkpoint_port
        self._graph_db_port = graph_db_port
        self._versions = versions
        self._run_id = run_id
        self._evidence_dir = evidence_dir

    async def execute(
        self,
        source_id: str,
        *,
        apply: bool = False,
        approval_path: Path | None = None,
    ) -> BackfillReport:
        """Run the backfill command.

        ``apply=False`` returns a dry-run report and writes nothing.
        ``apply=True`` requires ``approval_path`` to point to a file whose
        contents include the word "approve"; it then writes ``PROCESSED``
        checkpoints and an evidence bundle.
        """
        candidates = await self._graph_db_port.fetch_backfill_candidates(source_id)

        if not apply:
            return BackfillReport(
                source_id=source_id,
                dry_run=True,
                candidate_chunk_indices=candidates,
                processed_count=0,
                current_versions=self._versions,
            )

        self._validate_approval(approval_path)
        processed = await self._checkpoint_port.backfill_processed(
            source_id, candidates, self._versions
        )
        evidence_path = self._write_evidence_bundle(source_id, candidates)

        return BackfillReport(
            source_id=source_id,
            dry_run=False,
            candidate_chunk_indices=candidates,
            processed_count=len(processed),
            current_versions=self._versions,
            approval_path=approval_path,
            evidence_bundle_path=evidence_path,
        )

    async def close(self) -> None:
        """Close any closable ports passed to the use case."""
        for port in (self._checkpoint_port, self._graph_db_port):
            if hasattr(port, "close"):
                await port.close()

    def _validate_approval(self, approval_path: Path | None) -> None:
        """Ensure a human-signed approval artifact is present before applying."""
        if approval_path is None:
            raise ValueError(
                "Backfill apply requires an approval file (pass approval_path)"
            )
        if not approval_path.exists():
            raise ValueError(
                f"Approval file not found: {approval_path}"
            )
        content = approval_path.read_text(encoding="utf-8").strip().lower()
        if "approve" not in content:
            raise ValueError(
                f"Approval file {approval_path} must contain the word 'approve'"
            )

    def _write_evidence_bundle(
        self, source_id: str, candidate_chunk_indices: list[int]
    ) -> Path:
        """Persist an auditable evidence bundle for the backfill operation."""
        self._evidence_dir.mkdir(parents=True, exist_ok=True)
        evidence_path = self._evidence_dir / f"backfill-{self._run_id}.json"
        bundle = {
            "run_id": self._run_id,
            "source_id": source_id,
            "backfilled_at": datetime.now(UTC).isoformat(),
            "current_versions": self._versions.model_dump(mode="json"),
            "candidate_chunk_indices": candidate_chunk_indices,
            "processed_count": len(candidate_chunk_indices),
            "note": (
                "Legacy checkpoints backfilled under current version dimensions. "
                "Original per-chunk version dimensions were not recorded by the "
                "pre-Phase-2 indexing run."
            ),
        }
        evidence_path.write_text(
            json.dumps(bundle, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return evidence_path
