"""Deduplicate the evaluation dataset by semantic clustering via LLM.

Groups semantically similar questions, keeps the best representative per
group, and writes a smaller non-redundant dataset.  Uses the project's
existing LLM — zero extra dependencies.

Usage:
    uv run python scripts/deduplicate_dataset.py
    uv run python scripts/deduplicate_dataset.py -i dataset.jsonl -o clean.jsonl
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import click
import instructor
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from book_graph_rag.config import Settings

_DEFAULT_INPUT = "evaluation_dataset.jsonl"
_DEFAULT_OUTPUT = "evaluation_dataset_dedup.jsonl"


class _DedupPlan(BaseModel):
    """The LLM tells us which questions to keep — the rest are duplicates."""

    keep_indices: list[int] = Field(
        description="0-based indices of questions to KEEP (diverse, non-redundant set)"
    )
    reasoning: str = Field(
        description="Brief explanation of the deduplication strategy (max 200 chars)"
    )


_SYSTEM = (
    "You are a dataset curator.  Your task is to remove near-duplicate "
    "evaluation questions, keeping only a diverse, non-redundant set.\n\n"
    "Rules:\n"
    "- Two questions are duplicates if asking both would produce nearly "
    "identical answers from a GraphRAG system about agentic patterns.\n"
    "- Aim to keep 50–70 questions that collectively cover all the topics "
    "present in the input.\n"
    "- When choosing which duplicate to keep, prefer the clearest, most "
    "well-formed question.\n"
    "- Return only the indices of questions to KEEP (0-based, from the "
    "numbered list below).\n"
    "- Return your reasoning in 1-2 sentences."
)


def _load(path: str) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _format_questions(questions: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"[{i}] ({q['type']}) {q['question']}" for i, q in enumerate(questions)
    )


async def _dedup(
    questions: list[dict[str, Any]],
    client: instructor.AsyncInstructor,
    model: str,
) -> list[dict[str, Any]]:
    """Call the LLM and return only the questions at the returned indices."""
    prompt = (
        f"Here are {len(questions)} evaluation questions (indexed 0–"
        f"{len(questions) - 1}), many of which are near-duplicates:\n\n"
        f"{_format_questions(questions)}\n\n"
        "Return the indices of the questions that should be KEPT to form "
        "a diverse, non-redundant evaluation dataset of 50–70 questions."
    )
    plan = await client.chat.completions.create(
        model=model,
        response_model=_DedupPlan,
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": prompt},
        ],
    )
    # Validate indices are within bounds.
    max_idx = len(questions) - 1
    clean_indices: list[int] = []
    for idx in plan.keep_indices:
        if 0 <= idx <= max_idx and idx not in clean_indices:
            clean_indices.append(idx)
    click.echo(f"  LLM reasoning: {plan.reasoning}")
    click.echo(f"  Keeping {len(clean_indices)}/{len(questions)} questions")
    return [questions[i] for i in sorted(clean_indices)]


@click.command()
@click.option("--input", "-i", "dataset", default=_DEFAULT_INPUT,
              help="Path to the full evaluation dataset (JSONL)")
@click.option("--output", "-o", default=_DEFAULT_OUTPUT,
              help="Path for the deduplicated dataset (JSONL)")
def main(dataset: str, output: str) -> None:
    """Deduplicate the evaluation dataset via a single LLM call."""
    settings = Settings()  # type: ignore[call-arg]
    api_key: str = (
        settings.llm_api_key.get_secret_value()
        if settings.llm_api_key is not None
        else "ollama"
    )
    raw_client = AsyncOpenAI(
        base_url=settings.llm_base_url,
        api_key=api_key,
        timeout=120.0,
        max_retries=0,
    )
    client = instructor.from_openai(raw_client)

    click.echo(f"Loading: {dataset}")
    all_questions = _load(dataset)
    click.echo(f"  {len(all_questions)} questions")

    click.echo("Calling LLM to select a diverse subset…")
    deduped = asyncio.run(
        _dedup(all_questions, client, settings.llm_model_name)
    )

    out_path = Path(output)
    with open(out_path, "w", encoding="utf-8") as f:
        for q in deduped:
            f.write(json.dumps(q, ensure_ascii=False) + "\n")
    click.echo(f"Saved: {out_path}")
    global_count = sum(1 for q in deduped if q["type"] == "global")
    local_count = sum(1 for q in deduped if q["type"] == "local")
    click.echo(f"  Global: {global_count}  |  Local: {local_count}")


if __name__ == "__main__":
    main()
