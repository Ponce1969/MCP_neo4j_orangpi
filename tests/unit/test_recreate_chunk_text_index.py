"""Tests for ``scripts-ops/recreate_chunk_text_index.py`` (S5 of the phase-2 task).

The recreation is a gated DDL operation: dry-run by default, ``--apply`` needs
an approval file containing ``approve`` and an ``--expect-fingerprint`` that
ties the mutation to the reviewed plan (same contract as
``scripts-ops/resolve_intra_ns.py``). Nothing here touches a graph.

The five independent-verification defects are pinned here: the declared
unavailability window (text output, dry-run and apply), the failed-CREATE
recovery path, the analyzer post-condition read back from ``SHOW INDEXES``,
the rollback protocol documented in the docstring, and the single-source DDL
derivation from the adapter (plus the ``neo4j_uri`` fingerprint binding).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_ROOT = Path(__file__).parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "recreate_chunk_text_index",
    _ROOT / "scripts-ops/recreate_chunk_text_index.py",
)
assert _SPEC is not None
assert _SPEC.loader is not None
_MODULE: ModuleType = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)

create_statement = _MODULE.create_statement
build_plan = _MODULE.build_plan
plan_fingerprint = _MODULE.plan_fingerprint
parse_args = _MODULE._parse_args
validate_apply_gates = _MODULE.validate_apply_gates
DECLARED_ANALYZER: str = _MODULE.DECLARED_ANALYZER

_STATE_ONLINE: dict[str, Any] = {
    "index_state": "ONLINE (population=100.0%, analyzer=standard-no-stop-words)",
    "state": "ONLINE",
    "population": 100.0,
    "analyzer": "standard-no-stop-words",
}
_STATE_ABSENT: dict[str, Any] = {
    "index_state": "absent",
    "state": None,
    "population": None,
    "analyzer": None,
}
_CHUNK_COUNT = 10


class _StubSettings:
    """Minimal ``Settings`` stand-in so ``main`` runs without env or secrets."""

    neo4j_uri: str = "bolt://fake:7687"

    @classmethod
    def model_validate(cls, data: dict[str, Any]) -> _StubSettings:
        return cls()


class _StubAdapter:
    async def close(self) -> None:
        return None


def _stub_adapter_factory(_settings: Any) -> _StubAdapter:
    return _StubAdapter()


class _FakeReadAdapter:
    """Duck-typed ``execute_read`` feeding ``_read_state`` with canned rows."""

    def __init__(self, index_rows: list[dict[str, Any]], chunk_count: int) -> None:
        self._index_rows = index_rows
        self._chunk_count = chunk_count

    async def execute_read(
        self, cypher: str, parameters: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        if "SHOW INDEXES" in cypher:
            return self._index_rows
        return [{"chunks": self._chunk_count}]


def _wire_main(
    monkeypatch: pytest.MonkeyPatch,
    read_results: list[tuple[dict[str, Any], int]],
    *,
    apply_exc: Exception | None = None,
) -> dict[str, int]:
    """Stub ``main``'s I/O: canned state reads, fake apply, no real Settings.

    ``read_results`` is a queue (last entry repeats); reset ``"reads"`` between
    two ``main`` invocations when each run must start from the first entry.
    """
    counters = {"reads": 0, "applies": 0}

    async def fake_read_state(*args: Any, **kwargs: Any) -> tuple[dict[str, Any], int]:
        index = min(counters["reads"], len(read_results) - 1)
        counters["reads"] += 1
        return read_results[index]

    async def fake_apply(*args: Any, **kwargs: Any) -> None:
        counters["applies"] += 1
        if apply_exc is not None:
            raise apply_exc

    monkeypatch.setattr(_MODULE, "Settings", _StubSettings)
    monkeypatch.setattr(_MODULE, "Neo4jQueryAdapter", _stub_adapter_factory)
    monkeypatch.setattr(_MODULE, "_read_state", fake_read_state)
    monkeypatch.setattr(_MODULE, "_apply", fake_apply)
    return counters


def _approval(tmp_path: Path) -> Path:
    path = tmp_path / "approval.txt"
    path.write_text("approve", encoding="utf-8")
    return path


async def _fingerprint_from_dry_run(
    counters: dict[str, int], capsys: pytest.CaptureFixture[str]
) -> str:
    """Mint the reviewed fingerprint from a ``--json`` dry-run, then rewind."""
    rc = await _MODULE.main(["--json"])
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out[: out.rfind("}") + 1])
    counters["reads"] = 0
    return str(payload["fingerprint"])


def test_create_statement_is_byte_identical_to_ensure_indexes_ddl() -> None:
    """One literal, two owners: the script and the adapter must never drift.

    ``ensure_indexes`` pins the same string in
    ``tests/test_neo4j_query_adapter.py``; if either side changes alone, one
    of the two tests fails — which is what "the repository owns the analyzer"
    means concretely (S5a).
    """
    expected = (
        "CREATE FULLTEXT INDEX chunk_text_index IF NOT EXISTS FOR (n:Chunk) ON EACH [n.text] "
        "OPTIONS {indexConfig: {`fulltext.analyzer`: 'standard-no-stop-words'}}"
    )

    assert DECLARED_ANALYZER == "standard-no-stop-words"
    assert create_statement(DECLARED_ANALYZER) == expected


def test_create_statement_delegates_to_the_adapter_ddl(monkeypatch: pytest.MonkeyPatch) -> None:
    """The script holds no copy of the DDL: it must *call* the adapter's one.

    Re-pointing the adapter function must re-point the script's statement; a
    duplicated literal in the script would ignore the sentinel and fail here.
    """

    def _sentinel(analyzer: str) -> str:
        return f"SENTINEL:{analyzer}"

    monkeypatch.setattr(_MODULE, "chunk_text_index_ddl", _sentinel)
    assert _MODULE.create_statement("simple") == "SENTINEL:simple"


def test_create_statement_is_byte_identical_to_the_adapter_function() -> None:
    """Both entry points (adapter function, script wrapper) issue the same bytes."""
    from book_graph_rag.infrastructure.neo4j_query_adapter import chunk_text_index_ddl

    assert create_statement(DECLARED_ANALYZER) == chunk_text_index_ddl(DECLARED_ANALYZER)
    assert create_statement("simple") == chunk_text_index_ddl("simple")


def test_create_statement_rejects_an_analyzer_that_is_not_an_identifier() -> None:
    """The analyzer lands inside DDL text: only ``[a-z0-9-]+`` may pass."""
    with pytest.raises(ValueError, match="analyzer"):
        create_statement("standard; DROP INDEX entity_id")


async def test_index_state_query_projects_options_and_read_state_parses_the_analyzer() -> None:
    """``SHOW INDEXES`` must yield ``options`` so the analyzer is verifiable."""
    assert "options" in _MODULE._INDEX_STATE_CYPHER

    fake = _FakeReadAdapter(
        index_rows=[
            {
                "type": "FULLTEXT",
                "state": "ONLINE",
                "populationPercent": 100.0,
                "options": {
                    "indexProvider": "fulltext-1.0",
                    "indexConfig": {
                        "fulltext.analyzer": "standard-no-stop-words",
                        "fulltext.eventually_consistent": False,
                    },
                },
            }
        ],
        chunk_count=42,
    )
    info, count = await _MODULE._read_state(fake)

    assert info["analyzer"] == "standard-no-stop-words"
    assert info["state"] == "ONLINE"
    assert info["index_state"] == "ONLINE (population=100.0%, analyzer=standard-no-stop-words)"
    assert count == 42


async def test_read_state_reports_absent_when_the_index_is_gone() -> None:
    """No row ⇒ the full structured state, not just a display string."""
    fake = _FakeReadAdapter(index_rows=[], chunk_count=7)

    info, count = await _MODULE._read_state(fake)

    assert info == {
        "index_state": "absent",
        "state": None,
        "population": None,
        "analyzer": None,
    }
    assert count == 7


def test_population_at_100_accepts_both_neo4j_spellings() -> None:
    """Neo4j reports ``100.0`` or ``100``; neither may false-alarm the gate."""
    assert _MODULE._population_at_100(100.0) is True
    assert _MODULE._population_at_100(100) is True
    assert _MODULE._population_at_100("100") is True
    assert _MODULE._population_at_100(99.99999) is False
    assert _MODULE._population_at_100(None) is False
    assert _MODULE._population_at_100("full") is False


def test_plan_fingerprint_is_deterministic_and_content_addressed() -> None:
    """Same plan ⇒ same hash; any field the mutation depends on ⇒ different hash."""
    base = {
        "analyzer": DECLARED_ANALYZER,
        "chunk_count": 3468,
        "index_state": "absent",
        "neo4j_uri": "bolt://localhost:7687",
    }
    plan_a = build_plan(**base)
    plan_b = build_plan(**base)
    plan_other_count = build_plan(
        analyzer=DECLARED_ANALYZER,
        chunk_count=3469,
        index_state="absent",
        neo4j_uri="bolt://localhost:7687",
    )
    plan_other_analyzer = build_plan(
        analyzer="simple",
        chunk_count=3468,
        index_state="absent",
        neo4j_uri="bolt://localhost:7687",
    )

    assert plan_fingerprint(plan_a) == plan_fingerprint(plan_b)
    assert plan_fingerprint(plan_a) != plan_fingerprint(plan_other_count)
    assert plan_fingerprint(plan_a) != plan_fingerprint(plan_other_analyzer)


def test_plan_fingerprint_binds_the_neo4j_uri() -> None:
    """A dev-minted fingerprint must never validate a prod apply (or vice versa)."""
    dev = build_plan(
        analyzer=DECLARED_ANALYZER,
        chunk_count=3468,
        index_state="absent",
        neo4j_uri="bolt://dev:7687",
    )
    prod = build_plan(
        analyzer=DECLARED_ANALYZER,
        chunk_count=3468,
        index_state="absent",
        neo4j_uri="bolt://prod:7687",
    )

    assert plan_fingerprint(dev) != plan_fingerprint(prod)


def test_dry_run_is_the_default() -> None:
    """No flags ⇒ read-only. A mutating run must ask for it explicitly."""
    args = parse_args([])

    assert args.apply is False


def test_apply_requires_an_approval_file_containing_approve(tmp_path: Path) -> None:
    """The gated-operation word gate, mirrored from resolve_intra_ns."""
    args = argparse.Namespace(approval=None, expect_fingerprint=None)
    with pytest.raises(SystemExit, match="approval"):
        validate_apply_gates(args, computed="abc")

    refused = tmp_path / "approval.txt"
    refused.write_text("listo, dale", encoding="utf-8")
    args.approval = refused
    with pytest.raises(SystemExit, match="approve"):
        validate_apply_gates(args, computed="abc")


def test_apply_requires_the_reviewed_fingerprint(tmp_path: Path) -> None:
    """Missing or mismatched ``--expect-fingerprint`` aborts — rejected, not warned."""
    approval = tmp_path / "approval.txt"
    approval.write_text("approve", encoding="utf-8")
    args = argparse.Namespace(approval=approval, expect_fingerprint=None)
    with pytest.raises(SystemExit, match="expect-fingerprint"):
        validate_apply_gates(args, computed="abc")

    args.expect_fingerprint = "different"
    with pytest.raises(SystemExit, match="abc"):
        validate_apply_gates(args, computed="abc")

    args.expect_fingerprint = "abc"
    validate_apply_gates(args, computed="abc")  # accepted: no raise


async def test_dry_run_declares_the_inherent_unavailability_window(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The dry-run must name the window, the silent 0.0000, and the ban on evaluating."""
    _wire_main(monkeypatch, [(_STATE_ONLINE, _CHUNK_COUNT)])

    rc = await _MODULE.main([])
    out = capsys.readouterr().out

    assert rc == 0
    # (a) inherent window, hardcoded read-path name ⇒ no shadow-swap
    assert "shadow-swap" in out
    # (b) a 0.0000 after the DDL means absent/repopulating, not a dead analyzer
    assert "0.0000" in out
    assert "neo4j_retrieval_adapter.py:92-95" in out
    assert "return_exceptions=True" in out
    # (c) never evaluate while the window is open
    assert "NO correr la evaluación" in out
    # the rollback protocol is advertised in the same output
    assert "rollback" in out


