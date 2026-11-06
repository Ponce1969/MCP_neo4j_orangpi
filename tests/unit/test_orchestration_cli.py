"""Behaviour of the ``orchestrate`` CLI command (T7: L13, L14, L15 binding).

Every test drives the command through Click only: the stack factory
(``build_orchestration_stack``) and ``Settings.model_validate`` are replaced,
so no Neo4j driver, no LLM client, no BTRAG transport and no developer
``.env`` is ever touched — settings come from the sanctioned
``isolated_settings_factory`` in ``tests/conftest.py``.

The fake stack records the raw payload it receives and delegates to the real
``parse_orchestration_request`` + ``compose_answer`` with recording ports, so
payload shape (scope order, ``requires_passages``) and the L13 binding (no
port touched) are asserted through the same code path production runs.
"""

from __future__ import annotations

import json
from typing import Any

from click.testing import CliRunner
from tests.host_env import SettingsFactory

from book_graph_rag import main as cli_module
from book_graph_rag.application.orchestration.compose import CompositionResult, compose_answer
from book_graph_rag.application.orchestration.parse_request import parse_orchestration_request
from book_graph_rag.application.orchestration.ports import GraphEvidencePort, TextPassagePort
from book_graph_rag.config import Settings
from book_graph_rag.domain.namespaces import SourceNamespace
from book_graph_rag.domain.orchestration_models import OrchestrationRequest
from book_graph_rag.domain.orchestration_outcomes import GraphEvidence, Passage
from book_graph_rag.domain.routing_models import ResolvedRoute, RouteDecision
from book_graph_rag.infrastructure.routing_caller import RoutingCaller

QUESTION = "¿qué es un router de consultas?"
_AAP = "knowledge:agentic-architectural-patterns"
_ESSENTIAL = "knowledge:essential-graphrag"


def _ns(namespace_id: str) -> SourceNamespace:
    corpus, source = namespace_id.split(":", maxsplit=1)
    return SourceNamespace(corpus=corpus, source=source)


# ── Recording doubles for the real compose path ──────────────────────────────


class _FakeExecutor:
    """Router executor double: records calls, returns one canned route."""

    def __init__(self, route: ResolvedRoute) -> None:
        self.route = route
        self.calls: list[str] = []

    async def execute(self, question: str) -> ResolvedRoute:
        self.calls.append(question)
        return self.route


class _FakeEvidence(GraphEvidencePort):
    """Evidence double recording every query, keyed by scope source_id."""

    def __init__(self, results: dict[str, GraphEvidence | None] | None = None) -> None:
        self._results = results if results is not None else {}
        self.calls: list[str] = []

    async def query(self, question: str, scope: SourceNamespace) -> GraphEvidence | None:
        self.calls.append(scope.source_id)
        return self._results.get(scope.source_id)


class _FakeText(TextPassagePort):
    """Text double recording every ``(question, limit)`` call."""

    def __init__(self, passages: tuple[Passage, ...] = ()) -> None:
        self._passages = passages
        self.calls: list[tuple[str, int]] = []

    async def passages(self, question: str, *, limit: int) -> tuple[Passage, ...]:
        self.calls.append((question, limit))
        return self._passages


class _FakeStack:
    """Stand-in for ``OrchestrationStack`` over the real compose use case.

    Records the raw payload verbatim (that is what the CLI contract is
    asserted against) and every port touch; ``close()`` counts itself so the
    finally-path contract is observable.
    """

    def __init__(
        self,
        *,
        route: ResolvedRoute | None = None,
        evidence_results: dict[str, GraphEvidence | None] | None = None,
        passages: tuple[Passage, ...] = (),
        answer_error: Exception | None = None,
    ) -> None:
        self.payloads: list[dict[str, Any]] = []
        self.close_calls = 0
        self.answer_error = answer_error
        self.executor = _FakeExecutor(
            route
            if route is not None
            else ResolvedRoute(decision=RouteDecision(route_kind="abstain", reason="low_score"))
        )
        self.router = RoutingCaller(self.executor, enabled=True)
        self.evidence = _FakeEvidence(evidence_results)
        self.synthesis = _FakeEvidence()
        self.text = _FakeText(passages)

    async def answer(self, payload: dict[str, Any]) -> CompositionResult:
        self.payloads.append(payload)
        if self.answer_error is not None:
            raise self.answer_error
        request = parse_orchestration_request(payload)
        return await compose_answer(
            request,
            router=self.router,
            evidence=self.evidence,
            synthesis=self.synthesis,
            text=self.text,
        )

    async def close(self) -> None:
        self.close_calls += 1


def _committed_route(namespace_id: str = _AAP) -> ResolvedRoute:
    namespace = _ns(namespace_id)
    return ResolvedRoute(
        decision=RouteDecision(
            selected=namespace,
            top_score=0.42,
            margin=0.30,
            route_kind="single",
            reason="clear",
        ),
        validated_namespace=namespace,
    )


