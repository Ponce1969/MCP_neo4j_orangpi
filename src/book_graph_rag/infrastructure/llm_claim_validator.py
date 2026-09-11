"""Production claim validator using an LLM via instructor (Slice B, D1)."""

from __future__ import annotations

import logging
from typing import Any, cast

import instructor
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from book_graph_rag.config import Settings, validate_llm_provider_settings
from book_graph_rag.domain.evaluation_models import AtomicClaim, ClaimVerdictLabel
from book_graph_rag.infrastructure.llm_adapter import _escape_json_string_control_chars
from book_graph_rag.ports.claim_validator_port import ClaimValidatorPort

logger = logging.getLogger(__name__)


class _ExtractedClaim(BaseModel):
    """One claim as extracted by the LLM."""

    claim_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    evidence_refs: list[str] = Field(default_factory=list)


class _ExtractedClaims(BaseModel):
    """LLM response schema for claim extraction."""

    claims: list[_ExtractedClaim] = Field(default_factory=list)


class _VerifiedClaim(BaseModel):
    """One claim with a verdict as returned by the LLM."""

    claim_id: str = Field(min_length=1)
    verdict: str = Field(min_length=1)
    verdict_rationale: str = ""


class _VerifiedClaims(BaseModel):
    """LLM response schema for claim verification."""

    claims: list[_VerifiedClaim] = Field(default_factory=list)


_EXTRACT_SYSTEM_PROMPT = (
    "You are a claim extractor for a knowledge-graph evaluation task.\n\n"
    "Given a generated answer and the contexts it claims to be grounded on, "
    "extract a list of atomic, verifiable claims. Each claim must be a single "
    "factual assertion.\n\n"
    "Output JSON with a ``claims`` array. Each claim has:\n"
    "- claim_id: a short unique id\n"
    "- text: the atomic claim text\n"
    "- evidence_refs: list of context ids that support or refute the claim\n"
    "Use only the provided contexts as evidence references."
)

_VERIFY_SYSTEM_PROMPT = (
    "You are a claim verifier for a knowledge-graph evaluation task.\n\n"
    "Given a list of atomic claims and a set of retrieved contexts, assign each "
    "claim exactly one verdict:\n"
    "- support: the contexts explicitly support the claim\n"
    "- contradict: the contexts explicitly contradict the claim\n"
    "- not_verifiable: the contexts do not contain enough information\n\n"
    "Output JSON with a ``claims`` array. Each claim has:\n"
    "- claim_id: the original claim id\n"
    "- verdict: one of support/contradict/not_verifiable\n"
    "- verdict_rationale: a one-sentence explanation"
)


def _build_query_instructor(settings: Settings) -> instructor.AsyncInstructor:
    """Build a sanitized instructor client over the query LLM."""
    api_key = (
        settings.query_llm_api_key.get_secret_value()
        if settings.query_llm_api_key is not None
        else ""
    )
    raw_client = AsyncOpenAI(
        base_url=settings.query_llm_base_url,
        api_key=api_key,
        timeout=180.0,
        max_retries=0,
    )
    raw_create = raw_client.chat.completions.create

    async def _sanitizing_create(*args: Any, **kwargs: Any) -> Any:
        completion = await raw_create(*args, **kwargs)
        for choice in completion.choices:
            message = choice.message
            if message is not None and message.content is not None:
                message.content = _escape_json_string_control_chars(message.content)
        return completion

    return instructor.AsyncInstructor(
        client=raw_client,
        create=cast(Any, instructor.patch(create=_sanitizing_create, mode=instructor.Mode.MD_JSON)),
        mode=instructor.Mode.MD_JSON,
    )


class LLMClaimValidator(ClaimValidatorPort):
    """Production claim validator backed by the configured query LLM."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        validate_llm_provider_settings(settings)
        self._client = _build_query_instructor(settings)

    async def extract_claims(
        self,
        *,
        question_id: str,
        answer: str,
        contexts: tuple[str, ...],
        extractor_model_id: str,
    ) -> tuple[AtomicClaim, ...]:
        """Use the query LLM to extract atomic claims from ``answer``."""
        context_text = "\n---\n".join(contexts)
        user_prompt = (
            f"Question ID: {question_id}\n\n"
            f"Answer:\n{answer}\n\n"
            f"Contexts:\n{context_text}\n\n"
            "Extract the atomic claims."
        )
        response = await self._client.chat.completions.create(
            model=self._settings.query_llm_model_name,
            response_model=_ExtractedClaims,
            messages=[
                {"role": "system", "content": _EXTRACT_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0,
        )
        return tuple(
            AtomicClaim(
                claim_id=c.claim_id,
                text=c.text,
                evidence_refs=tuple(c.evidence_refs),
            )
            for c in response.claims
        )

    async def verify_claims(
        self,
        *,
        question_id: str,
        claims: tuple[AtomicClaim, ...],
        contexts: tuple[str, ...],
        verifier_model_id: str,
    ) -> tuple[AtomicClaim, ...]:
        """Use the query LLM to verify each claim against ``contexts``."""
        context_text = "\n---\n".join(contexts)
        claims_text = "\n".join(
            f"- {c.claim_id}: {c.text}" for c in claims
        )
        user_prompt = (
            f"Question ID: {question_id}\n\n"
            f"Claims:\n{claims_text}\n\n"
            f"Contexts:\n{context_text}\n\n"
            "Verify each claim."
        )
        response = await self._client.chat.completions.create(
            model=self._settings.query_llm_model_name,
            response_model=_VerifiedClaims,
            messages=[
                {"role": "system", "content": _VERIFY_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0,
        )
        verdict_map = {
            v.claim_id: (v.verdict, v.verdict_rationale)
            for v in response.claims
        }
        result: list[AtomicClaim] = []
        for claim in claims:
            raw_verdict, rationale = verdict_map.get(
                claim.claim_id,
                ("not_verifiable", "no verdict returned"),
            )
            validated_verdict: ClaimVerdictLabel | None = None
            if raw_verdict in {"support", "contradict", "not_verifiable"}:
                validated_verdict = raw_verdict  # type: ignore[assignment]
            result.append(
                claim.model_copy(
                    update={
                        "verdict": validated_verdict,
                        "verdict_rationale": rationale,
                        "verifier_model_id": verifier_model_id,
                    }
                )
            )
        return tuple(result)
