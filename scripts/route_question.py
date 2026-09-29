"""Route one or more questions to a validated book namespace (read-only).

Wires the production router: catalog-backed scope resolver, local profile
artifact, local sentence-transformers embeddings, deterministic lexical hints
derived from the catalog, and optional separate SQLite telemetry. The MCP
server and its fail-closed scope requirements are untouched.
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Sequence

import click

from book_graph_rag.application.route_question_use_case import RouteQuestionUseCase
from book_graph_rag.config import Settings
from book_graph_rag.infrastructure.routing_caller import (
    build_route_question_use_case,
    build_routing_event,
)
from book_graph_rag.infrastructure.sqlite_routing_telemetry import (
    SqliteRoutingTelemetryAdapter,
)


async def _route_all(
    questions: Sequence[str],
    use_case: RouteQuestionUseCase,
    telemetry_adapter: SqliteRoutingTelemetryAdapter | None,
    settings: Settings,
) -> int:
    if telemetry_adapter is not None:
        await telemetry_adapter.open()
    for text in questions:
        started = time.monotonic()
        resolved = await use_case.execute(text)
        latency_ms = (time.monotonic() - started) * 1000
        validated = (
            resolved.validated_namespace.source_id
            if resolved.validated_namespace is not None
            else None
        )
        fanout = [ns.source_id for ns in resolved.fanout_namespaces]
        click.echo(
            f"{text}\n  route={resolved.decision.route_kind} "
            f"({resolved.decision.reason}) selected={validated} "
            f"fanout={fanout or '-'} score={resolved.decision.top_score:.3f}"
        )
        if telemetry_adapter is not None:
            await telemetry_adapter.record_event(
                build_routing_event(text, resolved, settings, latency_ms=latency_ms)
            )
    return 0


@click.command()
@click.argument("question", required=False)
@click.option("--telemetry", is_flag=True, default=False, help="Record events in SQLite.")
def route_question(question: str | None, telemetry: bool) -> None:
    """Route a question; without a question, read lines from stdin."""
    try:
        settings = Settings.model_validate({})
    except Exception as exc:  # noqa: BLE001
        click.echo(f"Configuration error: {exc}", err=True)
        sys.exit(1)

    use_case = build_route_question_use_case(settings)

    questions = [question] if question else sys.stdin.read().splitlines()
    if not questions:
        click.echo("no question provided", err=True)
        sys.exit(2)

    telemetry_adapter: SqliteRoutingTelemetryAdapter | None = None
    if telemetry:
        telemetry_adapter = SqliteRoutingTelemetryAdapter(
            settings.routing_telemetry_path,
            enable_raw=False,
        )

    try:
        asyncio.run(_route_all(questions, use_case, telemetry_adapter, settings))
    except Exception as exc:  # noqa: BLE001
        click.echo(f"Route error: {exc}", err=True)
        sys.exit(3)
    finally:
        if telemetry_adapter is not None:
            asyncio.run(telemetry_adapter.close())


if __name__ == "__main__":
    route_question()
