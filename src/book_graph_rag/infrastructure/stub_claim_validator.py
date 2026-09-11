"""Stub claim validator for deterministic tests (Slice B, D8)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from book_graph_rag.domain.evaluation_models import AtomicClaim, ClaimVerdictLabel
from book_graph_rag.ports.claim_validator_port import ClaimValidatorPort


class StubClaimValidator(ClaimValidatorPort):
    """Reads claims/verdicts from a JSON fixture keyed by ``question_id``."""

    def __init__(self, fixture_path: Path) -> None:
        self._fixture_path = fixture_path
        self._data: dict[str, dict[str, Any]] = {}
        if fixture_path.exists():
            self._data = json.loads(fixture_path.read_text(encoding="utf-8"))

    async def extract_claims(
        self,
        *,
        question_id: str,
        answer: str,
        contexts: tuple[str, ...],
        extractor_model_id: str,
    ) -> tuple[AtomicClaim, ...]:
        """Return claims from the fixture; empty if unknown."""
        entry = self._data.get(question_id, {})
        raw_claims = entry.get("extracted", [])
        return tuple(
            AtomicClaim(
                claim_id=rc["claim_id"],
                text=rc["text"],
                evidence_refs=tuple(rc.get("evidence_refs", [])),
            )
            for rc in raw_claims
        )

    async def verify_claims(
        self,
        *,
        question_id: str,
        claims: tuple[AtomicClaim, ...],
        contexts: tuple[str, ...],
        verifier_model_id: str,
    ) -> tuple[AtomicClaim, ...]:
        """Return verified claims from the fixture; empty if unknown."""
        entry = self._data.get(question_id, {})
        raw_verified = entry.get("verified", [])
        verdict_map = {
            rv["claim_id"]: (
                rv.get("verdict", "not_verifiable"),
                rv.get("verdict_rationale", ""),
                rv.get("verifier_model_id", verifier_model_id),
            )
            for rv in raw_verified
        }
        result: list[AtomicClaim] = []
        for claim in claims:
            verdict, rationale, model_id = verdict_map.get(
                claim.claim_id,
                ("not_verifiable", "", verifier_model_id),
            )
            validated_verdict: ClaimVerdictLabel | None = None
            if verdict in {"support", "contradict", "not_verifiable"}:
                validated_verdict = verdict
            result.append(
                claim.model_copy(
                    update={
                        "verdict": validated_verdict,
                        "verdict_rationale": rationale,
                        "verifier_model_id": model_id,
                    }
                )
            )
        return tuple(result)
