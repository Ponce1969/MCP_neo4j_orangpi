"""Phase-2 lexical pins (S2/S3/S4/S7/S8 of ``odd/tasks/retrieval-phase2-lexical.md``).

The eval script is the instrument the operator runs against production; these
tests pin the parts that must not drift:

* the shape variant escapes the question for Lucene **before** wrapping it —
  no escaping anywhere else in the repo, so an unescaped shape variant measures
  a syntax error rather than retrieval (S4);
* the committed baseline comes from ``data/evaluation/retrieval_baseline.json``
  and from nowhere else — the script used to hardcode its own copy (S7a);
* the chunk-id denominator is armored: the gate divides by ``len(list)`` while
  the script divides by ``len(set)``, so duplicates would silently split the
  two (S7b);
* the cut decision encodes S2 (>= +1 net reference, zero lost, control
  reproduces the baseline) and declares the run void when the control canary
  does not reproduce the baseline (S3).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_ROOT = Path(__file__).parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "eval_fulltext_recall",
    _ROOT / "scripts/eval_fulltext_recall.py",
)
assert _SPEC is not None
assert _SPEC.loader is not None
_MODULE: ModuleType = importlib.util.module_from_spec(_SPEC)
# Register before exec: the module defines a dataclass, and dataclasses resolve
# ``cls.__module__`` through sys.modules at class-creation time.
sys.modules["eval_fulltext_recall"] = _MODULE
_SPEC.loader.exec_module(_MODULE)

_escape_lucene = _MODULE._escape_lucene
_shape_query = _MODULE._shape_query
_chunk_id_list = _MODULE._chunk_id_list
_committed_precision_baseline = _MODULE._committed_precision_baseline
_cut_decision = _MODULE._cut_decision
_print_s2_cut = _MODULE._print_s2_cut
_LUCENE_SPECIALS: frozenset[str] = _MODULE._LUCENE_SPECIALS


def _unescaped_specials(body: str) -> list[str]:
    """Lucene specials that a classic parser would still read as syntax.

    Walks the phrase body the way the classic QueryParser does: a backslash
    escapes the next character, everything else is syntax-significant when it
    is a special. An empty result is the structural proof that the shaped
    query is one parseable phrase instead of a syntax error.
    """
    found: list[str] = []
    index = 0
    while index < len(body):
        char = body[index]
        if char == "\\":
            index += 2
            continue
        if char in _LUCENE_SPECIALS:
            found.append(char)
        index += 1
    return found


# ── S4: the shape transform escapes first, then wraps ────────────────────────


def test_shape_query_escapes_quote_and_boolean_operator_in_one_phrase() -> None:
    """The pin S4 demands: a question with a quote AND a boolean operator.

    Unescaped, the inner ``"`` would close the phrase and ``AND`` would become
    a Lucene operator over garbage — a syntax error measured as retrieval. The
    escaped form keeps both inert: the quote is ``\\"`` and ``AND`` sits inside
    the phrase where it is plain text.
    """
    question = '¿Qué significa "ReAct" AND por qué falla OR el patrón?'
    shaped = _shape_query(question)

    assert shaped == '"¿Qué significa \\"ReAct\\" AND por qué falla OR el patrón\\?"'
    body = shaped[1:-1]
    # One phrase: the only unescaped quotes in the whole query are the wrapper.
    assert _unescaped_specials(body) == [], "every Lucene special must be escaped"
    assert "AND" in body, "the AND operator must survive as literal text"
    assert "OR" in body, "the OR operator must survive as literal text"


def test_shape_query_escapes_every_lucene_special() -> None:
    """Every classic-parser special is neutralized inside the phrase."""
    raw = '+ - && || ! ( ) { } [ ] ^ " ~ * ? : \\ /'
    body = _shape_query(raw)[1:-1]

    assert _unescaped_specials(body) == []


def test_escape_order_backslash_before_quote() -> None:
    """A literal backslash before a quote must not leave the quote unescaped."""
    assert _shape_query('a "b\\c" d') == '"a \\"b\\\\c\\" d"'
    assert _unescaped_specials(_shape_query('a "b\\c" d')[1:-1]) == []


def test_shape_query_is_the_only_transform_control_stays_verbatim() -> None:
    """The control leg is the raw question; only the variant leg is shaped.

    ``search_chunks`` binds its ``query`` argument verbatim as a Cypher
    parameter, so "caller-side transform" means: the shape mode passes
    ``_shape_query(question)`` to the variant leg and the untouched question to
    the control leg. The identity here pins that the raw question itself never
    acquires an escape it must not carry (S3: the control must reproduce the
    committed baseline, which means byte-identical queries).
    """
    question = 'raw "question" with specials: (a AND b)'
    assert question.count('"') == 2
    # The control leg's query is the question itself — no wrapper, no escapes;
    # for the variant leg, escaping is lossless (unescaping recovers the question).
    assert _shape_query(question) != question
    assert _shape_query(question)[1:-1].replace("\\", "") == question


# ── S7b: the denominator is armored against the gate/script drift ────────────


def _chunks(*ids: str) -> list[dict[str, Any]]:
    return [{"chunk_id": chunk_id, "text": "t"} for chunk_id in ids]


def test_chunk_id_list_gate_equivalent_for_unique_ids() -> None:
    """Unique ids ⇒ ``len(list) == len(set)``: the two denominators are equal."""
    ids = _chunk_id_list(_chunks("a:1", "a:2", "b:3"), question_id="ret-001", leg="control")
    assert len(ids) == len(set(ids)) == 3
    assert ids == ["a:1", "a:2", "b:3"], "order preserved (gate shape is a list)"


def test_chunk_id_list_rejects_duplicates_naming_the_drift() -> None:
    """A duplicate id must abort the run, not silently split the denominators."""
    with pytest.raises(SystemExit, match="len\\(list\\).*len\\(set\\)"):
        _chunk_id_list(_chunks("a:1", "a:2", "a:1"), question_id="ret-002", leg="shape-variant")


# ── S7a: the baseline has exactly one source of truth ────────────────────────


def test_committed_baseline_comes_from_the_artifact() -> None:
    """The cut criterion applies to the committed artifact's number, verbatim."""
    baseline = _committed_precision_baseline(_ROOT / "data/evaluation")

    assert baseline == 0.125, "the committed artifact says 0.125, not a hardcoded 0.1250"


