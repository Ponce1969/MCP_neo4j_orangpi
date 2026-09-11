"""Port for loading committed evaluation datasets by id."""

from __future__ import annotations

import abc

from book_graph_rag.domain.evaluation_models import EvaluationDataset


class EvaluationDatasetPort(abc.ABC):
    """Load a committed evaluation dataset by id, with manifest validation (R1.3)."""

    @abc.abstractmethod
    def load(self, dataset_id: str) -> EvaluationDataset:
        """Return the dataset after validating the manifest sha256 + schema_version.

        Raises:
            DatasetManifestMismatch: dataset file sha256 does not match manifest.
            UnknownDatasetError: dataset_id absent from MANIFEST.json.
            DatasetLoadError: manifest unreadable / malformed.
        """

    @abc.abstractmethod
    def list_datasets(self) -> tuple[str, ...]:
        """Return every dataset id declared in the manifest."""