def _install(
    monkeypatch: Any,
    isolated_settings_factory: SettingsFactory,
    stack: _FakeStack | None,
) -> list[Any]:
    """Install the settings provider and the stack factory; return factory calls.

    ``stack=None`` means the factory must never be reached — a sentinel
    assertion records the attempt instead of building anything.
    """
    settings = isolated_settings_factory(
        text_rag_url="http://text-rag.invalid/mcp",
        text_rag_token="unit-test-token-value",
    )

    class _SettingsProvider:
        @staticmethod
        def model_validate(_payload: dict[str, Any]) -> Settings:
            return settings

    factory_calls: list[Settings] = []

    def _factory(received: Settings) -> _FakeStack:
        factory_calls.append(received)
        if stack is None:
            raise AssertionError("the stack factory must not be reached on this path")
        return stack

    monkeypatch.setattr(cli_module, "Settings", _SettingsProvider)
    monkeypatch.setattr(
        "book_graph_rag.infrastructure.orchestration.wiring.build_orchestration_stack",
        _factory,
    )
    return factory_calls


def _invoke(*args: str) -> Any:
    return CliRunner().invoke(cli_module.cli, ["orchestrate", *args])


def _payload(result: Any) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(result.stdout)
    return parsed


def _stderr(result: Any) -> str:
    try:
        return str(result.stderr)
    except ValueError:  # click < 8.2 without a separate stderr stream
        return str(result.output)


# ── 1. Evidence mode, covered: data out, exit 0 ──────────────────────────────


def test_evidence_covered_exits_zero_with_full_json(
    monkeypatch: Any,
    isolated_settings_factory: SettingsFactory,
) -> None:
    """S1/S2: evidence mode + explicit scope → covered, JSON, exit 0."""
    stack = _FakeStack(
        route=_committed_route(),
        evidence_results={_AAP: GraphEvidence(namespace=_ns(_AAP), summary="s", citations=("c1",))},
    )
    _install(monkeypatch, isolated_settings_factory, stack)

    result = _invoke(QUESTION, "--scope", _AAP)

    assert result.exit_code == 0, result.output
    payload = _payload(result)
    assert payload["state"] == "covered"
    assert payload["mode"] == "evidence"
    assert payload["reason"].startswith("explicit_scope")
    assert payload["evidence"] == [{"citations": ["c1"], "namespace": _AAP}]
    assert payload["passages"] == []
    assert payload["scopes"] == [_AAP]
    assert payload["cache"] == {"cacheable": True, "reason": "cacheable"}
    assert payload["intent_tuning"] is None
    # Explicit scope ⇒ the router was never consulted (S2/L14).
    assert stack.executor.calls == []
    assert stack.close_calls == 1


# ── 2. Synthesis without a committed scope: insufficient, no port touched ───


def test_synthesis_without_committed_scope_is_insufficient_and_touches_no_port(
    monkeypatch: Any,
    isolated_settings_factory: SettingsFactory,
) -> None:
    """L13: synthesis + no committed scope → insufficient_coverage, ports at zero.

    Exit 0: insufficient coverage is data, not a failure — the same contract
    as ``route``'s abstention.
    """
    stack = _FakeStack()  # default route: abstain without a validated namespace
    _install(monkeypatch, isolated_settings_factory, stack)

    result = _invoke(QUESTION, "--mode", "synthesis")

    assert result.exit_code == 0, result.output
    payload = _payload(result)
    assert payload["state"] == "insufficient_coverage"
    assert "committed scope" in payload["reason"]
    assert payload["evidence"] == []
    assert payload["passages"] == []
    assert payload["scopes"] == []
    assert payload["cache"] == {"cacheable": False, "reason": "insufficient_coverage"}
    # L13: no evidence call, no ask_global, not even the text port.
    assert stack.evidence.calls == []
    assert stack.synthesis.calls == []
    assert stack.text.calls == []
    assert stack.close_calls == 1


# ── 3. Repeated --scope reaches the payload in order ─────────────────────────


def test_repeated_scope_reaches_payload_as_two_entries_in_order(
    monkeypatch: Any,
    isolated_settings_factory: SettingsFactory,
) -> None:
    """S2: two ``--scope`` options arrive as two entries, caller order kept."""
    stack = _FakeStack(
        evidence_results={
            _ESSENTIAL: GraphEvidence(namespace=_ns(_ESSENTIAL), summary="s", citations=("e1",)),
            _AAP: GraphEvidence(namespace=_ns(_AAP), summary="s", citations=("a1",)),
        },
    )
    _install(monkeypatch, isolated_settings_factory, stack)

    result = _invoke(QUESTION, "--scope", _ESSENTIAL, "--scope", _AAP)

    assert result.exit_code == 0, result.output
    assert stack.payloads[0]["scope"] == [
        {"corpus": "knowledge", "source": "essential-graphrag"},
        {"corpus": "knowledge", "source": "agentic-architectural-patterns"},
    ]
    # Both scopes dispatched in the caller's order (S2/L14), router untouched.
    assert stack.evidence.calls == [_ESSENTIAL, _AAP]
    assert stack.executor.calls == []
    assert _payload(result)["scopes"] == [_ESSENTIAL, _AAP]


