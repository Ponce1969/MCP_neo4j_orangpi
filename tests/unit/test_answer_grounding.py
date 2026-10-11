"""The deterministic evidence-usage signal: it must be auditable and free.

`answer_relevancy` is broken in this configuration (a deterministic 0.0 even when the
answer is the question echoed back), so this replaces it with numbers that read only the
answer, its citations and the contexts it was given. The tests below pin the properties
that make it trustworthy: it never credits an id that was not provided, it ignores
punctuation and stopwords, and it says 0.0 when there is nothing to lean on.

The fixtures use bare ids, because that is what a context carries in the real payload
(`{"id": "007873583f96e774", ...}`); the answer cites the wrapped form
(`[Data: CommunitySummary(007873583f96e774)]`).
"""

from book_graph_rag.domain.answer_grounding import GroundingContext, grounding


def _context(context_id: str, text: str) -> GroundingContext:
    return GroundingContext(id=context_id, text=text)


def _cite(context_id: str) -> str:
    return f"[Data: CommunitySummary({context_id})]"


class TestCitedContextShare:
    """How many of the provided contexts the answer actually cites."""

    def test_an_answer_citing_one_of_four_contexts_scores_a_quarter(self) -> None:
        contexts = [_context(f"hash-{i}", "texto irrelevante") for i in range(4)]
        answer = f"Solo uso una {_cite('hash-2')}."

        assert grounding(answer, contexts).cited_context_share == 0.25

    def test_citing_everything_scores_one(self) -> None:
        contexts = [_context("hash-1", "a"), _context("hash-2", "b")]
        answer = f"Dos fuentes {_cite('hash-1')} y {_cite('hash-2')}."

        assert grounding(answer, contexts).cited_context_share == 1.0

    def test_an_id_that_was_never_provided_earns_no_credit(self) -> None:
        """The signal can never disagree with the citations: an invented id is not a hit."""
        contexts = [_context("hash-1", "a")]

        result = grounding(f"Cito {_cite('hash-99')}.", contexts)

        assert result.cited_context_share == 0.0

    def test_repeating_a_citation_does_not_inflate_the_share(self) -> None:
        contexts = [_context("hash-1", "a"), _context("hash-2", "b")]
        answer = f"{_cite('hash-1')} y otra vez {_cite('hash-1')}."

        assert grounding(answer, contexts).cited_context_share == 0.5

    def test_no_contexts_means_no_share_rather_than_a_crash(self) -> None:
        assert grounding("cualquier cosa", []).cited_context_share == 0.0


class TestTermGrounding:
    """What share of the answer's content words appear in the contexts it was given."""

    def test_an_answer_written_from_the_context_scores_one(self) -> None:
        context = "Percentiles de latencia y presupuestos por etapa."
        answer = "Percentiles de latencia, con presupuestos por etapa."

        result = grounding(answer, [_context("hash-1", context)])

        assert result.term_grounding == 1.0

    def test_an_answer_about_something_else_scores_zero(self) -> None:
        contexts = [_context("hash-1", "Percentiles de latencia y presupuestos.")]
        answer = "Habla de recetas de cocina italiana."

        assert grounding(answer, contexts).term_grounding == 0.0

    def test_partial_overlap_scores_between(self) -> None:
        contexts = [_context("hash-1", "Percentiles de latencia.")]

        result = grounding("Percentiles de cocina.", contexts)

        assert 0.0 < result.term_grounding < 1.0

    def test_stopwords_and_punctuation_carry_no_weight(self) -> None:
        contexts = [_context("hash-1", "latencia")]

        # Only "latencia" is a content term; the rest is glue.
        result = grounding("la, de, el y latencia", contexts)

        assert result.term_grounding == 1.0

    def test_an_answer_with_no_content_terms_scores_zero(self) -> None:
        contexts = [_context("hash-1", "latencia")]

        assert grounding("y, de, la", contexts).term_grounding == 0.0

    def test_case_and_accents_do_not_change_the_verdict(self) -> None:
        contexts = [_context("hash-1", "Precisión por paso.")]

        assert grounding("PRECISION por paso", contexts).term_grounding == 1.0
