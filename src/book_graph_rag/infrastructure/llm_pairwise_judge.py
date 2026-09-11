"""Production pairwise judge using an LLM via instructor (Slice B, D2)."""

from __future__ import annotations

from typing import Any, cast

import instructor
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from book_graph_rag.config import Settings, validate_llm_provider_settings
from book_graph_rag.domain.evaluation_models import PairwiseJudgment
from book_graph_rag.infrastructure.llm_adapter import _escape_json_string_control_chars
from book_graph_rag.ports.pairwise_judge_port import PairwiseJudgePort


class _PairwiseVerdict(BaseModel):
    """LLM response schema for pairwise comparison."""

    verdict: str = Field(min_length=1)
    rationale: str = ""


_PAIRWISE_SYSTEM_PROMPT = (
    "You are an impartial judge comparing two answers to the same question.\n\n"
    "Given the question, the graph-generated answer, the vector-baseline answer, "
    "and the retrieved contexts, decide which answer is better overall in terms "
    "of accuracy, faithfulness to the contexts, and completeness.\n\n"
    "Return exactly one verdict: ``graph_wins``, ``tie``, or ``baseline_wins``. "
    "Provide a concise rationale."
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


class LLMPairwiseJudge(PairwiseJudgePort):
    """Production pairwise judge backed by the configured query LLM."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        validate_llm_provider_settings(settings)
        self._client = _build_query_instructor(settings)

    async def compare(
        self,
        *,
        question_id: str,
        question: str,
        graph_answer: str,
        baseline_answer: str,
        contexts: tuple[str, ...],
        judge_model_id: str,
    ) -> PairwiseJudgment:
        """Ask the query LLM to compare the two answers."""
        context_text = "\n---\n".join(contexts)
        user_prompt = (
            f"Question ID: {question_id}\n"
            f"Question: {question}\n\n"
            f"Graph answer:\n{graph_answer}\n\n"
            f"Baseline answer:\n{baseline_answer}\n\n"
            f"Retrieved contexts:\n{context_text}\n\n"
            "Which answer is better?"
        )
        response = await self._client.chat.completions.create(
            model=self._settings.query_llm_model_name,
            response_model=_PairwiseVerdict,
            messages=[
                {"role": "system", "content": _PAIRWISE_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0,
        )
        verdict = response.verdict if response.verdict in {
            "graph_wins", "tie", "baseline_wins"
        } else "tie"
        return PairwiseJudgment(
            question_id=question_id,
            verdict=verdict,  # type: ignore[arg-type]
            rationale=response.rationale,
            judge_model_id=judge_model_id,
        )
