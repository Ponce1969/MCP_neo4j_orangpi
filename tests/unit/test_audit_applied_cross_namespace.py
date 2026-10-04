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

The graph/ledger integration itself is exercised only by the maintainer's
production run (this machine has no local copy of the 958-entry ledger).
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

from book_graph_rag.domain.quarantine_review_models import RiskLevel, label_risk
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
            "RiskLevel",
            "format_risk_marker",
            "is_generic_label",
            "label_risk",
            "mention_snippet",
            "reading_for",
        }
    ),
    "book_graph_rag.domain.merge_ledger_models": frozenset({"MergeLedgerEntry"}),
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
    for function_name in ("reading_for", "label_risk", "description_overlap"):
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


# ── Pure stratification over synthetic pairs ─────────────────────────────────


def _pair(
    module: ModuleType,
    seq: int,
    overlap: float,
    risk: object,
    silent: bool,
    label: str,
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
    )


def test_stratify_matrix_counts_and_list_membership() -> None:
    """Matrix counts and the three lists' membership/ordering are pinned."""
    module = _load_script()
    thresholds = BandThresholds()
    pairs = [
        _pair(module, 1, 0.60, _HIGH, False, "embeddings"),  # strong -> clear
        _pair(module, 2, 0.00, _HIGH, True, "zzz"),  # none_silent -> reading
        _pair(module, 3, 0.00, _HIGH, False, "agent"),  # none_rest + alta -> suspicious
        _pair(module, 4, 0.30, _MEDIUM, False, "llm"),  # ambiguous + media -> suspicious
        _pair(module, 5, 0.25, _HIGH, False, "modularity"),  # ambiguous + alta -> suspicious
        _pair(module, 6, 0.70, _NONE, False, "embeddings"),  # strong, no risk -> clear
        _pair(module, 7, 0.05, _NONE, False, "agent"),  # absent, no risk -> matrix only
        _pair(module, 8, 0.00, _NONE, True, "agent"),  # none_silent, no risk -> reading
    ]

    result = module.stratify(pairs, thresholds)

    matrix = result.matrix
    assert matrix[RiskLevel.HIGH] == {
        "strong": 1,
        "ambiguous": 1,
        "none_silent": 1,
        "none_rest": 1,
    }
    assert matrix[RiskLevel.MEDIUM] == {
        "strong": 0,
        "ambiguous": 1,
        "none_silent": 0,
        "none_rest": 0,
    }
    assert matrix[RiskLevel.NONE] == {
        "strong": 1,
        "ambiguous": 0,
        "none_silent": 1,
        "none_rest": 1,
    }

    # clear identity: strong evidence, ordered by overlap descending.
    assert [pair.seq for pair in result.clear_identity] == [6, 1]
    # suspicious: high before medium, then the weakest evidence first; the
    # lexically silent pair (seq 2) is NOT suspicious — it is a reading case.
    assert [pair.seq for pair in result.suspicious] == [3, 5, 4]
    # needs reading: lexically silent, ordered by label then seq.
    assert [pair.seq for pair in result.needs_reading] == [8, 2]

    assert result.totals == {
        "risk_high": 4,
        "risk_medium": 1,
        "risk_none": 3,
        "clear_identity": 2,
        "suspicious": 3,
        "needs_reading": 2,
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
        "none_silent": 0,
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
