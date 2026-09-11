"""Tests for ClaimValidatorPort (Slice B, T-B.1)."""

from __future__ import annotations

import asyncio

import pytest

from book_graph_rag.domain.evaluation_models import AtomicClaim
from book_graph_rag.ports.claim_validator_port import ClaimValidatorPort


def test_claim_validator_port_is_abstract() -> None:
    """ClaimValidatorPort cannot be instantiated directly."""
    with pytest.raises(TypeError, match="abstract"):
        ClaimValidatorPort()  # type: ignore[abstract]


def test_claim_validator_port_extract_claims_signature() -> None:
    """A fake adapter implements extract_claims with the expected signature."""

    class FakeValidator(ClaimValidatorPort):
        async def extract_claims(
            self, *, question_id: str, answer: str, contexts: tuple[str, ...],
            extractor_model_id: str,
        ) -> tuple[AtomicClaim, ...]:
            return (
                AtomicClaim(
                    claim_id="c1",
                    text="claim one",
                    evidence_refs=("ctx1",),
                ),
            )

        async def verify_claims(
            self, *, question_id: str, claims: tuple[AtomicClaim, ...],
            contexts: tuple[str, ...], verifier_model_id: str,
        ) -> tuple[AtomicClaim, ...]:
            return claims

    port = FakeValidator()
    # Just assert the async method exists and is awaitable via a coroutine.
    claims = asyncio.run(port.extract_claims(
        question_id="q1",
        answer="answer",
        contexts=("ctx",),
        extractor_model_id="model-x",
    ))
    assert len(claims) == 1
    assert claims[0].text == "claim one"


def test_claim_validator_port_verify_populates_verdict() -> None:
    """A fake adapter can return claims with verdict populated."""

    class FakeValidator(ClaimValidatorPort):
        async def extract_claims(
            self, *, question_id: str, answer: str, contexts: tuple[str, ...],
            extractor_model_id: str,
        ) -> tuple[AtomicClaim, ...]:
            return ()

        async def verify_claims(
            self, *, question_id: str, claims: tuple[AtomicClaim, ...],
            contexts: tuple[str, ...], verifier_model_id: str,
        ) -> tuple[AtomicClaim, ...]:
            return tuple(
                claim.model_copy(update={"verdict": "support"})
                for claim in claims
            )

    port = FakeValidator()
    claims = (
        AtomicClaim(claim_id="c1", text="claim one", evidence_refs=("ctx1",)),
    )
    verified = asyncio.run(port.verify_claims(
        question_id="q1",
        claims=claims,
        contexts=("ctx",),
        verifier_model_id="model-y",
    ))
    assert len(verified) == 1
    assert verified[0].verdict == "support"
