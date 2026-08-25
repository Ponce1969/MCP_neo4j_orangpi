"""Evidence and manifest persistence ports for pre-reindex validation."""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from book_graph_rag.domain.validation_models import (
    ApprovalEvidence,
    EvidenceBundle,
    SmokeManifest,
)


class EvidenceWriterPort(ABC):
    """Writes the immutable evidence bundle; never mutates the graph."""

    @abstractmethod
    def write_bundle(self, bundle: EvidenceBundle, path: Path) -> None:
        """Serialize the complete evidence bundle to the given path."""


class ManifestReaderPort(ABC):
    """Reads versioned smoke manifests and approval records."""

    @abstractmethod
    def read_smoke_manifest(self, path: Path) -> SmokeManifest:
        """Load a versioned smoke manifest."""

    @abstractmethod
    def read_approval(self, path: Path) -> ApprovalEvidence:
        """Load an optional maintainer approval record."""