def test_committed_baseline_fails_loudly_when_artifact_missing(tmp_path: Path) -> None:
    """No artifact ⇒ no cut: the script aborts instead of inventing a number."""
    with pytest.raises(SystemExit):
        _committed_precision_baseline(tmp_path)


def test_hardcoded_committed_baseline_constant_is_gone() -> None:
    """The second source of truth (S7a) must not come back."""
    assert not hasattr(_MODULE, "COMMITTED_BASELINE")


# ── S2/S3: the cut decision ──────────────────────────────────────────────────


def test_cut_void_when_control_does_not_reproduce_the_baseline() -> None:
    """S3: the control row is a canary; a miss voids the run, it is not a result."""
    decision = _cut_decision(
        baseline=0.125,
        control_precision=0.1083,
        control_hits=13,
        variant_hits=16,
        lost_references=0,
    )

    assert decision.decision == "void"
    assert decision.control_reproduced is False


def test_cut_ships_on_monotonic_gain_over_the_baseline() -> None:
    """S2: >= +1 net reference, zero lost, control reproduces ⇒ ship."""
    decision = _cut_decision(
        baseline=0.125,
        control_precision=0.125,
        control_hits=15,
        variant_hits=16,
        lost_references=0,
    )

    assert decision.decision == "ship"
    assert decision.net_references == 1


def test_cut_holds_when_gain_and_loss_cancel() -> None:
    """S2 verbatim: a change that gains one and loses one is not an improvement."""
    decision = _cut_decision(
        baseline=0.125,
        control_precision=0.125,
        control_hits=15,
        variant_hits=15,
        lost_references=1,
    )

    assert decision.decision == "hold"


def test_cut_holds_when_net_gain_still_loses_a_reference() -> None:
    """+2 gained with 1 lost is net +1 but not monotonic ⇒ hold, not ship."""
    decision = _cut_decision(
        baseline=0.125,
        control_precision=0.125,
        control_hits=15,
        variant_hits=16,
        lost_references=1,
    )

    assert decision.decision == "hold"


def test_cut_holds_without_one_net_reference() -> None:
    """Ties and sub-granularity gains do not ship (the multiple-comparison hazard)."""
    decision = _cut_decision(
        baseline=0.125,
        control_precision=0.125,
        control_hits=15,
        variant_hits=15,
        lost_references=0,
    )

    assert decision.decision == "hold"
    assert decision.net_references == 0


def test_cut_print_declares_void_run_and_suppresses_the_verdict(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A void run prints the void, not a ship/hold verdict to be misread."""
    decision = _cut_decision(
        baseline=0.125,
        control_precision=0.1208,
        control_hits=14,
        variant_hits=18,
        lost_references=0,
    )

    _print_s2_cut(
        decision,
        baseline=0.125,
        control_precision=0.1208,
        variant_precision=0.15,
        top_k=10,
    )
    out = capsys.readouterr().out

    assert "DOES NOT REPRODUCE" in out
    assert "RUN VOID" in out
    assert "SHIP" not in out
    assert "HOLD" not in out


def test_cut_print_names_the_s2_rule_on_a_decided_run(capsys: pytest.CaptureFixture[str]) -> None:
    """The printed verdict states the rule it applied and the control reproduction."""
    decision = _cut_decision(
        baseline=0.125,
        control_precision=0.125,
        control_hits=15,
        variant_hits=16,
        lost_references=0,
    )

    _print_s2_cut(
        decision,
        baseline=0.125,
        control_precision=0.125,
        variant_precision=0.1333,
        top_k=10,
    )
    out = capsys.readouterr().out

    assert "reproduces" in out
    assert "SHIP" in out
    assert "0.150" in out, "the objective is printed as context"
