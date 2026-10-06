"""Anti-drift and stratification tests for the T8 retro-audit script.

Two protection layers over ``scripts-ops/audit_applied_cross_namespace.py``:

1. **Static anti-drift** (mirrors ``tests/unit/test_render_cross_namespace_sample.py``)
   — the script must import the shared model functions (evidence readings,
   risk rule, overlaps, truncation, namespace helper, ledger reader/model),
   must keep no threshold literal (``0.50``/``0.10``) anywhere in its source
   (AST float constants plus a documented regex over the raw text, which also
   catches literals hidden inside docstrings), must contain no write-Cypher
   keyword anywhere in its source, and every ``_QUERY*`` string constant must
   be ``MATCH``-only. Re-divergence fails statically, before any run.
2. **Pure stratification** — the matrix counts, the three lists' membership
   and ordering, and the ``lexically_silent`` boundary rule are exercised
   over synthetic pairs with no ledger and no graph.
3. **T9a keep discount** — a pair the human decided to KEEP leaves the
   suspicious population (``decided_keep`` per ``(seq, candidate_id)``), the
   keep keys come from the shared domain read model (imported and called,
   never re-implemented), a corrupt decisions registry fails loudly before
   any graph query, and the console report exposes the decided-keep count.

The graph/ledger integration itself is exercised only by the maintainer's
production run (this machine has no local copy of the 958-entry ledger).
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest

from book_graph_rag.domain.merge_ledger_models import MergeBand, MergeLedgerEntry
from book_graph_rag.domain.models import Entity
from book_graph_rag.domain.quarantine_review_models import (
    LanguageNote,
    LanguageRelation,
    RiskLevel,
    label_risk,
)
from book_graph_rag.domain.s4_band_assignment import BandThresholds

_ROOT = Path(__file__).parents[2]
_SCRIPT_PATH = _ROOT / "scripts-ops" / "audit_applied_cross_namespace.py"
_SOURCE = _SCRIPT_PATH.read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)

#: Write-Cypher keywords must never appear in the script source (the script is
#: read-only by construction). Case-sensitive on purpose: Cypher keywords are
#: uppercase; Spanish prose uses lowercase words that only look related.
_WRITE_KEYWORDS = re.compile(r"\b(CREATE|DELETE|MERGE|DETACH|SET|REMOVE|DROP)\b")

#: Threshold literals owned by ``BandThresholds()``, never by the script.
_THRESHOLD_PATTERN = re.compile(r"0\.50|0\.10")

#: module -> names the script must import from it (the shared domain model).
_REQUIRED_IMPORTS: dict[str, frozenset[str]] = {
    "book_graph_rag.domain.quarantine_review_models": frozenset(
        {
            "PRIMARY_SIGNAL",
            "STRUCTURAL_SIGNALS",
            "EvidenceReading",
            "LabelRisk",
            "LanguageNote",
            "LanguageRelation",
            "RiskLevel",
            "format_risk_marker",
            "is_generic_label",
            "label_risk",
            "language_note",
            "mention_snippet",
            "reading_for",
        }
    ),
    "book_graph_rag.domain.merge_ledger_models": frozenset({"MergeLedgerEntry"}),
    # T9a: the keep keys are read through the shared domain read model.
    "book_graph_rag.domain.cross_namespace_decision_models": frozenset(
        {"InvalidDecisionRecord", "keep_pair_keys"}
    ),
    "book_graph_rag.infrastructure.jsonl_cross_namespace_decisions": frozenset(
        {"JSONLCrossNamespaceDecisions"}
    ),
    "book_graph_rag.domain.s0_normalization": frozenset({"namespace_from_id"}),
    "book_graph_rag.domain.s3_context_scoring": frozenset(
        {"description_overlap", "mentions_jaccard", "related_jaccard"}
    ),
    "book_graph_rag.domain.s4_band_assignment": frozenset({"BandThresholds"}),
    "book_graph_rag.infrastructure.jsonl_merge_ledger": frozenset({"JSONLMergeLedger"}),
}

_HIGH = label_risk(single_word=True, namespace_count=3)  # alta (genérica · 3 nss)
_MEDIUM = label_risk(single_word=True, namespace_count=2)  # media (genérica)
_NONE = label_risk(single_word=False, namespace_count=2)  # sin riesgo

#: Language notes for the four T8b strata (built with the shared model).
_SAME_ES = LanguageNote(relation=LanguageRelation.SAME_LANGUAGE, anchor="es", candidate="es")
_CROSS_EN_ES = LanguageNote(
    relation=LanguageRelation.DIFFERENT_LANGUAGES, anchor="en", candidate="es"
)
_UNKNOWN_LANG = LanguageNote()  # relation=unknown, both sides unknown


def _load_script() -> ModuleType:
    """Import the ops script as a module (no ledger, no graph, no CLI args)."""
    spec = importlib.util.spec_from_file_location("audit_applied_cross_namespace", _SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec so module-level dataclasses resolve (T6 pattern).
    sys.modules["audit_applied_cross_namespace"] = module
    spec.loader.exec_module(module)
    return module


def _imported_names(tree: ast.AST) -> dict[str, set[str]]:
    """``module -> names`` imported from it anywhere in the script."""
    names: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            names.setdefault(node.module, set()).update(alias.name for alias in node.names)
    return names


def _called_names(tree: ast.AST) -> set[str]:
    """Simple (non-attribute) function names called anywhere in the script."""
    return {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


def _query_constants(tree: ast.Module) -> list[tuple[str, str]]:
    """Module-level ``_QUERY*`` string constants as ``(name, value)``."""
    found: list[tuple[str, str]] = []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        targets = [target.id for target in node.targets if isinstance(target, ast.Name)]
        if not any(name.startswith("_QUERY") for name in targets):
            continue
        value = node.value
        assert isinstance(value, ast.Constant), f"_QUERY* constant {targets} must be a literal"
        assert isinstance(value.value, str), f"_QUERY* constant {targets} must be a string"
        found.append((targets[0], value.value))
    return found


# ── Static anti-drift ────────────────────────────────────────────────────────


def test_script_imports_the_shared_model_functions() -> None:
    """Evidence, risk, truncation and ledger reading all come from the domain."""
    imported = _imported_names(_TREE)
    for module, required in _REQUIRED_IMPORTS.items():
        missing = required - imported.get(module, set())
        assert not missing, f"{module}: missing shared imports {sorted(missing)}"


def test_script_actually_calls_the_shared_rules() -> None:
    """Importing is not enough: the shared rules must be called, not shadowed."""
    called = _called_names(_TREE)
    for function_name in ("reading_for", "label_risk", "description_overlap", "language_note"):
        assert function_name in called, f"script never calls the shared {function_name}"


def test_script_carries_no_threshold_literals_of_its_own() -> None:
    """No ``0.50``/``0.10`` lives in the script — BandThresholds() owns them.

    Mechanism: (a) AST scan for float constants equal to either threshold
    (catches local ``HIGH = 0.50``-style definitions and inline comparisons);
    (b) documented regex over the raw source, which additionally catches
    literals written inside the module docstring or any string.
    """
    float_constants = {
        node.value
        for node in ast.walk(_TREE)
        if isinstance(node, ast.Constant) and isinstance(node.value, float)
    }
    assert float_constants.isdisjoint({0.5, 0.1}), float_constants
    assert _THRESHOLD_PATTERN.search(_SOURCE) is None, "threshold literal in script source"


def test_script_contains_no_write_cypher_keyword() -> None:
    """The script source never names a write operation: read-only by construction."""
    match = _WRITE_KEYWORDS.search(_SOURCE)
    offender = match.group(0) if match else ""
    assert match is None, f"write-Cypher keyword in script source: {offender}"


def test_script_queries_are_match_only() -> None:
    """Every ``_QUERY*`` constant starts with MATCH and carries no write keyword."""
    queries = _query_constants(_TREE)
    assert queries, "script defines no _QUERY* constants"
    for name, query in queries:
        stripped = query.strip()
        assert stripped.startswith("MATCH"), f"{name} does not start with MATCH"
        keyword = _WRITE_KEYWORDS.search(query)
        assert keyword is None, f"{name} contains write keyword {keyword.group(0)}"


# ── Selection over a synthetic ledger (compensating entries, T8 batch-1) ─────


def _crossing_entry(seq: int, *, rollback_of: int | None = None) -> MergeLedgerEntry:
    """A cross-namespace ledger entry (optionally a compensating rollback)."""
    return MergeLedgerEntry(
        seq=seq,
        candidate_ids=["ns-b:src:tool-concept"],
        canonical_id="ns-a:src:tool-concept",
        band=MergeBand.EXACT,
        evidence=[],
        aliases_folded=[],
        edge_inverse_map=[],
        approver="auto:rollback" if rollback_of is not None else "auto:bypass",
        applied_at=datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC),
        rollback_of=rollback_of,
    )


def _same_namespace_entry(seq: int, *, rollback_of: int | None = None) -> MergeLedgerEntry:
    """A same-namespace ledger entry: never selected, compensating or not."""
    return MergeLedgerEntry(
        seq=seq,
        candidate_ids=["ns-a:src:tool-concept"],
        canonical_id="ns-a:src:tool-concept",
        band=MergeBand.EXACT,
        evidence=[],
        aliases_folded=[],
        edge_inverse_map=[],
        approver="auto:rollback" if rollback_of is not None else "auto:bypass",
        applied_at=datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC),
        rollback_of=rollback_of,
    )


def test_selection_excludes_the_compensator_and_keeps_the_rolled_back_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The compensating entry leaves the selection; the original stays flagged.

    The rollback appends an entry that mirrors the original's ids, so it also
    looks cross-namespace. It must be excluded from the selection (and counted
    separately), while the ORIGINAL it compensates stays in the selection with
    ``rolled_back=True`` so the report still shows what was reverted, and the
    guard's invariant (the ORIGINAL cross-namespace population) must hold.
    """
    module = _load_script()
    original = _crossing_entry(1)
    compensating = _crossing_entry(2, rollback_of=1)
    # Same-namespace original + its compensator: neither crosses, so neither
    # may enter the selection nor the compensating count.
    same_original = _same_namespace_entry(3)
    same_compensating = _same_namespace_entry(4, rollback_of=3)
    entries = [original, compensating, same_original, same_compensating]

    selected = module._select_crossing_entries(entries)
    assert [entry.seq for entry in selected] == [1], (
        "the compensating entry (and the same-namespace pair) must be excluded"
    )

    compensating_entries = module._compensating_crossing_entries(entries)
    assert [entry.seq for entry in compensating_entries] == [2]

    compensated = module._compensated_candidates(compensating_entries)
    applied = module._applied_entries(selected, compensated)
    assert applied == [], "the original was compensated: nothing still applied"

    row = module._history_row(selected[0], compensated[selected[0].seq])
    assert row["rolled_back"] is True, "the flag must survive on the original"

    # Guard: the ORIGINAL population is the invariant (here 1 original).
    monkeypatch.setattr(module, "EXPECTED_CROSS_NAMESPACE_ENTRIES", 1)
    module._assert_expected_count(len(selected), len(compensating_entries))

    # An uncompensated original: same invariant, nothing compensated.
    module._assert_expected_count(1, 0)