# ── 4. Malformed --scope: usage error, stack never built ─────────────────────


def test_malformed_scope_is_usage_error_and_never_builds_the_stack(
    monkeypatch: Any,
    isolated_settings_factory: SettingsFactory,
) -> None:
    """A malformed ``--scope`` exits non-zero with a usage message, no factory."""
    factory_calls = _install(monkeypatch, isolated_settings_factory, None)

    for malformed in ("no-separator", "a:b:c", ":half-empty"):
        result = _invoke(QUESTION, "--scope", malformed)
        assert result.exit_code != 0, result.output
        combined = result.stdout + _stderr(result)
        assert "--scope must be CORPUS:SOURCE" in combined

    assert factory_calls == []  # the stack was never built


# ── 5. --requires-passages reaches the request as true ───────────────────────


def test_requires_passages_flag_reaches_the_request(
    monkeypatch: Any,
    isolated_settings_factory: SettingsFactory,
) -> None:
    """S5: the caller's flag arrives as ``requires_passages: true``, verbatim."""
    stack = _FakeStack(
        route=_committed_route(),
        evidence_results={_AAP: GraphEvidence(namespace=_ns(_AAP), summary="s", citations=("c1",))},
        passages=(Passage(chunk_id="book-text-rag:chunk-9", text="verbatim prose"),),
    )
    _install(monkeypatch, isolated_settings_factory, stack)

    result = _invoke(QUESTION, "--scope", _AAP, "--requires-passages")

    assert result.exit_code == 0, result.output
    assert stack.payloads[0]["requires_passages"] is True
    # The text port was called (S5) and the passage rides out as an id only.
    assert len(stack.text.calls) == 1
    assert _payload(result)["passages"] == ["book-text-rag:chunk-9"]
    assert "verbatim prose" not in result.stdout


# ── 6. Exception from answer: exit 1, stderr message, no JSON on stdout ──────


def test_answer_exception_exits_one_with_message_and_no_json(
    monkeypatch: Any,
    isolated_settings_factory: SettingsFactory,
) -> None:
    """A runtime failure exits 1: message on stderr, no traceback, no JSON."""
    stack = _FakeStack(answer_error=RuntimeError("graph unreachable"))
    _install(monkeypatch, isolated_settings_factory, stack)

    result = _invoke(QUESTION)

    assert result.exit_code == 1
    assert "graph unreachable" in _stderr(result)
    assert result.stdout.strip() == ""
    assert stack.close_calls == 1  # the error path still closed the stack


# ── 7. close() on every path ─────────────────────────────────────────────────


def test_close_is_called_on_every_path(
    monkeypatch: Any,
    isolated_settings_factory: SettingsFactory,
) -> None:
    """The stack closes on success, on insufficient coverage and on failure."""
    covered = _FakeStack(
        route=_committed_route(),
        evidence_results={_AAP: GraphEvidence(namespace=_ns(_AAP), summary="s", citations=("c1",))},
    )
    _install(monkeypatch, isolated_settings_factory, covered)
    assert _invoke(QUESTION, "--scope", _AAP).exit_code == 0
    assert covered.close_calls == 1

    insufficient = _FakeStack()
    _install(monkeypatch, isolated_settings_factory, insufficient)
    assert _invoke(QUESTION, "--mode", "synthesis").exit_code == 0
    assert insufficient.close_calls == 1

    failing = _FakeStack(answer_error=RuntimeError("boom"))
    _install(monkeypatch, isolated_settings_factory, failing)
    assert _invoke(QUESTION).exit_code == 1
    assert failing.close_calls == 1


# ── Request-level pin: the payload the CLI builds parses as the contract ────


def test_default_payload_is_the_evidence_request_contract(
    monkeypatch: Any,
    isolated_settings_factory: SettingsFactory,
) -> None:
    """Bare invocation: mode defaults to evidence, no intent, scope omitted."""
    stack = _FakeStack()
    _install(monkeypatch, isolated_settings_factory, stack)

    result = _invoke(QUESTION)

    assert result.exit_code == 0, result.output
    assert stack.payloads[0] == {
        "question": QUESTION,
        "mode": "evidence",
        "requires_passages": False,
    }
    request = OrchestrationRequest.model_validate(stack.payloads[0])
    assert request.scope is None  # omitted scope = all books (S2)
    assert request.intent is None
