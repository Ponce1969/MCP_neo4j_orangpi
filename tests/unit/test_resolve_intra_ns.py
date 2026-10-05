"""T10 contract of ``scripts-ops/resolve_intra_ns.py`` — no graph, every touch stubbed.

Four protections over the intra-namespace cleanup executor:

1. **Grouping parity (T10-A)** — the intra-namespace group key expression lives
   in ONE constant exported by ``neo4j_audit_adapter`` (the T9a
   ``CROSS_NAMESPACE_DECISION_EXCLUSION`` pattern): the ``duplicates_entity``
   rule and the script's grouping query are *built from* it (AST-checked), and
   the literal never appears in the script source, so plan and audit cannot
   drift apart after T9b's case-insensitive grouping. The deletion counters key
   on the PLAN built from that query (plan-scoped row fetches, no second
   grouping copy), so their parity is inherited by construction.
2. **Fingerprint (T10-C)** — ``plan_fingerprint`` is a deterministic sha256 over
   the ordered canonical plan (namespace, group name, kind, canonical_id,
   duplicate_ids): same plan, same hash; any changed field changes it.
   ``--apply`` REFUSES without ``--expect-fingerprint`` and on a mismatch, in
   both cases before any write (decision: refuse, not warn).
3. **Census (T10-D)** — the predicted delta is one active entity fewer and one
   ``merged_into`` more per duplicate folded, PLUS the deletions the apply really
   performs: ``MENTIONS − collapses`` (a ``(chunk, duplicate)`` edge whose chunk
   already mentions the canonical collapses under ``MERGE`` + ``DELETE``) and
   ``RELATED − intra_group_edges`` (``_DELETE_INTRA_GROUP_RELATED`` deletes every
   directed edge inside the merged group, counted per directed edge, no id
   ordering); the post-apply drift report exits non-zero on any mismatch
   (mirrors ``ledger rollback``).
4. **§7.2 + fingerprint gate ordering** — the refusals happen before the first
   write, proven by a recorder that must stay empty.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from book_graph_rag.domain.duplicate_grouping import (
    DuplicateMemberRow,
    PlannedMerge,
    plan_intra_resolution,
)
from book_graph_rag.infrastructure.neo4j_audit_adapter import (
    DUPLICATE_GROUP_KEY_EXPRESSION,
    QUERY_PLAN,
)

_ROOT = Path(__file__).parents[2]
_SCRIPT_PATH = _ROOT / "scripts-ops" / "resolve_intra_ns.py"
_ADAPTER_PATH = _ROOT / "src" / "book_graph_rag" / "infrastructure" / "neo4j_audit_adapter.py"
_SOURCE = _SCRIPT_PATH.read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)

_NS = "knowledge:ai-engineering-huyen"
_CANON = f"{_NS}:llm-concept"
_DUP = f"{_NS}:large-language-model-concept"
_FRAGMENT = "toLower(trim(n.name)) AS name"

_ROWS = [DuplicateMemberRow(name="llm", kind="concept", entity_ids=(_CANON, _DUP))]
#: (mentions, related degree): ``llm-concept`` is the richer member.
_SCORES = {_CANON: (7, 19), _DUP: (1, 4)}


def _load_script() -> ModuleType:
    """Import the ops script as a module (no CLI args, no graph, no Settings)."""
    spec = importlib.util.spec_from_file_location("resolve_intra_ns", _SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["resolve_intra_ns"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def script() -> ModuleType:
    return _load_script()


def _plan(script: ModuleType) -> list[PlannedMerge]:
    return plan_intra_resolution(_ROWS, scores=_SCORES)


# ── Fakes: Settings, driver/session and the write itself ──────────────────────


class _FakeSecret:
    def get_secret_value(self) -> str:
        return "not-a-real-secret"


class _FakeSettings:
    @classmethod
    def model_validate(cls, _data: object) -> SimpleNamespace:
        return SimpleNamespace(
            neo4j_uri="bolt://localhost:7687",
            neo4j_user="neo4j",
            neo4j_password=_FakeSecret(),
        )


class _FakeResult:
    def __init__(self, records: list[object]) -> None:
        self._records = records

    async def values(self) -> list[object]:
        return self._records

    async def single(self) -> object | None:
        return self._records[0] if self._records else None


class _FakeSession:
    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def run(self, _query: str, **_params: object) -> _FakeResult:
        return _FakeResult([])


class _FakeDriver:
    def session(self) -> _FakeSession:
        return _FakeSession()

    async def close(self) -> None:
        return None


def _install_graph_fakes(
    script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    *,
    census_reads: list[object],
    related_rows: list[tuple[str, str]] | None = None,
    mention_rows: list[tuple[str, str]] | None = None,
    repoint_rows: list[tuple[str, str, str | None]] | None = None,
) -> list[list[PlannedMerge]]:
    """Stub every graph touch; return the recorder of ``_apply`` invocations.

    ``related_rows`` / ``mention_rows`` feed the READ-ONLY row fetches; the
    per-directed-edge counting itself runs the real pure functions, which is
    exactly what the deletion-prediction tests exercise.
    """
    applied: list[list[PlannedMerge]] = []
    read_index = {"i": 0}

    async def _fetch_rows(_session: object, _prefix: str) -> list[DuplicateMemberRow]:
        return list(_ROWS)

    async def _edge_impact(_session: object, _ids: list[str]) -> dict[str, tuple[int, int]]:
        return dict(_SCORES)

    async def _related(_session: object, _member_ids: list[str]) -> list[tuple[str, str]]:
        return list(related_rows or [])

    async def _mentions(_session: object, _member_ids: list[str]) -> list[tuple[str, str]]:
        return list(mention_rows or [])

    async def _repoint(
        _session: object, _member_ids: list[str]
    ) -> list[tuple[str, str, str | None]]:
        return list(repoint_rows or [])

    async def _read_census(_session: object, _prefix: str) -> object:
        value = census_reads[read_index["i"]]
        read_index["i"] += 1
        return value

    async def _apply(
        plan: list[PlannedMerge],
        _settings: object,
        _approver: str,
        *,
        quiet: bool = False,
    ) -> int:
        applied.append(list(plan))
        return 0

    monkeypatch.setattr(script, "Settings", _FakeSettings)
    monkeypatch.setattr(
        script,
        "AsyncGraphDatabase",
        SimpleNamespace(driver=lambda *_args, **_kwargs: _FakeDriver()),
    )
    monkeypatch.setattr(script, "_fetch_rows", _fetch_rows)
    monkeypatch.setattr(script, "_edge_impact", _edge_impact)
    monkeypatch.setattr(script, "_intra_group_related_rows", _related)
    monkeypatch.setattr(script, "_member_mention_rows", _mentions)
    monkeypatch.setattr(script, "_related_repoint_rows", _repoint)
    monkeypatch.setattr(script, "_read_census", _read_census)
    monkeypatch.setattr(script, "_apply", _apply)
    return applied


def _census(script: ModuleType, active: int, merged: int, mentions: int, related: int) -> object:
    return script.Census(
        active_entities=active,
        merged_into_count=merged,
        total_mentions=mentions,
        total_related=related,
    )


def _gate_files(tmp_path: Path) -> tuple[Path, Path]:
    approval = tmp_path / "approve.txt"
    approval.write_text("approve\n", encoding="utf-8")
    backup = tmp_path / "backup.json"
    backup.write_text("{}", encoding="utf-8")
    return approval, backup


def _apply_args(script: ModuleType, tmp_path: Path, fingerprint: str | None) -> list[str]:
    approval, backup = _gate_files(tmp_path)
    argv = ["--apply", "--approval", str(approval), "--backup", str(backup)]
    if fingerprint is not None:
        argv.extend(["--expect-fingerprint", fingerprint])
    return argv


# ── T10-A: grouping parity through ONE shared constant ───────────────────────


def test_grouping_key_expression_is_present_in_both_sources(script: ModuleType) -> None:
    rule_query = dict(QUERY_PLAN)["duplicates_entity"]
    script_query = script._GROUPS_QUERY

    assert DUPLICATE_GROUP_KEY_EXPRESSION in rule_query
    assert DUPLICATE_GROUP_KEY_EXPRESSION in script_query
    assert _FRAGMENT in rule_query
    assert _FRAGMENT in script_query
    # imported, never copied: the n-alias literal survives only in the constant.
    assert _FRAGMENT not in _SOURCE
    assert "toLower(trim(" not in _SOURCE


def test_both_queries_are_built_from_the_imported_constant(script: ModuleType) -> None:
    adapter_tree = ast.parse(_ADAPTER_PATH.read_text(encoding="utf-8"))

    assert _references(
        _rule_query_node(adapter_tree, "duplicates_entity"),
        "DUPLICATE_GROUP_KEY_EXPRESSION",
    )
    assert _references(_assign_node(_TREE, "_GROUPS_QUERY"), "DUPLICATE_GROUP_KEY_EXPRESSION")
    # The deletion counters do NOT re-group: they fetch rows for the member ids of
    # the PLAN built from _GROUPS_QUERY above (parity inherited by construction),
    # with NO id-ordering filter so every directed intra-group edge is counted.
    related_fetch = str(script._INTRA_GROUP_RELATED_QUERY)
    assert "$member_ids" in related_fetch
    assert "STARTS WITH" not in related_fetch
    assert "a.id <" not in related_fetch
    assert "< b.id" not in related_fetch
    assert "$member_ids" in str(script._MEMBER_MENTIONS_QUERY)
    assert "DUPLICATE_GROUP_KEY_EXPRESSION" in _imported_from(
        _TREE, "book_graph_rag.infrastructure.neo4j_audit_adapter"
    )


def _rule_query_node(tree: ast.AST, rule: str) -> ast.AST:
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Tuple)
            and node.elts
            and isinstance(node.elts[0], ast.Constant)
            and node.elts[0].value == rule
            and len(node.elts) == 2
        ):
            return node.elts[1]
    raise AssertionError(f"query {rule!r} not found in the AST")


def _assign_node(tree: ast.AST, name: str) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return node.value
    raise AssertionError(f"assignment {name!r} not found in the AST")


def _references(node: ast.AST, name: str) -> bool:
    return any(isinstance(child, ast.Name) and child.id == name for child in ast.walk(node))


def _imported_from(tree: ast.AST, module: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == module:
            names.update(alias.name for alias in node.names)
    return names


# ── T10-C: fingerprint ───────────────────────────────────────────────────────


def test_plan_fingerprint_is_deterministic(script: ModuleType) -> None:
    first = script.plan_fingerprint(_plan(script))
    second = script.plan_fingerprint(plan_intra_resolution(_ROWS, scores=_SCORES))

    assert first == second
    assert re.fullmatch(r"[0-9a-f]{64}", first)


def test_plan_fingerprint_changes_when_any_plan_field_changes(script: ModuleType) -> None:
    base_plan = [
        PlannedMerge(name="llm", kind="concept", canonical_id=_CANON, duplicate_ids=(_DUP,))
    ]
    base = script.plan_fingerprint(base_plan)
    variants: dict[str, list[PlannedMerge]] = {
        # namespace derives from the canonical id, so it is covered by construction:
        "namespace": [
            PlannedMerge(
                name="llm",
                kind="concept",
                canonical_id="knowledge:other-source:llm-concept",
                duplicate_ids=("knowledge:other-source:large-language-model-concept",),
            )
        ],
        "name": [
            PlannedMerge(name="large", kind="concept", canonical_id=_CANON, duplicate_ids=(_DUP,))
        ],
        "kind": [
            PlannedMerge(name="llm", kind="pattern", canonical_id=_CANON, duplicate_ids=(_DUP,))
        ],
        "canonical_id": [
            PlannedMerge(name="llm", kind="concept", canonical_id=_DUP, duplicate_ids=(_CANON,))
        ],
        "duplicate_ids": [
            PlannedMerge(
                name="llm",
                kind="concept",
                canonical_id=_CANON,
                duplicate_ids=(_DUP, f"{_NS}:modelo-de-lenguaje-grande-concept"),
            )
        ],
    }

    for field, plan in variants.items():
        assert script.plan_fingerprint(plan) != base, f"{field} must be covered by the fingerprint"


async def test_dry_run_prints_the_fingerprint(
    script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_graph_fakes(
        script,
        monkeypatch,
        census_reads=[
            _census(script, 100, 10, 500, 700),
            _census(script, 1000, 50, 5000, 7000),
        ],
    )

    result = await script.main([])

    out = capsys.readouterr().out
    assert result is None
    assert script.plan_fingerprint(_plan(script)) in out
    assert "DRY-RUN" in out
    assert "--expect-fingerprint" in out


async def test_json_output_carries_the_fingerprint_and_predicted_delta(
    script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_graph_fakes(
        script,
        monkeypatch,
        census_reads=[
            _census(script, 100, 10, 500, 700),
            _census(script, 1000, 50, 5000, 7000),
        ],
    )

    await script.main(["--json"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["fingerprint"] == script.plan_fingerprint(_plan(script))
    assert payload["predicted_delta"] == {
        "active_entities": -1,
        "merged_into_count": 1,
        "total_mentions": 0,
        "total_related": 0,
    }
    assert payload["groups"][0]["canonical_id"] == _CANON


async def test_apply_without_expect_fingerprint_refuses_before_any_write(
    script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    applied = _install_graph_fakes(
        script,
        monkeypatch,
        census_reads=[
            _census(script, 100, 10, 500, 700),
            _census(script, 1000, 50, 5000, 7000),
        ],
    )

    with pytest.raises(SystemExit) as excinfo:
        await script.main(_apply_args(script, tmp_path, fingerprint=None))

    assert "expect-fingerprint" in str(excinfo.value)
    assert applied == []


async def test_apply_with_a_wrong_fingerprint_refuses_before_any_write(
    script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    applied = _install_graph_fakes(
        script,
        monkeypatch,
        census_reads=[
            _census(script, 100, 10, 500, 700),
            _census(script, 1000, 50, 5000, 7000),
        ],
    )
    reviewed = "0" * 64

    with pytest.raises(SystemExit) as excinfo:
        await script.main(_apply_args(script, tmp_path, fingerprint=reviewed))

    message = str(excinfo.value)
    assert "FINGERPRINT" in message
    assert reviewed in message
    assert script.plan_fingerprint(_plan(script)) in message
    assert applied == []


async def test_apply_with_the_reviewed_fingerprint_runs_and_reports_no_drift(
    script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fingerprint = script.plan_fingerprint(_plan(script))
    applied = _install_graph_fakes(
        script,
        monkeypatch,
        census_reads=[
            _census(script, 100, 10, 500, 700),
            _census(script, 1000, 50, 5000, 7000),
            _census(script, 99, 11, 500, 700),
            _census(script, 999, 51, 5000, 7000),
        ],
    )

    with pytest.raises(SystemExit) as excinfo:
        await script.main(_apply_args(script, tmp_path, fingerprint=fingerprint))

    out = capsys.readouterr().out
    assert excinfo.value.code == 0
    assert len(applied) == 1
    assert "APPLY_OK" in out
    assert "census drift: none" in out
    assert fingerprint in out


async def test_apply_json_emits_one_parseable_payload(
    script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No stray print may corrupt the JSON: gate/progress output is suppressed."""
    fingerprint = script.plan_fingerprint(_plan(script))
    _install_graph_fakes(
        script,
        monkeypatch,
        census_reads=[
            _census(script, 100, 10, 500, 700),
            _census(script, 1000, 50, 5000, 7000),
            _census(script, 99, 11, 500, 700),
            _census(script, 999, 51, 5000, 7000),
        ],
    )

    with pytest.raises(SystemExit) as excinfo:
        await script.main(_apply_args(script, tmp_path, fingerprint=fingerprint) + ["--json"])

    payload = json.loads(capsys.readouterr().out)
    assert excinfo.value.code == 0
    assert payload["fingerprint"] == fingerprint
    assert payload["drift"] == []
    assert payload["applied_exit_code"] == 0
    assert payload["census_after"]["namespace"]["active_entities"] == 99


