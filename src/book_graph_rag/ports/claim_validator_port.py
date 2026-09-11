"""Port for extracting and verifying atomic claims from generated answers."""

from __future__ import annotations

import abc

from book_graph_rag.domain.evaluation_models import AtomicClaim


class ClaimValidatorPort(abc.ABC):
    """Extract atomic claims from an answer; verify each claim against evidence."""

    @abc.abstractmethod
    async def extract_claims(
        self,
        *,
        question_id: str,
        answer: str,
        contexts: tuple[str, ...],
        extractor_model_id: str,
    ) -> tuple[AtomicClaim, ...]:
        """Extract atomic claims from ``answer`` grounded in ``contexts``."""

    @abc.abstractmethod
    async def verify_claims(
        self,
        *,
        question_id: str,
        claims: tuple[AtomicClaim, ...],
        contexts: tuple[str, ...],
        verifier_model_id: str,
    ) -> tuple[AtomicClaim, ...]:
        """Return ``claims`` with ``verdict`` and ``verdict_rationale`` populated."""
