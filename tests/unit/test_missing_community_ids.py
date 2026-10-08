"""Unit tests for the community cost guard's counting rule.

`community_max_calls` bounds the LLM summaries a run generates, so it has to be compared against
the communities that still lack a summary — not against everything detection found. Guarding the
detected total meant no book with more than 150 communities could ever be resumed, which is
exactly the run that costs nothing.
"""

from __future__ import annotations

from book_graph_rag.infrastructure.community_clustering import (
    _community_summary_id,
    missing_community_ids,
)


def _by_level() -> dict[int, list[list[str]]]:
    return {
        0: [["a", "b", "c"]],
        1: [["a", "b"], ["c"]],
        2: [["a"], ["b"], ["c"]],
    }


def test_every_detected_community_is_missing_without_persisted_summaries() -> None:
    """A first run (or a `--fresh` one) has nothing persisted, so all of them count."""
    missing = missing_community_ids(_by_level(), set())

    assert len(missing) == 6
    assert _community_summary_id(0, ["a", "b", "c"]) in missing


def test_only_the_communities_without_a_summary_count() -> None:
    """The resume case: the guard must see the gap, not the book's whole size."""
    persisted = {
        _community_summary_id(0, ["a", "b", "c"]),
        _community_summary_id(1, ["a", "b"]),
        _community_summary_id(1, ["c"]),
        _community_summary_id(2, ["a"]),
    }

    missing = missing_community_ids(_by_level(), persisted)

    assert missing == {_community_summary_id(2, ["b"]), _community_summary_id(2, ["c"])}


def test_a_fully_summarized_book_needs_nothing() -> None:
    """The case that used to abort: 190 communities detected, none left to write."""
    communities = _by_level()
    persisted = {
        _community_summary_id(level, ids) for level, groups in communities.items() for ids in groups
    }

    assert missing_community_ids(communities, persisted) == set()