async def test_json_dry_run_carries_the_window_warning(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The machine-readable plan output carries the same warning verbatim."""
    _wire_main(monkeypatch, [(_STATE_ONLINE, _CHUNK_COUNT)])

    rc = await _MODULE.main(["--json"])
    out = capsys.readouterr().out
    payload = json.loads(out[: out.rfind("}") + 1])

    assert rc == 0
    assert "shadow-swap" in payload["unavailability_window"]


async def test_apply_output_declares_the_window_before_the_ddl(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """The apply run prints the window warning before the first DDL statement."""
    counters = _wire_main(
        monkeypatch,
        [(_STATE_ONLINE, _CHUNK_COUNT), (_STATE_ONLINE, _CHUNK_COUNT)],
    )
    fingerprint = await _fingerprint_from_dry_run(counters, capsys)

    rc = await _MODULE.main(
        [
            "--apply",
            "--approval",
            str(_approval(tmp_path)),
            "--expect-fingerprint",
            fingerprint,
        ]
    )
    out = capsys.readouterr().out

    assert rc == 0
    assert "shadow-swap" in out
    assert out.index("shadow-swap") < out.index("APLICANDO")


async def test_apply_failure_reports_observed_state_and_exact_recovery(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """CREATE failing after DROP committed ⇒ observed ``absent`` + exact recovery, rc 2.

    The recovery must say the index has to be recreated and that re-running
    needs a fresh dry-run and a new fingerprint, because the plan embeds
    ``current_index_state`` and it now reads ``absent``.
    """
    counters = _wire_main(
        monkeypatch,
        [(_STATE_ONLINE, _CHUNK_COUNT), (_STATE_ABSENT, _CHUNK_COUNT)],
        apply_exc=RuntimeError("CREATE falló"),
    )
    fingerprint = await _fingerprint_from_dry_run(counters, capsys)

    rc = await _MODULE.main(
        [
            "--apply",
            "--approval",
            str(_approval(tmp_path)),
            "--expect-fingerprint",
            fingerprint,
        ]
    )
    out = capsys.readouterr().out

    assert rc == 2
    assert "APPLY FALLÓ" in out
    assert "estado observado tras el fallo: absent" in out
    assert "search_chunks fallará en duro" in out
    assert "current_index_state" in out
    assert "--expect-fingerprint" in out
    assert counters["applies"] == 1


async def test_apply_failure_with_a_surviving_index_still_reports_rc2(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """If ``db.awaitIndexes`` fails but the index exists, still rc 2 + re-verify advice."""
    counters = _wire_main(
        monkeypatch,
        [(_STATE_ONLINE, _CHUNK_COUNT), (_STATE_ONLINE, _CHUNK_COUNT)],
        apply_exc=TimeoutError("awaitIndexes timeout"),
    )
    fingerprint = await _fingerprint_from_dry_run(counters, capsys)

    rc = await _MODULE.main(
        [
            "--apply",
            "--approval",
            str(_approval(tmp_path)),
            "--expect-fingerprint",
            fingerprint,
        ]
    )
    out = capsys.readouterr().out

    assert rc == 2
    assert "APPLY FALLÓ: TimeoutError" in out
    assert "estado observado tras el fallo: ONLINE (population=100.0%, analyzer=" in out
    assert "no es 'absent'" in out
    assert "ÍNDICE AUSENTE" not in out


async def test_post_condition_fails_when_the_analyzer_does_not_match(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """ONLINE/100% with the wrong analyzer must fail: the no-op hole stays closed."""
    wrong_analyzer: dict[str, Any] = {
        "index_state": "ONLINE (population=100.0%, analyzer=simple)",
        "state": "ONLINE",
        "population": 100.0,
        "analyzer": "simple",
    }
    counters = _wire_main(
        monkeypatch,
        [(_STATE_ONLINE, _CHUNK_COUNT), (wrong_analyzer, _CHUNK_COUNT)],
    )
    fingerprint = await _fingerprint_from_dry_run(counters, capsys)

    rc = await _MODULE.main(
        [
            "--apply",
            "--approval",
            str(_approval(tmp_path)),
            "--expect-fingerprint",
            fingerprint,
        ]
    )
    out = capsys.readouterr().out

    assert rc == 1
    assert "analyzer observado 'simple'" in out
    assert "!= 'standard-no-stop-words'" in out


async def test_post_condition_accepts_an_integer_population_and_reports_verification(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """``population=100`` (integer spelling) passes, and the success line is true."""
    int_population: dict[str, Any] = {
        "index_state": "ONLINE (population=100, analyzer=standard-no-stop-words)",
        "state": "ONLINE",
        "population": 100,
        "analyzer": "standard-no-stop-words",
    }
    counters = _wire_main(
        monkeypatch,
        [(_STATE_ONLINE, _CHUNK_COUNT), (int_population, _CHUNK_COUNT)],
    )
    fingerprint = await _fingerprint_from_dry_run(counters, capsys)

    rc = await _MODULE.main(
        [
            "--apply",
            "--approval",
            str(_approval(tmp_path)),
            "--expect-fingerprint",
            fingerprint,
        ]
    )
    out = capsys.readouterr().out

    assert rc == 0
    assert "verificado vía SHOW INDEXES" in out


def test_docstring_names_the_rollback_protocol_and_the_post_ddl_void_trap() -> None:
    """The rollback is *the* documented protocol, RUN VOID trap included."""
    doc = _MODULE.__doc__ or ""

    assert "Rollback" in doc
    assert "scripts/eval_fulltext_recall.py --shape-ab" in doc
    assert "RUN VOID" in doc
    assert "pre-DDL" in doc