async def test_post_apply_census_drift_exits_non_zero(
    script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fingerprint = script.plan_fingerprint(_plan(script))
    applied = _install_graph_fakes(
        script,
        monkeypatch,
        census_reads=[
            _census(script, 100, 10, 500, 700),
            _census(script, 1000, 50, 5000, 7000),
            # active entities did NOT drop and MENTIONS moved: two drifts.
            _census(script, 100, 11, 499, 700),
            _census(script, 1000, 51, 5000, 7000),
        ],
    )

    with pytest.raises(SystemExit) as excinfo:
        await script.main(_apply_args(script, tmp_path, fingerprint=fingerprint))

    out = capsys.readouterr().out
    assert excinfo.value.code == 1
    assert len(applied) == 1  # the write happened; the drift is reported, not hidden
    assert "DRIFT" in out
    assert "APPLY_OK" not in out


# ── T10-D: census arithmetic ─────────────────────────────────────────────────


def test_predicted_census_delta_folds_one_duplicate_per_entity(script: ModuleType) -> None:
    plan = [
        PlannedMerge(
            name="a",
            kind="concept",
            canonical_id=f"{_NS}:a-concept",
            duplicate_ids=(f"{_NS}:a2-concept",),
        ),
        PlannedMerge(
            name="b",
            kind="concept",
            canonical_id=f"{_NS}:b-concept",
            duplicate_ids=(f"{_NS}:b2-concept",),
        ),
        PlannedMerge(
            name="c",
            kind="concept",
            canonical_id=f"{_NS}:c-concept",
            duplicate_ids=(f"{_NS}:c2-concept", f"{_NS}:c3-concept"),
        ),
    ]

    delta = script.predicted_census_delta(plan)

    assert delta.active_entities == -4
    assert delta.merged_into_count == 4
    assert delta.total_mentions == 0
    assert delta.total_related == 0


def test_census_drift_is_empty_only_when_the_measurement_matches(
    script: ModuleType,
) -> None:
    plan = [
        PlannedMerge(
            name="a",
            kind="concept",
            canonical_id=f"{_NS}:a-concept",
            duplicate_ids=(f"{_NS}:a2-concept",),
        )
    ]
    before = _census(script, 100, 10, 500, 700)
    delta = script.predicted_census_delta(plan)

    assert script.census_drift(before, _census(script, 99, 11, 500, 700), delta) == []
    assert script.census_drift(before, _census(script, 99, 11, 501, 700), delta)


def test_census_drift_names_every_mismatched_field(script: ModuleType) -> None:
    plan = [
        PlannedMerge(
            name="a",
            kind="concept",
            canonical_id=f"{_NS}:a-concept",
            duplicate_ids=(f"{_NS}:a2-concept",),
        )
    ]
    before = _census(script, 100, 10, 500, 700)
    delta = script.predicted_census_delta(plan)

    drift = script.census_drift(before, _census(script, 98, 12, 499, 701), delta)

    assert len(drift) == 4
    assert any("activas" in line for line in drift)
    assert any("merged_into" in line for line in drift)
    assert any("MENTIONS" in line for line in drift)
    assert any("RELATED" in line for line in drift)


# ── T10-D: predicted deletions (MENTIONS collapses + intra-group RELATED) ────


def test_predicted_census_delta_subtracts_the_deletions_the_apply_performs(
    script: ModuleType,
) -> None:
    """``MENTIONS − collapses`` and ``RELATED − intra_group_edges``, not invariance."""
    plan = [
        PlannedMerge(
            name="a",
            kind="concept",
            canonical_id=f"{_NS}:a-concept",
            duplicate_ids=(f"{_NS}:a2-concept",),
        )
    ]

    delta = script.predicted_census_delta(plan, collapses=3, intra_group_edges=2)

    assert delta.active_entities == -1
    assert delta.merged_into_count == 1
    assert delta.total_mentions == -3
    assert delta.total_related == -2


def test_intra_group_related_counts_per_directed_edge(script: ModuleType) -> None:
    """Every edge with both endpoints in the plan group is deleted by the apply.

    ``_DELETE_INTRA_GROUP_RELATED`` has NO ``id`` ordering filter, so neither may
    the counter: a bidirectional pair is two directed edges (2) and a single edge
    whose SOURCE id sorts HIGHER than its target still counts 1 — the old
    ``a.id < b.id`` guard dropped exactly that edge.
    """
    plan = _plan(script)
    outside = f"{_NS}:some-other-concept"

    bidirectional = [(_CANON, _DUP), (_DUP, _CANON)]
    assert script.count_intra_group_related(plan, bidirectional) == 2
    # _CANON (llm-...) sorts higher than _DUP (large-...): the single-edge case
    # the ordering filter used to drop.
    assert script.count_intra_group_related(plan, [(_CANON, _DUP)]) == 1
    assert script.count_intra_group_related(plan, [(_DUP, _CANON)]) == 1
    # an edge touching an entity outside the plan group is re-pointed, not deleted.
    assert script.count_intra_group_related(plan, [(_CANON, outside)]) == 0


def test_mentions_collapse_count_covers_chunks_that_already_mention_the_canonical(
    script: ModuleType,
) -> None:
    """A ``(chunk, duplicate)`` edge collapses only if the chunk mentions the canonical.

    ``_REPOINT_MENTIONS_BATCH`` does ``MERGE`` (no new edge when one exists) then
    ``DELETE`` the duplicate's edge: -1 total. A chunk that mentions only the
    duplicate gets a NEW canonical edge first: net 0, no collapse.
    """
    plan = _plan(script)

    assert script.count_mentions_collapses(plan, [("chunk-1", _DUP), ("chunk-1", _CANON)]) == 1
    assert script.count_mentions_collapses(plan, [("chunk-2", _DUP)]) == 0
    assert script.count_mentions_collapses(plan, [("chunk-3", _CANON)]) == 0
    # a chunk mentioning a duplicate of another group is irrelevant here.
    assert script.count_mentions_collapses(plan, [("chunk-4", f"{_NS}:other-concept")]) == 0


async def test_dry_run_reports_the_predicted_deletions(
    script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_graph_fakes(
        script,
        monkeypatch,
        census_reads=[
            _census(script, 100, 10, 500, 700),
            _census(script, 1000, 50, 5000, 7000),
        ],
        related_rows=[(_CANON, _DUP)],
        mention_rows=[("chunk-1", _DUP), ("chunk-1", _CANON)],
    )

    await script.main([])

    out = capsys.readouterr().out
    assert "MENTIONS a colapsar: 1" in out
    assert "RELATED intra-grupo a borrar: 1" in out
    assert "MENTIONS -1" in out
    assert "RELATED -1" in out
    assert "invariante" not in out


async def test_json_output_carries_the_predicted_deletions(
    script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_graph_fakes(
        script,
        monkeypatch,
        census_reads=[
            _census(script, 100, 10, 500, 700),
            _census(script, 1000, 50, 5000, 7000),
        ],
        related_rows=[(_CANON, _DUP), (_DUP, _CANON)],
        mention_rows=[("chunk-1", _DUP), ("chunk-1", _CANON)],
    )

    await script.main(["--json"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["mentions_collapses"] == 1
    assert payload["intra_group_related"] == 2
    assert payload["predicted_delta"]["total_mentions"] == -1
    assert payload["predicted_delta"]["total_related"] == -2


async def test_apply_with_intra_group_edge_and_collapse_exits_without_drift(
    script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A correct apply measuring its own deletions must NOT report DRIFT.

    The census after the apply really drops by 1 MENTIONS (collapse) and 1
    RELATED (intra-group edge): predicting invariance used to exit 1 with a DRIFT
    line that an operator could misread as a failure.
    """
    fingerprint = script.plan_fingerprint(_plan(script))
    _install_graph_fakes(
        script,
        monkeypatch,
        census_reads=[
            _census(script, 100, 10, 500, 700),
            _census(script, 1000, 50, 5000, 7000),
            _census(script, 99, 11, 499, 699),
            _census(script, 999, 51, 4999, 6999),
        ],
        related_rows=[(_CANON, _DUP)],
        mention_rows=[("chunk-1", _DUP), ("chunk-1", _CANON)],
    )

    with pytest.raises(SystemExit) as excinfo:
        await script.main(_apply_args(script, tmp_path, fingerprint=fingerprint))

    out = capsys.readouterr().out
    assert excinfo.value.code == 0
    assert "APPLY_OK" in out
    assert "census drift: none" in out
    assert "DRIFT" not in out


async def test_prediction_gate_still_fires_when_a_measurement_mismatches(
    script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Triangulation: predicting the deletions does NOT loosen the gate.

    The prediction says RELATED drops by 1 (intra-group edge); if the
    post-apply census shows it did not, the run must still exit 1 with DRIFT.
    """
    fingerprint = script.plan_fingerprint(_plan(script))
    _install_graph_fakes(
        script,
        monkeypatch,
        census_reads=[
            _census(script, 100, 10, 500, 700),
            _census(script, 1000, 50, 5000, 7000),
            # RELATED unchanged although the intra-group edge should have gone.
            _census(script, 99, 11, 499, 700),
            _census(script, 999, 51, 4999, 7000),
        ],
        related_rows=[(_CANON, _DUP)],
        mention_rows=[("chunk-1", _DUP), ("chunk-1", _CANON)],
    )

    with pytest.raises(SystemExit) as excinfo:
        await script.main(_apply_args(script, tmp_path, fingerprint=fingerprint))

    out = capsys.readouterr().out
    assert excinfo.value.code == 1
    assert "DRIFT" in out
    assert "RELATED" in out


# ── Re-point collapses (defect: DRIFT fired on a CORRECT apply) ──────────────
#
# ``_REPOINT_RELATED_{OUT,IN}_BATCH`` re-point each captured member edge with
# ``MERGE (canon)-[r2:RELATED {type: r.type}]->(other)`` (and the symmetric in
# direction) BEFORE deleting the original, so for every
# (group, direction, other, type) key the survivor is 1 when
# ``existing + incoming >= 1`` and the loss is ``max(0, existing + incoming - 1)``.
# ``existing`` is the canonical's own pre-merge edge (the adapter only captures
# ``duplicate_ids``); ``incoming`` are the captured member edges on that same
# key. Member edges whose other endpoint is inside the group are skipped by the
# adapter's ``WHERE`` and deleted by ``_DELETE_INTRA_GROUP_RELATED`` — counted
# by ``count_intra_group_related``, NEVER twice.

_OUTSIDE = f"{_NS}:some-other-concept"
_REFERS = "refers_to"
_THIRD = f"{_NS}:llm-concept-also"
_PLAN3 = [
    PlannedMerge(name="llm", kind="concept", canonical_id=_CANON, duplicate_ids=(_DUP, _THIRD))
]


def test_related_repoint_collapses_follow_max_zero_existing_plus_incoming(
    script: ModuleType,
) -> None:
    plan = _plan(script)

    # canonical only, no member edge: nothing is captured, nothing collapses.
    assert script.count_related_repoint_collapses(plan, [(_CANON, _OUTSIDE, _REFERS)]) == 0
    # canonical already has the key and one member adds it: 1 + 1 - 1 = 1.
    assert (
        script.count_related_repoint_collapses(
            plan, [(_CANON, _OUTSIDE, _REFERS), (_DUP, _OUTSIDE, _REFERS)]
        )
        == 1
    )
    # the symmetric in direction collapses independently: 1 + 1 - 1 = 1.
    assert (
        script.count_related_repoint_collapses(
            plan, [(_OUTSIDE, _CANON, _REFERS), (_OUTSIDE, _DUP, _REFERS)]
        )
        == 1
    )
    # out-exists + in-from-member are DIFFERENT keys: no collapse.
    assert (
        script.count_related_repoint_collapses(
            plan, [(_CANON, _OUTSIDE, _REFERS), (_OUTSIDE, _DUP, _REFERS)]
        )
        == 0
    )
    # ``MERGE ... {type: r.type}``: a different type is a different key.
    assert (
        script.count_related_repoint_collapses(
            plan, [(_CANON, _OUTSIDE, _REFERS), (_DUP, _OUTSIDE, "related_to")]
        )
        == 0
    )
    # both endpoints in the group: skipped by the adapter WHERE and deleted by
    # _DELETE_INTRA_GROUP_RELATED — the intra-group counter owns that population.
    assert script.count_related_repoint_collapses(plan, [(_CANON, _DUP, _REFERS)]) == 0
    # edges between entities outside the plan groups are irrelevant.
    assert (
        script.count_related_repoint_collapses(
            plan, [("knowledge:other:x", "knowledge:other:y", _REFERS)]
        )
        == 0
    )
    # the adapter's ``WHERE r.type = inv.edge_properties.type`` never matches a
    # NULL type: such an edge is NOT re-pointed, so it cannot collapse.
    assert (
        script.count_related_repoint_collapses(
            plan, [(_CANON, _OUTSIDE, None), (_DUP, _OUTSIDE, None)]
        )
        == 0
    )
    assert (
        script.count_related_repoint_collapses(
            plan, [(_CANON, _OUTSIDE, _REFERS), (_DUP, _OUTSIDE, None)]
        )
        == 0
    )


def test_two_members_sharing_one_key_collapse_to_a_single_survivor(
    script: ModuleType,
) -> None:
    """``incoming = 2`` against a canonical with or without its own edge."""
    # canonical has none: 0 + 2 - 1 = 1 survivor → loss 1.
    assert (
        script.count_related_repoint_collapses(
            _PLAN3, [(_DUP, _OUTSIDE, _REFERS), (_THIRD, _OUTSIDE, _REFERS)]
        )
        == 1
    )
    # canonical already has it: 1 + 2 → loss 2 (all three collapse onto one).
    assert (
        script.count_related_repoint_collapses(
            _PLAN3,
            [
                (_CANON, _OUTSIDE, _REFERS),
                (_DUP, _OUTSIDE, _REFERS),
                (_THIRD, _OUTSIDE, _REFERS),
            ],
        )
        == 2
    )


def test_predicted_census_delta_subtracts_all_three_loss_components(
    script: ModuleType,
) -> None:
    delta = script.predicted_census_delta(
        _plan(script), collapses=3, intra_group_edges=2, repoint_collapses=4
    )

    assert delta.active_entities == -1
    assert delta.merged_into_count == 1
    assert delta.total_mentions == -3
    assert delta.total_related == -6


def test_mentions_collapse_counts_one_per_chunk_touching_the_group(
    script: ModuleType,
) -> None:
    """Every chunk mentioning ANY member collapses onto one canonical edge."""
    # a chunk mentioning BOTH members of a size-2 group: 2 → 1 survivor = 1.
    assert (
        script.count_mentions_collapses(_plan(script), [("chunk-1", _CANON), ("chunk-1", _DUP)])
        == 1
    )
    # canonical only / single member only: no collapse.
    assert script.count_mentions_collapses(_plan(script), [("chunk-2", _CANON)]) == 0
    assert script.count_mentions_collapses(_plan(script), [("chunk-3", _DUP)]) == 0
    # general case, size-3 group: chunk mentions the two members but NOT the
    # canonical — the old counter (canonical-anchored) missed exactly this.
    assert script.count_mentions_collapses(_PLAN3, [("chunk-4", _DUP), ("chunk-4", _THIRD)]) == 1
    # chunk mentioning all three members: 3 → 1 survivor = 2.
    assert (
        script.count_mentions_collapses(
            _PLAN3, [("chunk-5", _CANON), ("chunk-5", _DUP), ("chunk-5", _THIRD)]
        )
        == 2
    )
    # a chunk is counted per group it touches; entities outside the plan are ignored.
    assert (
        script.count_mentions_collapses(
            _PLAN3, [("chunk-6", _DUP), ("chunk-6", _THIRD), ("chunk-6", _OUTSIDE)]
        )
        == 1
    )


async def test_dry_run_reports_the_repoint_collapse_component(
    script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_graph_fakes(
        script,
        monkeypatch,
        census_reads=[
            _census(script, 100, 10, 500, 700),
            _census(script, 1000, 50, 5000, 7000),
        ],
        repoint_rows=[(_CANON, _OUTSIDE, _REFERS), (_DUP, _OUTSIDE, _REFERS)],
    )

    await script.main([])

    out = capsys.readouterr().out
    assert "RELATED re-point a colapsar: 1" in out
    assert "RELATED -1" in out


async def test_json_carries_the_repoint_collapse_component(
    script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_graph_fakes(
        script,
        monkeypatch,
        census_reads=[
            _census(script, 100, 10, 500, 700),
            _census(script, 1000, 50, 5000, 7000),
        ],
        related_rows=[],
        repoint_rows=[(_CANON, _OUTSIDE, _REFERS), (_DUP, _OUTSIDE, _REFERS)],
    )

    await script.main(["--json"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["related_repoint_collapses"] == 1
    assert payload["intra_group_related"] == 0
    assert payload["mentions_collapses"] == 0
    assert payload["predicted_delta"]["total_related"] == -1


async def test_apply_with_repoint_collapse_and_matching_measurement_exits_without_drift(
    script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """PRODUCTION CASE: a shared neighbour receives two parallel edges.

    The canonical already had ``canon -> outside`` and the duplicate adds
    ``dup -> outside``: the re-point MERGEs onto the existing edge and deletes
    the member's, so RELATED really drops by 1 with ZERO intra-group edges.
    Predicting invariance made the drift gate exit 1 on a CORRECT apply.
    """
    fingerprint = script.plan_fingerprint(_plan(script))
    _install_graph_fakes(
        script,
        monkeypatch,
        census_reads=[
            _census(script, 100, 10, 500, 700),
            _census(script, 1000, 50, 5000, 7000),
            _census(script, 99, 11, 500, 699),
            _census(script, 999, 51, 5000, 6999),
        ],
        related_rows=[],
        repoint_rows=[(_CANON, _OUTSIDE, _REFERS), (_DUP, _OUTSIDE, _REFERS)],
    )

    with pytest.raises(SystemExit) as excinfo:
        await script.main(_apply_args(script, tmp_path, fingerprint=fingerprint))

    out = capsys.readouterr().out
    assert excinfo.value.code == 0
    assert "APPLY_OK" in out
    assert "census drift: none" in out
    assert "DRIFT" not in out
    assert "RELATED -1" in out


async def test_repoint_prediction_gate_still_fires_when_the_measurement_mismatches(
    script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Triangulation: modelling the collapses must NOT loosen the drift gate."""
    fingerprint = script.plan_fingerprint(_plan(script))
    _install_graph_fakes(
        script,
        monkeypatch,
        census_reads=[
            _census(script, 100, 10, 500, 700),
            _census(script, 1000, 50, 5000, 7000),
            # RELATED unchanged although the re-point collapse should have happened.
            _census(script, 99, 11, 500, 700),
            _census(script, 999, 51, 5000, 7000),
        ],
        related_rows=[],
        repoint_rows=[(_CANON, _OUTSIDE, _REFERS), (_DUP, _OUTSIDE, _REFERS)],
    )

    with pytest.raises(SystemExit) as excinfo:
        await script.main(_apply_args(script, tmp_path, fingerprint=fingerprint))

    out = capsys.readouterr().out
    assert excinfo.value.code == 1
    assert "DRIFT" in out
    assert "RELATED" in out
