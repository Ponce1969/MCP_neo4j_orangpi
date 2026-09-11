"""Tests for StubClaimValidator (Slice B, T-B.5)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from book_graph_rag.domain.evaluation_models import AtomicClaim
from book_graph_rag.infrastructure.stub_claim_validator import StubClaimValidator


@pytest.fixture
def fixture_path() -> Path:
    return Path("tests/fixtures/evaluation/claims.json")


@pytest.fixture
def validator(fixture_path: Path) -> StubClaimValidator:
    return StubClaimValidator(fixture_path)


def test_stub_claim_validator_returns_fixture_claims(
    validator: StubClaimValidator,
) -> None:
    """extract_claims returns the claims stored in the fixture keyed by question_id."""
    claims = asyncio.run(validator.extract_claims(
        question_id="q1",
        answer="MCP is a protocol for context exchange. MCP stands for Model Context Protocol.",
        contexts=("ctx1", "ctx2"),
        extractor_model_id="stub-extractor",
    ))
    assert len(claims) == 2
    assert claims[0].claim_id == "q1-c1"
    assert claims[0].text == "MCP is a protocol for context exchange."
    assert claims[0].evidence_refs == ("ctx1",)


def test_stub_claim_validator_verify_populates_verdict(
    validator: StubClaimValidator,
) -> None:
    """verify_claims returns fixture claims with verdict and rationale populated."""
    extracted = (
        AtomicClaim(
            claim_id="q1-c1",
            text="MCP is a protocol for context exchange.",
            evidence_refs=("ctx1",),
        ),
    )
    verified = asyncio.run(validator.verify_claims(
        question_id="q1",
        claims=extracted,
        contexts=("ctx1",),
        verifier_model_id="stub-verifier",
    ))
    assert len(verified) == 1
    assert verified[0].verdict == "support"
    assert verified[0].verdict_rationale == "Explicitly supported by ctx1."
    assert verified[0].verifier_model_id == "stub-verifier"


def test_stub_claim_validator_unknown_question_returns_empty(
    validator: StubClaimValidator,
) -> None:
    """An unknown question_id yields no claims and no verdicts."""
    claims = asyncio.run(validator.extract_claims(
        question_id="unknown",
        answer="...",
        contexts=(),
        extractor_model_id="stub-extractor",
    ))
    assert claims == ()

    verified = asyncio.run(validator.verify_claims(
        question_id="unknown",
        claims=(),
        contexts=(),
        verifier_model_id="stub-verifier",
    ))
    assert verified == ()


def test_stub_claim_validator_no_real_llm_imports(
    validator: StubClaimValidator,
) -> None:
    """The stub adapter does not import LLM client libraries."""
    import book_graph_rag.infrastructure.stub_claim_validator as stub_module

    source = Path(stub_module.__file__).read_text(encoding="utf-8")
    assert "openai" not in source
    assert "anthropic" not in source
    assert "instructor" not in source
