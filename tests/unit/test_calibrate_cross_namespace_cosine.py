"""Anti-drift and behavior tests for the T8 cosine calibration script.

Two protection layers over ``scripts-ops/calibrate_cross_namespace_cosine.py``
(mirroring the sibling audit/renderer suites):

1. **Static anti-drift** — the script must import the shared model
   (``BandThresholds`` for the cosine bands, ``reading_for`` for the lexical
   reading, the shared ``cosine_similarity``), must keep no threshold literal
   of its own anywhere in its source (AST float constants plus a documented
   regex over the raw text, which also catches literals hidden inside
   docstrings), and must contain no write-Cypher keyword anywhere in its
   source — every ``_QUERY*`` constant must be ``MATCH``-only. Re-divergence
   fails statically, before any run.
2. **Behavior over a fake embedding provider** — deterministic sampling per
   seed, the cosine math (identical -> 1.0, orthogonal -> 0.0), the
   stratum x band matrix, the three verdicts, the headline rescue count, the
   exact provider call count (two per sampled pair), and ``--dry-run`` making
   ZERO provider calls and ZERO graph fetches.

No test opens a network connection, no test loads a model, no test touches
the graph: the provider and the description fetcher are injected fakes.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from book_graph_rag.ports.embedding_provider_port import (
    EmbeddingBatch,
    EmbeddingProviderPort,
    EmbeddingRequest,
    EmbeddingVector,
)

_ROOT = Path(__file__).parents[2]
_SCRIPT_PATH = _ROOT / "scripts-ops" / "calibrate_cross_namespace_cosine.py"
_SOURCE = _SCRIPT_PATH.read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)

#: Write-Cypher keywords must never appear in the script source (the script is
#: read-only by construction). Case-sensitive on purpose: Cypher keywords are
#: uppercase; prose uses lowercase words that only look related.
_WRITE_KEYWORDS = re.compile(r"\b(CREATE|DELETE|MERGE|DETACH|SET|REMOVE|DROP)\b")

#: Threshold values owned by ``BandThresholds()`` (high 0.90 / medium 0.80 and
#: the lexical 0.50 / 0.10), never by the script. Longest alternative first so
#: ``0.90`` matches as a whole before ``0.9``.
_THRESHOLD_PATTERN = re.compile(r"(?<![\w.])0\.(?:90|80|50|10|9|8|5|1)(?![\d])")
_THRESHOLD_VALUES = {0.9, 0.8, 0.5, 0.1}

#: module -> names the script must import from it (the shared model).
_REQUIRED_IMPORTS: dict[str, frozenset[str]] = {
    "book_graph_rag.domain.s4_band_assignment": frozenset({"BandThresholds"}),
    "book_graph_rag.domain.quarantine_review_models": frozenset(
        {"EvidenceReading", "mention_snippet", "reading_for"}
    ),
    "book_graph_rag.infrastructure.brute_force_candidate_retrieval": frozenset(
        {"cosine_similarity"}
    ),
    "book_graph_rag.infrastructure.sentence_transformer_adapter": frozenset(
        {"SentenceTransformerAdapter"}
    ),
    "book_graph_rag.ports.embedding_provider_port": frozenset(
        {"EmbeddingProviderPort", "EmbeddingRequest"}
    ),
}

_MODEL_ID = "fake-model"
#: Cosine band names asserted in payloads (labels come from BandThresholds).
_BAND_HIGH = "high_cosine"
_BAND_MEDIUM = "medium_cosine"
_BAND_LOW = "low_cosine"


def _load_script() -> ModuleType:
    """Import the ops script as a module (no CLI args, no graph, no provider)."""
    spec = importlib.util.spec_from_file_location("calibrate_cross_namespace_cosine", _SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec so module-level dataclasses resolve (T6 pattern).
    sys.modules["calibrate_cross_namespace_cosine"] = module
    spec.loader.exec_module(module)
    return module


def _imported_names(tree: ast.AST) -> dict[str, set[str]]:
    names: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            names.setdefault(node.module, set()).update(alias.name for alias in node.names)
    return names


def _called_names(tree: ast.AST) -> set[str]:
    return {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


def _query_constants(tree: ast.Module) -> list[tuple[str, str]]:
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


def test_script_imports_the_shared_model() -> None:
    """Bands, lexical reading, cosine math and adapter all come from the project."""
    imported = _imported_names(_TREE)
    for module, required in _REQUIRED_IMPORTS.items():
        missing = required - imported.get(module, set())
        assert not missing, f"{module}: missing shared imports {sorted(missing)}"


def test_script_actually_calls_the_shared_rules() -> None:
    """Importing is not enough: the shared rules must be called, not shadowed."""
    called = _called_names(_TREE)
    for function_name in ("BandThresholds", "reading_for", "cosine_similarity"):
        assert function_name in called, f"script never calls the shared {function_name}"


def test_script_carries_no_threshold_literals_of_its_own() -> None:
    """No band/lexical threshold number lives in the script — BandThresholds owns it."""
    float_constants = {
        node.value
        for node in ast.walk(_TREE)
        if isinstance(node, ast.Constant) and isinstance(node.value, float)
    }
    assert float_constants.isdisjoint(_THRESHOLD_VALUES), float_constants
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


# ── Fakes (no provider, no graph, no network) ───────────────────────────────


def _stable_vector(text: str) -> tuple[float, ...]:
    """Deterministic, never-zero 2D vector derived from the text bytes."""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return (1.0 + digest[0] / 255.0, 1.0 + digest[1] / 255.0)


class FakeEmbeddingProvider(EmbeddingProviderPort):
    """Deterministic fake provider with an explicit call counter.

    Default vectors come from ``_stable_vector`` (identical text -> identical
    vector); an override map pins exact vectors where the test needs an exact
    cosine (1.0 / 0.0 / a medium-band value).
    """

    def __init__(self, vectors: Mapping[str, tuple[float, ...]] | None = None) -> None:
        self.calls = 0
        self.seen_model_ids: list[str] = []
        self._vectors: dict[str, tuple[float, ...]] = dict(vectors) if vectors else {}

    async def embed(self, request: EmbeddingRequest) -> EmbeddingBatch:
        self.calls += 1
        self.seen_model_ids.append(request.model_id)
        return EmbeddingBatch(
            model_id=request.model_id,
            vectors=[
                EmbeddingVector(
                    values=self._vectors.get(text, _stable_vector(text)),
                    model_id=request.model_id,
                )
                for text in request.texts
            ],
        )

    def model_dim(self, model_id: str) -> int:
        return 2


class SpyFetch:
    """Description fetcher spy: counts graph fetches, serves a fixed dict."""

    def __init__(self, descriptions: dict[str, str]) -> None:
        self.calls = 0
        self._descriptions = descriptions

    async def __call__(self, ids: Sequence[str]) -> dict[str, str]:
        self.calls += 1
        return {entity_id: self._descriptions[entity_id] for entity_id in ids}


# ── Synthetic input payloads (audit schema layout) ──────────────────────────


def _write_input(
    tmp_path: Path,
    rows: list[dict[str, Any]],
    *,
    schema: str = "applied-cross-namespace-audit/2",
    name: str = "audit.json",
) -> Path:
    """Synthetic audit payload carrying pair ids + lexical stratum per pair."""
    pairs = [
        {
            "seq": row["seq"],
            "canonical_id": row["canonical_id"],
            "candidate_id": row["candidate_id"],
            "label": row.get("label", "concept"),
            "entity_type": "concept",
            "evidence": {
                "description_overlap": row["overlap"],
                "strength": row["stratum"],
                "reading": row.get("reading", ""),
            },
            "canonical": {"description": "truncated copy in the audit payload"},
            "candidate": {"description": "truncated copy in the audit payload"},
            "missing_entities": row.get("missing", False),
        }
        for row in rows
    ]
    columns: list[str] = []
    for row in rows:
        if row["stratum"] not in columns:
            columns.append(row["stratum"])
    payload = {
        "schema": schema,
        "generated_at": "2026-10-03T00:00:00+00:00",
        "read_only": True,
        "stratification": {"strength_columns": columns},
        "pairs": pairs,
    }
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8", newline="\n")
    return path


async def _run(
    module: ModuleType,
    *,
    input_path: Path,
    out_path: Path,
    provider: EmbeddingProviderPort,
    fetch: SpyFetch,
    dry_run: bool = False,
    per_stratum: int = 5,
    seed: int = 7,
    thresholds: Any | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = await module.calibrate(
        input_path=input_path,
        per_stratum=per_stratum,
        seed=seed,
        dry_run=dry_run,
        out_path=out_path,
        model_id=_MODEL_ID,
        thresholds=thresholds if thresholds is not None else module.BandThresholds(),
        provider=provider,
        fetch_descriptions=fetch,
    )
    return result


def _row(
    seq: int,
    stratum: str,
    overlap: float,
    canonical_id: str,
    candidate_id: str,
    **extra: Any,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "seq": seq,
        "stratum": stratum,
        "overlap": overlap,
        "canonical_id": canonical_id,
        "candidate_id": candidate_id,
    }
    row.update(extra)
    return row


# ── Behavior: cosine math ───────────────────────────────────────────────────


async def test_identical_vectors_yield_cosine_one(tmp_path: Path) -> None:
    """Both sides pinned to the same vector -> cosine exactly 1.0."""
    module = _load_script()
    input_path = _write_input(tmp_path, [_row(1, "none_rest", 0.0, "ns-a:s:c1", "ns-b:s:c1")])
    descriptions = {"ns-a:s:c1": "text one", "ns-b:s:c1": "text two"}
    provider = FakeEmbeddingProvider({"text one": (1.0, 0.0), "text two": (1.0, 0.0)})
    payload = await _run(
        module,
        input_path=input_path,
        out_path=tmp_path / "out.json",
        provider=provider,
        fetch=SpyFetch(descriptions),
    )
    assert payload["pairs"][0]["cosine"] == pytest.approx(1.0)
    assert payload["pairs"][0]["cosine_band"] == _BAND_HIGH


async def test_orthogonal_vectors_yield_cosine_zero(tmp_path: Path) -> None:
    """Orthogonal pinned vectors -> cosine 0.0, low band, doubt verdict."""
    module = _load_script()
    input_path = _write_input(tmp_path, [_row(1, "none_rest", 0.0, "ns-a:s:c1", "ns-b:s:c1")])
    descriptions = {"ns-a:s:c1": "text one", "ns-b:s:c1": "text two"}
    provider = FakeEmbeddingProvider({"text one": (1.0, 0.0), "text two": (0.0, 1.0)})
    payload = await _run(
        module,
        input_path=input_path,
        out_path=tmp_path / "out.json",
        provider=provider,
        fetch=SpyFetch(descriptions),
    )
    assert payload["pairs"][0]["cosine"] == pytest.approx(0.0)
    assert payload["pairs"][0]["cosine_band"] == _BAND_LOW


# ── Behavior: deterministic sampling ────────────────────────────────────────


def _sampling_rows() -> list[dict[str, Any]]:
    return [
        _row(seq, "none_rest", 0.0, f"ns-a:s:c{seq}", f"ns-b:s:c{seq}")
        for seq in range(1, 9)  # 8 pairs in one stratum
    ]


async def test_sampling_is_deterministic_for_a_seed(tmp_path: Path) -> None:
    """Same seed -> byte-identical sample; a different seed picks differently."""
    module = _load_script()
    rows = _sampling_rows()

    async def _sample(seed: int) -> list[int]:
        provider = FakeEmbeddingProvider()
        payload = await _run(
            module,
            input_path=_write_input(tmp_path, rows, name=f"audit-{seed}.json"),
            out_path=tmp_path / f"out-{seed}.json",
            provider=provider,
            fetch=SpyFetch(
                {
                    row[key]: "same description"
                    for row in rows
                    for key in ("canonical_id", "candidate_id")
                }
            ),
            per_stratum=3,
            seed=seed,
        )
        return [pair["seq"] for pair in payload["pairs"]]

    first = await _sample(7)
    second = await _sample(7)
    other = await _sample(8)
    assert first == second, "same seed must reproduce the same sample"
    assert len(first) == 3
    assert first != other, "different seeds must pick different samples"


# ── Behavior: matrix, verdicts, headline, call count ────────────────────────

#: Five pairs across four strata with pinned vectors:
#: seq -> (canonical vector, candidate vector) => cosine band.
#:
#: 1 strong      + low    -> identity vs low  -> agree
#: 2 ambiguous   + high   -> weak vs high     -> cosine rescues it
#: 3 silent_cross + medium -> weak vs medium  -> agree (neither high nor low)
#: 4 none_rest   + low    -> weak vs low      -> cosine confirms the doubt
#: 5 none_rest   + high   -> weak vs high     -> cosine rescues it
_SCENARIO_ROWS = [
    _row(1, "strong", 0.60, "ns-a:s:alpha-can", "ns-b:s:alpha-cand"),
    _row(2, "ambiguous", 0.20, "ns-a:s:beta-can", "ns-b:s:beta-cand"),
    _row(3, "silent_cross_language", 0.00, "ns-a:s:gamma-can", "ns-b:s:gamma-cand"),
    _row(4, "none_rest", 0.00, "ns-a:s:delta-can", "ns-b:s:delta-cand"),
    _row(5, "none_rest", 0.00, "ns-a:s:epsilon-can", "ns-b:s:epsilon-cand"),
]

_SCENARIO_DESCRIPTIONS = {
    "ns-a:s:alpha-can": "alpha canonical description",
    "ns-b:s:alpha-cand": "alpha candidate description",
    "ns-a:s:beta-can": "beta canonical description",
    "ns-b:s:beta-cand": "beta candidate description",
    "ns-a:s:gamma-can": "gamma canonical description",
    "ns-b:s:gamma-cand": "gamma candidate description",
    "ns-a:s:delta-can": "delta canonical description",
    "ns-b:s:delta-cand": "delta candidate description",
    "ns-a:s:epsilon-can": "epsilon canonical description",
    "ns-b:s:epsilon-cand": "epsilon candidate description",
}

_SCENARIO_VECTORS = {
    # orthogonal -> low
    "alpha canonical description": (1.0, 0.0),
    "alpha candidate description": (0.0, 1.0),
    # identical -> high (cosine 1.0)
    "beta canonical description": (3.0, 4.0),
    "beta candidate description": (3.0, 4.0),
    # 4 / (sqrt(5) * 2) = 0.894 -> medium band (between shared thresholds)
    "gamma canonical description": (2.0, 1.0),
    "gamma candidate description": (2.0, 0.0),
    # orthogonal -> low
    "delta canonical description": (1.0, 0.0),
    "delta candidate description": (0.0, 1.0),
    # identical -> high
    "epsilon canonical description": (5.0, 1.0),
    "epsilon candidate description": (5.0, 1.0),
}


async def _run_scenario(tmp_path: Path) -> dict[str, Any]:
    module = _load_script()
    provider = FakeEmbeddingProvider(_SCENARIO_VECTORS)
    return await _run(
        module,
        input_path=_write_input(tmp_path, _SCENARIO_ROWS),
        out_path=tmp_path / "calibration.json",
        provider=provider,
        fetch=SpyFetch(_SCENARIO_DESCRIPTIONS),
        per_stratum=5,
        seed=7,
    )


async def test_matrix_counts_stratum_by_band(tmp_path: Path) -> None:
    """The stratum x cosine-band matrix counts each sampled pair exactly once."""
    payload = await _run_scenario(tmp_path)
    matrix = payload["matrix"]
    assert matrix["strong"] == {
        _BAND_HIGH: 0,
        _BAND_MEDIUM: 0,
        _BAND_LOW: 1,
        "total": 1,
    }
    assert matrix["ambiguous"] == {
        _BAND_HIGH: 1,
        _BAND_MEDIUM: 0,
        _BAND_LOW: 0,
        "total": 1,
    }
    assert matrix["silent_cross_language"] == {
        _BAND_HIGH: 0,
        _BAND_MEDIUM: 1,
        _BAND_LOW: 0,
        "total": 1,
    }
    assert matrix["none_rest"] == {
        _BAND_HIGH: 1,
        _BAND_MEDIUM: 0,
        _BAND_LOW: 1,
        "total": 2,
    }
    assert sum(row["total"] for row in matrix.values()) == 5


async def test_three_verdicts_are_assigned(tmp_path: Path) -> None:
    """Rescue (weak+high), doubt (weak+low), agree (otherwise) — all three fire."""
    payload = await _run_scenario(tmp_path)
    verdicts = {pair["seq"]: pair["verdict"] for pair in payload["pairs"]}
    assert verdicts[1] == "agree"  # identity reading, low cosine
    assert verdicts[2] == "cosine rescues it"  # undecided, high cosine
    assert verdicts[3] == "agree"  # weak, medium band -> neither rescue nor doubt
    assert verdicts[4] == "cosine confirms the doubt"  # weak, low cosine
    assert verdicts[5] == "cosine rescues it"  # no shared context, high cosine
    assert payload["verdict_counts"] == {
        "cosine rescues it": 2,
        "cosine confirms the doubt": 1,
        "agree": 2,
    }


async def test_headline_counts_rescued_no_lexical_evidence(tmp_path: Path) -> None:
    """Headline: how many sampled 'no lexical evidence' pairs the cosine rescues."""
    payload = await _run_scenario(tmp_path)
    # seq 3, 4, 5 read as no_shared_context (overlap below conflict_floor);
    # only seq 5 lands in the high band -> rescued.
    assert payload["headline"] == {
        "no_lexical_evidence_sampled": 3,
        "cosine_rescued": 1,
    }


async def test_happy_path_makes_exactly_two_calls_per_pair(tmp_path: Path) -> None:
    """Cost transparency: 2 provider calls per sampled pair, model id reported."""
    module = _load_script()
    provider = FakeEmbeddingProvider(_SCENARIO_VECTORS)
    fetch = SpyFetch(_SCENARIO_DESCRIPTIONS)
    payload = await _run(
        module,
        input_path=_write_input(tmp_path, _SCENARIO_ROWS),
        out_path=tmp_path / "calibration.json",
        provider=provider,
        fetch=fetch,
        per_stratum=5,
        seed=7,
    )
    sampled = sum(plan["sampled"] for plan in payload["plan"])
    assert sampled == 5
    assert provider.calls == 2 * sampled == 10
    assert payload["provider_calls"] == 10
    assert payload["planned_provider_calls"] == 10
    assert all(pair["provider_calls"] == 2 for pair in payload["pairs"])
    assert payload["model_id"] == _MODEL_ID
    assert set(provider.seen_model_ids) == {_MODEL_ID}
    assert fetch.calls == 1  # one batched graph fetch, MATCH only


async def test_dry_run_makes_zero_provider_calls(tmp_path: Path) -> None:
    """--dry-run: prints the plan, calls the provider ZERO times and the graph never."""
    module = _load_script()
    provider = FakeEmbeddingProvider(_SCENARIO_VECTORS)
    fetch = SpyFetch(_SCENARIO_DESCRIPTIONS)
    payload = await _run(
        module,
        input_path=_write_input(tmp_path, _SCENARIO_ROWS),
        out_path=tmp_path / "dry.json",
        provider=provider,
        fetch=fetch,
        per_stratum=5,
        seed=7,
        dry_run=True,
    )
    assert provider.calls == 0, "dry run must not embed anything"
    assert fetch.calls == 0, "dry run must not read the graph"
    assert payload["dry_run"] is True
    assert payload["provider_calls"] == 0
    assert payload["planned_provider_calls"] == 10  # 2 calls x 5 sampled pairs
    assert len(payload["pairs"]) == 5
    assert all(pair["cosine"] is None and pair["verdict"] is None for pair in payload["pairs"])
    assert all(plan["sampled"] == plan["population"] for plan in payload["plan"])


# ── Behavior: input schema handling ─────────────────────────────────────────


async def test_unknown_input_schema_is_rejected(tmp_path: Path) -> None:
    """Only the retro-audit schemas are consumed; anything else exits with 2."""
    module = _load_script()
    input_path = _write_input(
        tmp_path,
        [_row(1, "none_rest", 0.0, "ns-a:s:c1", "ns-b:s:c1")],
        schema="something-else/9",
    )
    with pytest.raises(SystemExit) as excinfo:
        await _run(
            module,
            input_path=input_path,
            out_path=tmp_path / "out.json",
            provider=FakeEmbeddingProvider(),
            fetch=SpyFetch({}),
        )
    assert excinfo.value.code == 2


async def test_legacy_schema_one_still_samples(tmp_path: Path) -> None:
    """The committed /1 payload (pre-language strata) remains consumable."""
    module = _load_script()
    rows = [_row(1, "none_silent", 0.0, "ns-a:s:c1", "ns-b:s:c1")]
    descriptions = {"ns-a:s:c1": "first side", "ns-b:s:c1": "second side"}
    provider = FakeEmbeddingProvider({"first side": (1.0, 0.0), "second side": (1.0, 0.0)})
    payload = await _run(
        module,
        input_path=_write_input(tmp_path, rows, schema="applied-cross-namespace-audit/1"),
        out_path=tmp_path / "out.json",
        provider=provider,
        fetch=SpyFetch(descriptions),
    )
    assert payload["input"]["schema"] == "applied-cross-namespace-audit/1"
    assert payload["pairs"][0]["stratum"] == "none_silent"
    assert payload["pairs"][0]["cosine"] == pytest.approx(1.0)
    assert provider.calls == 2
