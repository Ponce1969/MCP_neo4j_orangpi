"""Anti-drift tests for the cross-namespace sample renderer (T2b).

Two protection layers over ``scripts-ops/render_cross_namespace_sample.py``:

1. **Static anti-drift** — the script must import the shared evidence model
   (``domain/quarantine_review_models``) and must keep no threshold literals
   nor reading vocabulary of its own. Mechanism: the module source is parsed
   once as an AST, and (a) import statements, (b) float constants and
   (c) function definitions are inspected; a documented regex over the raw
   source additionally catches literals hidden inside docstrings or strings
   (an AST-only check would miss them). If someone re-adds a local
   ``_reading`` helper, a ``0.50``/``0.10`` literal or the old
   ``strong_identity_evidence`` label, these tests fail before the numbers
   can drift apart from the CLI and the audit.
2. **Behaviour pins** — the script's pair/group payloads are exercised with
   synthetic entities and asserted equal to the shared model's output
   (``reading_for``, structural labels, ``label_risk``/``format_risk_marker``),
   plus the already-merged ledger case the shared sheet must not turn into a
   merge proposal.

The other three synthetic reading scenarios (same-language duplicate,
cross-language duplicate, generic single-word collision) are already covered
by the ``reading_for`` block of ``tests/unit/test_quarantine_review_models.py``
and by the CLI suite ``tests/integration/test_quarantine_review_cli.py``;
they are not duplicated here.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

from book_graph_rag.domain.models import Entity
from book_graph_rag.domain.quarantine_review_models import (
    PRIMARY_SIGNAL,
    ROUTING_NOTE,
    STRUCTURAL_SIGNALS,
    DecisionSheet,
    EvidenceReading,
    PriorMergeFacts,
    RiskLevel,
    SheetEvidence,
    SheetMember,
    format_risk_marker,
    format_sheet,
    label_risk,
    reading_for,
)
from book_graph_rag.domain.s3_context_scoring import description_overlap
from book_graph_rag.domain.s4_band_assignment import BandThresholds

_ROOT = Path(__file__).parents[2]
_SCRIPT_PATH = _ROOT / "scripts-ops" / "render_cross_namespace_sample.py"
_SOURCE = _SCRIPT_PATH.read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)
_SHARED_MODULE = "book_graph_rag.domain.quarantine_review_models"

_ES_DUPLICATE_A = (
    "Representación vectorial de texto que permite buscar documentos por "
    "significado y medir similitud semántica."
)
_ES_DUPLICATE_B = (
    "Representación vectorial de texto que permite buscar documentos por "
    "significado y comparar similitud semántica."
)


def _load_script() -> ModuleType:
    """Import the ops script as a module (no graph, no CLI args)."""
    spec = importlib.util.spec_from_file_location("render_cross_namespace_sample", _SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec so module-level dataclasses resolve (T6 pattern).
    sys.modules["render_cross_namespace_sample"] = module
    spec.loader.exec_module(module)
    return module


def _shared_import_names(tree: ast.AST) -> set[str]:
    """Names imported from the shared evidence model anywhere in the script."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == _SHARED_MODULE:
            names.update(alias.name for alias in node.names)
    return names


def _entity(entity_id: str, description: str) -> Entity:
    """Synthetic live entity for payload pins (no graph involved)."""
    return Entity(id=entity_id, name="embeddings concept", type="concept", description=description)


# ── Static anti-drift ────────────────────────────────────────────────────────


def test_script_imports_the_shared_evidence_model() -> None:
    """The script reads evidence rules from the shared model, not its own."""
    imported = _shared_import_names(_TREE)
    assert {"reading_for", "label_risk", "format_risk_marker"} <= imported, imported


def test_script_carries_no_threshold_literals_of_its_own() -> None:
    """No ``0.50``/``0.10`` lives in the script — BandThresholds() owns them.

    Mechanism: (a) AST scan for float constants equal to either threshold
    (catches ``HIGH_CONTEXT = 0.50``-style definitions and inline comparisons
    such as ``composite >= 0.5``); (b) documented regex ``0\\.50|0\\.10`` over
    the raw source, which additionally catches literals written inside the
    module docstring or any string, where the AST would only see text.
    """
    float_constants = {
        node.value
        for node in ast.walk(_TREE)
        if isinstance(node, ast.Constant) and isinstance(node.value, float)
    }
    assert float_constants.isdisjoint({0.5, 0.1}), float_constants
    assert re.search(r"0\.50|0\.10", _SOURCE) is None, "threshold literal in script source"


def test_script_defines_no_reading_vocabulary_of_its_own() -> None:
    """The old local ``_reading`` helper and its labels must stay gone.

    Mechanism: AST walk over function definitions (no ``_reading`` may be
    redefined) plus a source check for the retired label
    ``strong_identity_evidence``, which only existed in the script's private
    copy of the reading. ``reading_for`` must actually be called.
    """
    functions = {
        node.name
        for node in ast.walk(_TREE)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_reading" not in functions, "local reading helper reintroduced"
    assert "strong_identity_evidence" not in _SOURCE, "retired reading label reintroduced"
    calls_reading_for = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "reading_for"
        for node in ast.walk(_TREE)
    )
    assert calls_reading_for, "script never calls the shared reading_for"


# ── Behaviour pins over synthetic pairs ──────────────────────────────────────


