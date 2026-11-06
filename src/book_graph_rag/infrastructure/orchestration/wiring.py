"""Composition root for the orchestration layer (T7).

Assembles, from ``Settings`` alone, the one stack the ``orchestrate`` CLI
drives: the read-only graph adapters, the synthesis use case, the namespace
router and the BTRAG text port — the four contracts :func:`compose_answer`
needs — plus ``answer`` (parse a payload, compose one result) and ``close``
(release everything this stack created).

Decisions, mirrored from ``mcp_server_main.py`` so the two roots cannot drift:

* **Read-only credentials for every Neo4j access.** Both Neo4j adapters
  receive ``readonly_settings(settings)`` — this layer never writes, the same
  view the MCP's read paths use.
* **Nothing connects at import time.** The module only defines the stack; the
  CLI builds it per invocation, never at module import. The Neo4j drivers
  connect lazily on their first query anyway.
* **Construction order = fail-closed first.** The constructors that reject
  bad configuration run *before* anything that holds a resource:
  ``BtrTextPassageAdapter`` raises on a blank url or token (L15 decision c),
  ``LLMAdapter`` validates the provider settings, ``build_routing_caller``
  fails closed on telemetry-key and catalog problems — so a configuration
  error leaves nothing behind to close. The two drivers come last, both from
  the same URI (if the first accepted it the second cannot fail on it) and
  both connect lazily, and the pure composition (use case + port adapters)
  comes last of all because it cannot raise.
* **Close is reverse construction, best effort per adapter.** Every adapter
  this stack created that has a ``close`` is attempted in reverse creation
  order; one adapter failing to close never masks the others — every failure
  is collected and reported together in one
  :class:`OrchestrationCloseError` raised *after* all were attempted.
  Adapters without network state (LLM clients, BTRAG's per-call transport,
  the pure port wrappers) have no ``close`` and none is invented for them.

Spec home: ``odd/tasks/tool-intent-router.md`` (T7; rulings L13-L14-L15).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from book_graph_rag.application.global_query_use_case import GlobalQueryUseCase
from book_graph_rag.application.orchestration.compose import (
    CompositionResult,
    compose_answer,
)
from book_graph_rag.application.orchestration.parse_request import (
    parse_orchestration_request,
)
from book_graph_rag.application.orchestration.ports import (
    GraphEvidencePort,
    TextPassagePort,
)
from book_graph_rag.config import Settings
from book_graph_rag.infrastructure.community_adapter import Neo4jCommunityAdapter
from book_graph_rag.infrastructure.llm_adapter import LLMAdapter
from book_graph_rag.infrastructure.neo4j_query_adapter import Neo4jQueryAdapter
from book_graph_rag.infrastructure.neo4j_read_credentials import readonly_settings
from book_graph_rag.infrastructure.orchestration.btr_text_passage_adapter import (
    BtrTextPassageAdapter,
)
from book_graph_rag.infrastructure.orchestration.chunk_evidence_adapter import (
    ChunkEvidenceAdapter,
)
from book_graph_rag.infrastructure.orchestration.community_answer_adapter import (
    CommunityAnswerAdapter,
)
from book_graph_rag.infrastructure.routing_caller import (
    RoutingCaller,
    build_routing_caller,
)

#: A zero-argument async closer: the bound ``adapter.close`` of a stack-owned adapter.
_Closer = Callable[[], Awaitable[None]]


class OrchestrationCloseError(RuntimeError):
    """Raised by ``close()`` after *every* adapter was attempted; names each failure."""


class OrchestrationStack:
    """One composed orchestration answer path: parse → route → ports → one result.

    Built by :func:`build_orchestration_stack` from ``Settings`` alone; the
    CLI owns the lifetime (construct, ``answer``, ``close`` in a ``finally``).
    The stack is deliberately inert between construction and ``answer``: no
    query, no model load, no network happens at build time.
    """

    def __init__(
        self,
        *,
        router: RoutingCaller,
        evidence: GraphEvidencePort,
        synthesis: GraphEvidencePort,
        text: TextPassagePort,
        closers: Sequence[tuple[str, _Closer]],
    ) -> None:
        self._router = router
        self._evidence = evidence
        self._synthesis = synthesis
        self._text = text
        # Creation order; ``close`` walks it backwards.
        self._closers = tuple(closers)

    async def answer(self, payload: Mapping[str, Any]) -> CompositionResult:
        """Parse ``payload`` into an :class:`OrchestrationRequest` and compose one result.

        Validation errors from :func:`parse_orchestration_request` (blank
        question, unknown key, malformed scope, …) propagate untouched — a
        payload defect is never rewritten into an outcome. Port exceptions
        propagate too: the use case converts nothing into a result.

        Raises:
            ValueError: On any request-contract violation (fail-closed parse).
            Exception: Whatever a port raises, undecorated.
        """
        request = parse_orchestration_request(payload)
        return await compose_answer(
            request,
            router=self._router,
            evidence=self._evidence,
            synthesis=self._synthesis,
            text=self._text,
        )

    async def close(self) -> None:
        """Close every stack-owned adapter in reverse creation order.

        Best effort by design: an adapter failing to close is recorded, the
        remaining adapters are still closed, and only once all have been
        attempted does the aggregated :class:`OrchestrationCloseError` rise,
        naming every adapter that failed — one failure never masks another.
        Calling ``close`` twice is safe: each underlying ``close`` is
        idempotent (drivers and the telemetry adapter tolerate it).

        Raises:
            OrchestrationCloseError: After all adapters were attempted, when
                at least one of them failed; the message lists each failure.
        """
        failures: list[str] = []
        for name, closer in reversed(self._closers):
            try:
                await closer()
            except Exception as exc:  # recorded and re-reported after every adapter ran
                failures.append(f"{name}: {exc}")
        if failures:
            raise OrchestrationCloseError(
                f"{len(failures)} of {len(self._closers)} adapter(s) failed to close: "
                + "; ".join(failures)
            )


def build_orchestration_stack(settings: Settings) -> OrchestrationStack:
    """Compose the orchestration stack from ``settings`` alone.

    Construction order (fail-closed configuration first, resource holders
    after, pure composition last — see the module docstring) is also the
    order :meth:`OrchestrationStack.close` reverses.

    Raises:
        TextRagConfigurationError: On a blank ``text_rag_url`` or
            ``text_rag_token`` (L15 decision c: at construction, never at
            query time).
        ValueError: When the LLM provider settings are incomplete.
        RoutingTelemetryError / CatalogLoadError: From the router's own
            fail-closed composition.
    """
    # 1. Fail-closed configuration, zero resources held on failure.
    text = BtrTextPassageAdapter(settings.text_rag_url, settings.text_rag_token)
    llm_adapter = LLMAdapter(settings)
    router = build_routing_caller(settings)

    # 2. Read-only graph access — both drivers share ``settings.neo4j_uri``
    #    (the first construction already proved it parses) and connect lazily.
    read_settings = readonly_settings(settings)
    query_adapter = Neo4jQueryAdapter(read_settings)
    community_adapter = Neo4jCommunityAdapter(read_settings)

    # 3. Pure composition: cannot raise, holds no resource of its own.
    #    Parameters copied from ``mcp_server_main`` so the ask_global path
    #    behaves identically behind both roots.
    global_query_use_case = GlobalQueryUseCase(
        read_port=community_adapter,
        llm_port=llm_adapter,
        max_concurrency=settings.summary_max_concurrency,
        top_n=settings.ask_global_top_n,
        score_batch_size=settings.ask_global_score_batch_size,
    )
    evidence: GraphEvidencePort = ChunkEvidenceAdapter(query_adapter)
    synthesis: GraphEvidencePort = CommunityAnswerAdapter(global_query_use_case)

    # Creation order of every adapter exposing ``close``; close() reverses it.
    closers: list[tuple[str, _Closer]] = [
        ("routing_caller", router.close),
        ("neo4j_query", query_adapter.close),
        ("neo4j_community", community_adapter.close),
    ]
    return OrchestrationStack(
        router=router,
        evidence=evidence,
        synthesis=synthesis,
        text=text,
        closers=closers,
    )