def test_guard_fails_loudly_when_the_numbers_do_not_add_up(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A ledger whose original population misses the ground truth exits 2."""
    module = _load_script()
    monkeypatch.setattr(module, "EXPECTED_CROSS_NAMESPACE_ENTRIES", 302)

    with pytest.raises(SystemExit) as excinfo:
        module._assert_expected_count(301, 17)

    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "301" in err, "the guard must print the original population it measured"
    assert "302" in err, "the guard must print the expected ground truth"
    assert "17" in err, "the guard must report the compensating count it saw"


# ── T8f.2: partial compensations are candidate-aware ────────────────────────


def _two_candidate_crossing_entry(seq: int) -> MergeLedgerEntry:
    """One entry that crossed with TWO candidates (a multi-candidate merge)."""
    return MergeLedgerEntry(
        seq=seq,
        candidate_ids=["ns-b:src:node-component", "ns-c:src:node-component"],
        canonical_id="ns-a:src:node-component",
        band=MergeBand.EXACT,
        evidence=[],
        aliases_folded=[],
        edge_inverse_map=[],
        approver="auto:bypass",
        applied_at=datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC),
    )


def _partial_compensator(seq: int, rollback_of: int, candidate_ids: list[str]) -> MergeLedgerEntry:
    """The compensating entry ``ledger rollback --candidate`` appends."""
    return MergeLedgerEntry(
        seq=seq,
        candidate_ids=list(candidate_ids),
        canonical_id="ns-a:src:node-component",
        band=MergeBand.EXACT,
        evidence=[],
        aliases_folded=[],
        edge_inverse_map=[],
        approver="auto:rollback",
        applied_at=datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC),
        rollback_of=rollback_of,
    )


def test_compensated_candidates_unions_every_compensator_of_a_seq() -> None:
    """Two partial compensations of one entry aggregate into one candidate set."""
    module = _load_script()
    compensating = [
        _partial_compensator(10, rollback_of=1, candidate_ids=["ns-b:src:node-component"]),
        _partial_compensator(11, rollback_of=1, candidate_ids=["ns-c:src:node-component"]),
    ]
    assert module._compensated_candidates(compensating) == {
        1: {"ns-b:src:node-component", "ns-c:src:node-component"}
    }


def test_partially_compensated_entry_stays_applied_until_every_pair_is_gone() -> None:
    """A partial rollback must not hide the pairs it did NOT reverse."""
    module = _load_script()
    entry = _two_candidate_crossing_entry(1)

    partial = {1: {"ns-b:src:node-component"}}
    assert module._applied_entries([entry], partial) == [entry], (
        "one remaining crossing candidate keeps the entry in the applied population"
    )

    full = {1: {"ns-b:src:node-component", "ns-c:src:node-component"}}
    assert module._applied_entries([entry], full) == [], (
        "every crossing candidate compensated: the entry leaves the applied population"
    )


def test_build_pairs_flags_only_the_compensated_candidate() -> None:
    """``rolled_back`` is per candidate, not per entry."""
    module = _load_script()
    entry = _two_candidate_crossing_entry(1)
    pairs, _, _ = module._build_pairs(
        [entry],
        entities={},
        merged_into={},
        mentions={},
        neighbors={},
        label_namespaces={},
        compensated_candidates={1: {"ns-b:src:node-component"}},
        keep_pair_keys=set(),
        namespace_filter=None,
    )
    flags = {(pair.candidate_id, pair.rolled_back) for pair in pairs}
    assert flags == {
        ("ns-b:src:node-component", True),
        ("ns-c:src:node-component", False),
    }, "only the compensated candidate is rolled back; its sibling stays stratified"


def test_history_row_reports_partial_and_full_compensation() -> None:
    """The history row distinguishes a partial rollback from a full one."""
    module = _load_script()
    entry = _two_candidate_crossing_entry(1)

    partial = module._history_row(entry, {"ns-b:src:node-component"})
    assert partial["rolled_back"] is False, "a partial rollback is not a full reversal"
    assert partial["rolled_back_candidates"] == ["ns-b:src:node-component"]

    full = module._history_row(entry, {"ns-b:src:node-component", "ns-c:src:node-component"})
    assert full["rolled_back"] is True
    assert sorted(full["rolled_back_candidates"]) == [
        "ns-b:src:node-component",
        "ns-c:src:node-component",
    ]


def test_compensations_must_reference_a_known_original_candidate(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A compensation outside the ground truth aborts before the graph."""
    module = _load_script()
    original = _two_candidate_crossing_entry(1)

    # Two complementary partials of the same entry are legal (A, then B).
    module._assert_compensations_reference_originals(
        [original],
        [
            _partial_compensator(10, 1, ["ns-b:src:node-component"]),
            _partial_compensator(11, 1, ["ns-c:src:node-component"]),
        ],
    )

    with pytest.raises(SystemExit) as unknown_target:
        module._assert_compensations_reference_originals(
            [original], [_partial_compensator(12, 99, ["ns-b:src:node-component"])]
        )
    assert unknown_target.value.code == 2
    assert "99" in capsys.readouterr().err

    with pytest.raises(SystemExit) as unknown_candidate:
        module._assert_compensations_reference_originals(
            [original], [_partial_compensator(13, 1, ["ns-z:src:ghost"])]
        )
    assert unknown_candidate.value.code == 2
    assert "ns-z:src:ghost" in capsys.readouterr().err

    # The population guard is count-only: double accounting must abort here.
    with pytest.raises(SystemExit) as duplicated:
        module._assert_compensations_reference_originals(
            [original],
            [
                _partial_compensator(14, 1, ["ns-b:src:node-component"]),
                _partial_compensator(15, 1, ["ns-b:src:node-component"]),
            ],
        )
    assert duplicated.value.code == 2
    assert "ya compensados" in capsys.readouterr().err

    with pytest.raises(SystemExit) as mismatched:
        module._assert_compensations_reference_originals(
            [original],
            [
                _partial_compensator(16, 1, ["ns-b:src:node-component"]).model_copy(
                    update={"canonical_id": "ns-x:src:other"}
                )
            ],
        )
    assert mismatched.value.code == 2
    assert "canonical_id" in capsys.readouterr().err


def test_stratifiable_pairs_keeps_the_siblings_of_a_partial_rollback() -> None:
    """Only the reverted pair leaves the stratum; missing entities always do."""
    module = _load_script()

    def _synthetic(
        seq: int, label: str, *, rolled_back: bool = False, missing: bool = False
    ) -> object:
        return module.PairAudit(
            seq=seq,
            canonical_id=f"ns-a:src:{label}-concept",
            candidate_id=f"ns-b:src:{label}-concept",
            canonical_namespace="ns-a:src",
            candidate_namespace="ns-b:src",
            label=label,
            description_overlap=0.1,
            risk=RiskLevel.NONE,
            lexically_silent=True,
            rolled_back=rolled_back,
            missing_entities=missing,
        )

    kept = _synthetic(1, "alpha")
    rolled = _synthetic(2, "beta", rolled_back=True)
    missing = _synthetic(3, "gamma", missing=True)

    result = module._stratifiable_pairs([kept, rolled, missing])

    assert [pair.seq for pair in result] == [1], (
        "the sibling of a partial rollback stays stratified; the reverted pair "
        "and the pairs with missing entities do not"
    )


# ── T9a: decided-keep pairs leave the suspicious population ──────────────────


def test_decided_keep_pair_is_flagged_and_the_undecided_sibling_stays() -> None:
    """A human-kept pair is no longer a suspicious pair; its sibling is."""
    module = _load_script()
    entry = _two_candidate_crossing_entry(1)
    entities = {
        entity_id: Entity(
            id=entity_id, name="Node", type="component", description="A node component."
        )
        for entity_id in (
            "ns-a:src:node-component",
            "ns-b:src:node-component",
            "ns-c:src:node-component",
        )
    }
    pairs, _, _ = module._build_pairs(
        [entry],
        entities=entities,
        merged_into={},
        mentions={},
        neighbors={},
        label_namespaces={},
        compensated_candidates={},
        keep_pair_keys={(1, "ns-b:src:node-component")},
        namespace_filter=None,
    )

    flags = {(pair.candidate_id, pair.decided_keep) for pair in pairs}
    assert flags == {
        ("ns-b:src:node-component", True),
        ("ns-c:src:node-component", False),
    }, "decided_keep is per (seq, candidate_id) key, not per entry"

    remaining = module._stratifiable_pairs(pairs)
    assert [pair.candidate_id for pair in remaining] == ["ns-c:src:node-component"], (
        "the kept pair leaves the stratum while the undecided sibling stays"
    )


def test_a_rolled_back_pair_keeps_its_behaviour_beside_a_kept_pair() -> None:
    """The pre-T9a exclusions (rolled_back, missing entities) are unchanged."""
    module = _load_script()

    def _synthetic(seq: int, label: str, *, decided_keep: bool, rolled_back: bool) -> object:
        return module.PairAudit(
            seq=seq,
            canonical_id=f"ns-a:src:{label}-concept",
            candidate_id=f"ns-b:src:{label}-concept",
            canonical_namespace="ns-a:src",
            candidate_namespace="ns-b:src",
            label=label,
            description_overlap=0.1,
            risk=RiskLevel.NONE,
            lexically_silent=True,
            decided_keep=decided_keep,
            rolled_back=rolled_back,
        )

    kept = _synthetic(1, "alpha", decided_keep=True, rolled_back=False)
    rolled = _synthetic(2, "beta", decided_keep=False, rolled_back=True)
    plain = _synthetic(3, "gamma", decided_keep=False, rolled_back=False)

    result = module._stratifiable_pairs([kept, rolled, plain])

    assert [pair.seq for pair in result] == [3], (
        "a rolled-back pair keeps its existing exclusion next to a kept pair"
    )


def test_the_keep_keys_come_from_the_shared_read_model() -> None:
    """The script imports AND calls the shared read model — no re-implementation."""
    imported = _imported_names(_TREE)
    shared = imported.get("book_graph_rag.domain.cross_namespace_decision_models", set())
    assert "keep_pair_keys" in shared, "the keep keys must come from the shared read model"
    adapter = imported.get("book_graph_rag.infrastructure.jsonl_cross_namespace_decisions", set())
    assert "JSONLCrossNamespaceDecisions" in adapter, "reads go through the shared adapter"
    called = _called_names(_TREE)
    assert "keep_pair_keys" in called, "importing is not enough: it must be called"
    assert "InvalidDecisionRecord" in shared, "a corrupt registry must fail loudly, typed"


def test_corrupt_decisions_registry_fails_loudly_and_a_missing_file_reads_empty(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Missing = no decisions (empty set); corrupt = non-zero exit, typed."""
    module = _load_script()
    assert module._load_keep_pair_keys(tmp_path / "absent.jsonl") == set()

    corrupt = tmp_path / "decisions.jsonl"
    corrupt.write_text('{"seq": "not-an-int"}\n', encoding="utf-8")

    with pytest.raises(SystemExit) as excinfo:
        module._load_keep_pair_keys(corrupt)

    assert excinfo.value.code != 0
    assert "decisions.jsonl" in capsys.readouterr().err


def test_main_reads_the_decisions_before_opening_the_graph() -> None:
    """The registry read (and its fail-loud path) runs BEFORE any driver call."""
    main_node = next(
        node
        for node in _TREE.body
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == "main"
    )

    def _first_line(matcher: Callable[[ast.AST], bool]) -> int:
        lines = [
            node.lineno
            for node in ast.walk(main_node)
            if isinstance(node, ast.stmt | ast.expr) and matcher(node)
        ]
        assert lines, "call not found in main"
        return min(lines)

    load_line = _first_line(
        lambda node: (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_load_keep_pair_keys"
        )
    )
    driver_line = _first_line(
        lambda node: (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "driver"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "AsyncGraphDatabase"
        )
    )
    assert load_line < driver_line, "a corrupt registry must fail before any graph query"


def test_console_history_reports_the_decided_keep_counter(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The console line next to the rolled_back counters carries the count,
    and the JSON payload exposes it as ``selection.decided_keep_pairs``."""
    module = _load_script()
    module._print_history(
        entries_total=4,
        selected=[],
        compensating_count=0,
        applied_count=0,
        fully_compensated=0,
        partially_compensated=0,
        compensated_pair_count=1,
        decided_keep_pairs=8,
        pair_count=10,
        skipped_entities=0,
        missing_pairs=0,
        fallback_pairs=0,
    )

    out = capsys.readouterr().out
    assert "decididos keep: 8" in out
    assert "pares revertidos por rollback: 1" in out, (
        "the keep counter sits on the rolled_back counters line"
    )
    assert '"decided_keep_pairs"' in _SOURCE, "the JSON selection must expose the count too"


# ── Pure stratification over synthetic pairs ─────────────────────────────────


def _pair(
    module: ModuleType,
    seq: int,
    overlap: float,
    risk: object,
    silent: bool,
    label: str,
    language: LanguageNote | None = None,
) -> object:
    """Synthetic pair: only the fields the stratification reads are set."""
    return module.PairAudit(
        seq=seq,
        canonical_id=f"ns-a:src:{label}-concept",
        candidate_id=f"ns-b:src:{label}-concept",
        canonical_namespace="ns-a:src",
        candidate_namespace="ns-b:src",
        label=label,
        description_overlap=overlap,
        risk=risk,
        lexically_silent=silent,
        language=language or LanguageNote(),
    )


def test_stratify_matrix_counts_and_list_membership() -> None:
    """Matrix counts and the three lists' membership/ordering are pinned."""
    module = _load_script()
    thresholds = BandThresholds()
    pairs = [
        _pair(module, 1, 0.60, _HIGH, False, "embeddings"),  # strong -> clear
        # silent + cross-language -> needs_reading (the cosine shortlist).
        _pair(module, 2, 0.00, _HIGH, True, "zzz", _CROSS_EN_ES),
        _pair(module, 3, 0.00, _HIGH, False, "agent"),  # none_rest + alta -> suspicious
        _pair(module, 4, 0.30, _MEDIUM, False, "llm"),  # ambiguous + media -> suspicious
        _pair(module, 5, 0.25, _HIGH, False, "modularity"),  # ambiguous + alta -> suspicious
        _pair(module, 6, 0.70, _NONE, False, "embeddings"),  # strong, no risk -> clear
        _pair(module, 7, 0.05, _NONE, False, "agent"),  # absent, no risk -> matrix only
        # silent + undeterminable language -> stays a reading case (T8b).
        _pair(module, 8, 0.00, _NONE, True, "agent", _UNKNOWN_LANG),
    ]

    result = module.stratify(pairs, thresholds)

    matrix = result.matrix
    assert matrix[RiskLevel.HIGH] == {
        "strong": 1,
        "ambiguous": 1,
        "silent_cross_language": 1,
        "silent_same_language": 0,
        "silent_language_unknown": 0,
        "none_rest": 1,
    }
    assert matrix[RiskLevel.MEDIUM] == {
        "strong": 0,
        "ambiguous": 1,
        "silent_cross_language": 0,
        "silent_same_language": 0,
        "silent_language_unknown": 0,
        "none_rest": 0,
    }
    assert matrix[RiskLevel.NONE] == {
        "strong": 1,
        "ambiguous": 0,
        "silent_cross_language": 0,
        "silent_same_language": 0,
        "silent_language_unknown": 1,
        "none_rest": 1,
    }

    # clear identity: strong evidence, ordered by overlap descending.
    assert [pair.seq for pair in result.clear_identity] == [6, 1]
    # suspicious: high before medium, then the weakest evidence first; the
    # cross-language silent pair (seq 2) is NOT suspicious — it is a reading
    # case, and neither is the unknown-language one (seq 8).
    assert [pair.seq for pair in result.suspicious] == [3, 5, 4]
    # needs reading: silent pairs whose language does not argue against
    # identity, ordered by label then seq.
    assert [pair.seq for pair in result.needs_reading] == [8, 2]

    assert result.totals == {
        "risk_high": 4,
        "risk_medium": 1,
        "risk_none": 3,
        "clear_identity": 2,
        "suspicious": 3,
        "needs_reading": 2,
        "silent_cross_language": 1,
        "silent_same_language": 0,
        "silent_language_unknown": 1,
    }


def test_stratify_splits_the_silent_class_by_language_stratum() -> None:
    """T8b: same-language silence is evidence AGAINST identity (suspicious);
    cross-language silence is uninformative (needs reading / cosine); an
    undeterminable language stays a reading case; ambiguous and strong keep
    their pre-T8b destinations."""
    module = _load_script()
    thresholds = BandThresholds()
    pairs = [
        _pair(module, 1, 0.60, _MEDIUM, False, "embeddings"),  # strong -> clear
        _pair(module, 2, 0.30, _MEDIUM, False, "llm"),  # ambiguous -> suspicious
        _pair(module, 3, 0.00, _MEDIUM, True, "zzz", _CROSS_EN_ES),  # -> reading
        _pair(module, 4, 0.00, _MEDIUM, True, "zzz", _SAME_ES),  # -> suspicious
        _pair(module, 5, 0.00, _MEDIUM, True, "zzz", _UNKNOWN_LANG),  # -> reading
        _pair(module, 6, 0.00, _MEDIUM, False, "agent"),  # absent + media -> suspicious
        # Same-language silence is suspicious even WITHOUT label risk: the
        # silence itself is the evidence against the merge.
        _pair(module, 7, 0.00, _NONE, True, "zzz", _SAME_ES),
    ]

    result = module.stratify(pairs, thresholds)

    assert [pair.seq for pair in result.clear_identity] == [1]
    assert [pair.seq for pair in result.needs_reading] == [3, 5]
    # Suspicious sorts high-risk first, then lowest overlap: seq 4 (silent
    # same-language), 6, 7 (all overlap 0.0, ascending seq), then 2 at 0.30.
    assert [pair.seq for pair in result.suspicious] == [4, 6, 7, 2]

    # The medium row exercises all six strata exactly once.
    assert result.matrix[RiskLevel.MEDIUM] == {
        "strong": 1,
        "ambiguous": 1,
        "silent_cross_language": 1,
        "silent_same_language": 1,
        "silent_language_unknown": 1,
        "none_rest": 1,
    }
    assert result.matrix[RiskLevel.NONE] == {
        "strong": 0,
        "ambiguous": 0,
        "silent_cross_language": 0,
        "silent_same_language": 1,
        "silent_language_unknown": 0,
        "none_rest": 0,
    }
    assert result.totals == {
        "risk_medium": 6,
        "risk_none": 1,
        "risk_high": 0,
        "clear_identity": 1,
        "suspicious": 4,
        "needs_reading": 2,
        "silent_cross_language": 1,
        "silent_same_language": 2,
        "silent_language_unknown": 1,
    }


def test_stratify_strength_boundaries_come_from_shared_thresholds() -> None:
    """Strong starts at high_context, ambiguous at conflict_floor (shared)."""
    module = _load_script()
    thresholds = BandThresholds()
    pairs = [
        _pair(module, 1, thresholds.high_context, _HIGH, False, "a"),
        _pair(module, 2, thresholds.conflict_floor, _HIGH, False, "b"),
        _pair(module, 3, thresholds.conflict_floor / 2, _HIGH, False, "c"),
    ]

    result = module.stratify(pairs, thresholds)

    assert result.matrix[RiskLevel.HIGH] == {
        "strong": 1,
        "ambiguous": 1,
        "silent_cross_language": 0,
        "silent_same_language": 0,
        "silent_language_unknown": 0,
        "none_rest": 1,
    }
    assert [pair.seq for pair in result.clear_identity] == [1]
    # seq 3 is at absent evidence but NOT silent (overlap above zero) with
    # high risk, so it is suspicious, not a reading case.
    assert [pair.seq for pair in result.suspicious] == [3, 2]
    assert result.needs_reading == ()


def test_lexically_silent_rule_at_its_boundary() -> None:
    """Zero overlap + both descriptions substantial (documented floor).

    The floor is ``LEXICAL_SILENCE_MIN_CHARS`` characters per side (trimmed):
    at or above it, zero token overlap means the lexical number genuinely
    cannot judge (cross-language or framing gap) and a human must read;
    below it, the description is simply too short for overlap to mean anything.
    """
    module = _load_script()
    floor = module.LEXICAL_SILENCE_MIN_CHARS
    substantial_a = "a" * floor
    substantial_b = "b" * floor

    # Boundary: exactly at the floor on both sides -> silent.
    assert module.is_lexically_silent(0.0, substantial_a, substantial_b) is True
    # One char short on either side -> not silent (too little text to judge).
    assert module.is_lexically_silent(0.0, "a" * (floor - 1), substantial_b) is False
    assert module.is_lexically_silent(0.0, substantial_a, "b" * (floor - 1)) is False
    # Empty / whitespace-only descriptions are never "silent", just absent.
    assert module.is_lexically_silent(0.0, "", "") is False
    assert module.is_lexically_silent(0.0, " " * (floor * 2), " " * (floor * 2)) is False
    # Any nonzero overlap means the lexical number did judge -> not silent.
    assert module.is_lexically_silent(0.09, substantial_a, substantial_b) is False
