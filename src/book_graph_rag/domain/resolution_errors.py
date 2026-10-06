"""Domain exceptions for semantic entity resolution."""

from __future__ import annotations

from collections.abc import Sequence


class ResolutionError(Exception):
    """Base class for resolution-stage errors."""


class InvalidNormalizationInput(ResolutionError):  # noqa: N818
    """Raised when normalization receives invalid input."""


class DatasetManifestMismatch(ResolutionError):  # noqa: N818
    """Raised when a dataset manifest hash does not match the pairs file."""


class LedgerChainBroken(ResolutionError):  # noqa: N818
    """Raised when the merge ledger chained hash is tampered with."""

    def __init__(
        self,
        message: str,
        *,
        seq: int | None = None,
        expected: str | None = None,
        actual: str | None = None,
    ) -> None:
        super().__init__(message)
        self.seq = seq
        self.expected = expected
        self.actual = actual


class RollbackTargetInvalid(ResolutionError):  # noqa: N818
    """Raised when a rollback target entry is missing or invalid."""


class MergeNotReversible(ResolutionError):  # noqa: N818
    """Raised when a merge can no longer be rolled back."""


class CrossNamespaceApprovalRequired(ResolutionError):  # noqa: N818
    """Raised when a cross-namespace merge lacks a matching ``MergeApproval``.

    Carries the canonical, the offending (crossing) candidates and an
    explanation of what the supplied reference got wrong, so the message alone
    tells the operator how to obtain a valid one: approve the quarantine
    record first (spec 03 §2.4, policy R6.2, design D-A2).
    """

    def __init__(
        self,
        *,
        canonical_id: str,
        crossing_ids: Sequence[str],
        detail: str,
    ) -> None:
        self.canonical_id = canonical_id
        self.crossing_ids = tuple(crossing_ids)
        self.detail = detail
        message = (
            f"Cross-namespace merge refused: canonical {canonical_id} and candidates "
            f"{list(self.crossing_ids)} span namespaces; {detail}. "
            "Provide a MergeApproval: approve the quarantine record first "
            "(quarantine enqueue → render → approve)."
        )
        super().__init__(message)
