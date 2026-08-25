"""JSON evidence and manifest adapter."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from book_graph_rag.domain.validation_models import (
    ApprovalEvidence,
    EvidenceBundle,
    SmokeManifest,
)
from book_graph_rag.ports.evidence_port import EvidenceWriterPort, ManifestReaderPort


def _tuples(value: Any) -> Any:
    """Coerce JSON lists into the frozen tuples expected by strict contracts."""
    if isinstance(value, list):
        return tuple(_tuples(item) for item in value)
    if isinstance(value, dict):
        return {key: _tuples(item) for key, item in value.items()}
    return value


class JSONEvidenceAdapter(EvidenceWriterPort, ManifestReaderPort):
    """Persist and load validation artifacts as canonical JSON."""

    def write_bundle(self, bundle: EvidenceBundle, path: Path) -> None:
        """Serialize the complete evidence bundle as canonical JSON."""
        path.write_text(bundle.model_dump_json(indent=2), encoding="utf-8")

    def read_smoke_manifest(self, path: Path) -> SmokeManifest:
        """Load a versioned smoke manifest from JSON."""
        data = _tuples(json.loads(path.read_text(encoding="utf-8")))
        return SmokeManifest.model_validate(data)

    def read_approval(self, path: Path) -> ApprovalEvidence:
        """Load a maintainer approval record from JSON."""
        data = _tuples(json.loads(path.read_text(encoding="utf-8")))
        return ApprovalEvidence.model_validate(data)
