"""A deterministic signal for whether an answer leans on the evidence it was given.

The project had no working relevance metric: RAGAS ``answer_relevancy`` returns a
deterministic ``0.0`` in this configuration, even when the answer is the question echoed
back. This replaces it with something auditable and free — it reads only the answer, its
citations and the contexts it was given, so it can never disagree with them and it costs no
model call.

Two numbers, both in ``[0, 1]``:

- ``cited_context_share``: the share of the provided contexts the answer cites.
- ``term_grounding``: the share of the answer's content terms that appear in the contexts.

Both are **proxies**, and their limits are real: a paraphrase sharing no vocabulary scores
low, and copying a context scores high. They are honest as a floor — an answer that leans on
nothing scores ``0`` — not as a verdict on quality.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

_CITATION = re.compile(r"CommunitySummary\(([^)\s]+)\)")
_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_MIN_TERM_LENGTH = 3

#: Glue words that carry no evidence in either language this corpus uses. Kept small and
#: explicit: a longer list would quietly decide what counts as content.
_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "from",
        "are",
        "was",
        "were",
        "los",
        "las",
        "una",
        "uno",
        "unos",
        "unas",
        "del",
        "que",
        "por",
        "con",
        "para",
        "como",
        "mas",
        "pero",
        "sus",
        "este",
        "esta",
        "esto",
        "son",
        "ser",
        "han",
    }
)


class GroundingContext(BaseModel):
    """One context an answer was given: the id it is cited by and its text."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    text: str = ""


class AnswerGrounding(BaseModel):
    """How far one answer leans on the contexts it received."""

    model_config = ConfigDict(frozen=True)

    cited_context_share: float = Field(ge=0.0, le=1.0)
    term_grounding: float = Field(ge=0.0, le=1.0)
    cited_contexts: int = Field(ge=0)
    contexts_provided: int = Field(ge=0)


def cited_ids(answer: str) -> list[str]:
    """The ids the answer cites, in order, without duplicates."""
    seen: list[str] = []
    for match in _CITATION.finditer(answer):
        if match.group(1) not in seen:
            seen.append(match.group(1))
    return seen


def content_terms(text: str) -> set[str]:
    """The content words of a text: folded, without glue, at least three characters."""
    return {
        word
        for word in _WORD.findall(_fold(text))
        if len(word) >= _MIN_TERM_LENGTH and word not in _STOPWORDS
    }


def grounding(answer: str, contexts: Sequence[GroundingContext]) -> AnswerGrounding:
    """Compute both numbers for one answer against the contexts it was given."""
    provided = [context.id for context in contexts]
    if not provided:
        return AnswerGrounding(
            cited_context_share=0.0,
            term_grounding=0.0,
            cited_contexts=0,
            contexts_provided=0,
        )

    cited = [context_id for context_id in cited_ids(answer) if context_id in provided]
    terms = content_terms(answer)
    if not terms:
        term_grounding = 0.0
    else:
        context_terms = content_terms(" ".join(context.text for context in contexts))
        term_grounding = len(terms & context_terms) / len(terms)

    return AnswerGrounding(
        cited_context_share=len(cited) / len(provided),
        term_grounding=term_grounding,
        cited_contexts=len(cited),
        contexts_provided=len(provided),
    )


def _fold(text: str) -> str:
    """Lowercase and strip accents, so ``PRECISION`` matches ``Precisión``."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(char for char in decomposed if not unicodedata.combining(char))