def test_payload_reading_equals_shared_reading_over_description_overlap() -> None:
    """Same-language duplicate: payload reading is exactly the shared reading.

    The script used to grade the S3 mean composite with its own thresholds;
    this pin fails for any payload whose reading is not what
    ``reading_for(description_overlap)`` returns (design §4.1).
    """
    module = _load_script()
    anchor = _entity("knowledge:alpha:embeddings-concept", _ES_DUPLICATE_A)
    candidate = _entity("knowledge:beta:embeddings-concept", _ES_DUPLICATE_B)
    thresholds = BandThresholds()

    payload = module._pair_payload(anchor, candidate, {}, {}, thresholds)

    overlap = description_overlap(anchor.description, candidate.description)
    assert overlap >= thresholds.high_context
    assert payload["reading"] == reading_for(overlap, thresholds).value
    assert payload["reading"] == EvidenceReading.IDENTITY.value


def test_payload_labels_evidence_with_the_shared_structural_constants() -> None:
    """The payload marks description_overlap primary and both jaccards structural."""
    module = _load_script()
    anchor = _entity("knowledge:alpha:embeddings-concept", _ES_DUPLICATE_A)
    candidate = _entity("knowledge:beta:embeddings-concept", _ES_DUPLICATE_B)

    payload = module._pair_payload(anchor, candidate, {}, {}, BandThresholds())

    assert payload["primary_signal"] == PRIMARY_SIGNAL
    assert tuple(payload["structural_signals"]) == STRUCTURAL_SIGNALS
    assert payload["cosine_computed"] is False


def test_group_payload_risk_marker_comes_from_the_shared_rule() -> None:
    """Single-word label across 3 namespaces carries the shared 'alta' marker."""
    module = _load_script()
    entities = {
        entity_id: Entity(id=entity_id, name="agent", type="concept", description="")
        for entity_id in (
            "knowledge:alpha:agent-concept",
            "knowledge:beta:agent-concept",
            "books:gamma:agent-concept",
        )
    }
    group = module.CandidateGroup(
        group_key="agent",
        entity_type="concept",
        member_ids=tuple(sorted(entities)),
        namespaces=frozenset({"knowledge:alpha", "knowledge:beta", "books:gamma"}),
        single_word=True,
        degree=0,
        stratum="generic",
    )

    payload = module._group_payload(
        group,
        entities,
        {},
        {},
        BandThresholds(),
        namespace_filter=None,
    )

    expected = format_risk_marker(label_risk(single_word=True, namespace_count=3))
    assert payload["risk_marker"] == expected
    assert payload["risk_marker"].startswith("⚠ alta")
    assert payload["risk"] == {
        "level": RiskLevel.HIGH.value,
        "single_word": True,
        "namespace_count": 3,
        "reason": "genérica · 3 nss",
    }


# ── Already-merged pair: evidence never becomes a merge proposal ─────────────


def test_already_merged_pair_keeps_quarantine_and_surfaces_the_ledger() -> None:
    """Ledger shows an applied merge: identity evidence still never proposes.

    A pair with identical same-language descriptions reads ``identity``, and
    the ledger records a previous merge between the two ids. The sheet must
    keep the constant R6.2 quarantine routing (it has no merge-proposal field
    to escalate into), must surface the prior merge as history, and must keep
    claiming neither a cosine nor an S4 band.
    """
    thresholds = BandThresholds()
    overlap = description_overlap(_ES_DUPLICATE_A, _ES_DUPLICATE_B)
    reading = reading_for(overlap, thresholds)
    assert reading is EvidenceReading.IDENTITY

    anchor = SheetMember(
        entity_id="knowledge:alpha:embeddings-concept",
        namespace="knowledge:alpha",
        name="embeddings concept",
        entity_type="concept",
        description=_ES_DUPLICATE_A,
    )
    candidate = SheetMember(
        entity_id="knowledge:beta:embeddings-concept",
        namespace="knowledge:beta",
        name="embeddings concept",
        entity_type="concept",
        description=_ES_DUPLICATE_B,
    )
    evidence = SheetEvidence(
        description_overlap=overlap,
        mentions_jaccard=0.0,
        mentions_shared=0,
        mentions_union=0,
        related_jaccard=0.0,
        related_shared=0,
        related_union=0,
        composite=overlap / 3.0,
        reading=reading,
        s0_matched_field="none",
        s2_type_gate_passed=True,
        s2_type_gate_reason="anchor type concept matches candidate type concept",
    )
    sheet = DecisionSheet(
        seq=9,
        label="embeddings concept",
        entity_type="concept",
        generic_label=False,
        cross_namespace=True,
        risk=label_risk(single_word=False, namespace_count=2),
        members=(anchor, candidate),
        evidence=evidence,
        prior_merge=PriorMergeFacts(
            seq=7,
            applied_at=datetime(2026, 9, 30, 10, 5, tzinfo=UTC),
        ),
        thresholds=thresholds,
    )

    # Identity evidence + a prior merge: routing is still the constant note.
    assert sheet.routing == ROUTING_NOTE
    assert sheet.evidence.reading is EvidenceReading.IDENTITY
    assert sheet.evidence.cosine_computed is False
    assert sheet.evidence.s4_band_claimed is False
    assert sheet.approve_command is None
    # The ledger history is rendered as history, not as a decision.
    rendered = format_sheet(sheet)
    assert "seq 7 · 2026-09-30T10:05Z" in rendered
    assert "sin merge previo" not in rendered
    assert ROUTING_NOTE in rendered
