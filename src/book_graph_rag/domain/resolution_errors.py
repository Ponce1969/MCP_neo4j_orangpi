"""Domain exceptions for semantic entity resolution."""

from __future__ import annotations


class ResolutionError(Exception):
    """Base class for resolution-stage errors."""


class InvalidNormalizationInput(ResolutionError):  # noqa: N818
    """Raised when normalization receives invalid input."""


class DatasetManifestMismatch(ResolutionError):  # noqa: N818
    """Raised when a dataset manifest hash does not match the pairs file."""


class LedgerChainBroken(ResolutionError):  # noqa: N818
    """Raised when the merge ledger chained hash is tampered with."""


class RollbackTargetInvalid(ResolutionError):  # noqa: N818
    """Raised when a rollback target entry is missing or invalid."""


class MergeNotReversible(ResolutionError):  # noqa: N818
    """Raised when a merge can no longer be rolled back."""
