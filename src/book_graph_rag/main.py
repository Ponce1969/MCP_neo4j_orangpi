"""CLI entrypoint for book-graph-rag."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

import click

from book_graph_rag.application.audit_graph_use_case import AuditGraphUseCase, build_audit_target
from book_graph_rag.application.backfill_checkpoints_use_case import (
    BackfillCheckpointsUseCase,
)
from book_graph_rag.application.index_book_use_case import IndexBookUseCase
from book_graph_rag.application.query_knowledge_graph_use_case import (
    QueryKnowledgeGraphUseCase,
)
from book_graph_rag.application.replay_dead_letter_use_case import (
    ReplayDeadLetterUseCase,
)
from book_graph_rag.application.validate_graph_use_case import ValidateGraphUseCase
from book_graph_rag.config import Settings, validate_llm_provider_settings
from book_graph_rag.domain.checkpoint_models import ReplayCommand
from book_graph_rag.domain.models import (
    BatchEntityQuery,
    EntityQuery,
    GraphQueryUnion,
    PathQuery,
    RelationQuery,
)
from book_graph_rag.domain.namespaces import SourceNamespace, UnknownNamespaceError
from book_graph_rag.domain.validation_models import BookScope, SmokeManifest, TargetScope
from book_graph_rag.infrastructure.catalog_loader import CatalogLoader, CatalogLoadError
from book_graph_rag.infrastructure.dead_letter import JSONLDeadLetter
from book_graph_rag.infrastructure.json_evidence_adapter import JSONEvidenceAdapter
from book_graph_rag.infrastructure.llm_adapter import LLMAdapter
from book_graph_rag.infrastructure.neo4j_audit_adapter import Neo4jAuditAdapter
from book_graph_rag.infrastructure.neo4j_checkpoint_adapter import Neo4jCheckpointAdapter
from book_graph_rag.infrastructure.neo4j_command_adapter import Neo4jCommandAdapter
from book_graph_rag.infrastructure.neo4j_query_adapter import Neo4jQueryAdapter
from book_graph_rag.infrastructure.neo4j_retrieval_smoke_adapter import Neo4jRetrievalSmokeAdapter
from book_graph_rag.infrastructure.neo4j_validation_adapter import Neo4jValidationAdapter
from book_graph_rag.infrastructure.pdf_adapter import PDFAdapter
from book_graph_rag.infrastructure.version_dimensions import compute_version_dimensions


@click.group()
@click.version_option(prog_name="book-graph-rag")
def cli() -> None:
    """book-graph-rag: Knowledge-graph RAG indexer for Agentic Architectural Patterns."""


def _resolve_namespace(
    settings: Settings, corpus: str | None, source: str | None
) -> SourceNamespace | None:
    """Resolve the catalog namespace, or ``None`` for legacy non-namespaced ids."""
    if corpus is None and source is None:
        return None
    if corpus is None or source is None:
        raise click.UsageError("--corpus and --source must be provided together")
    catalog = CatalogLoader(settings.catalog_path).load()
    return catalog.resolve_source(corpus, source)


@cli.command("index")
@click.argument("pdf_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--corpus", default=None, help="Catalog corpus that owns the source.")
@click.option(
    "--source",
    default=None,
    help="Catalog source slug (e.g. agentic-architectural-patterns).",
)
@click.option(
    "--resume/--no-resume",
    default=True,
    show_default=True,
    help="Resume skipping already-PROCESSED chunks (default), or one-shot legacy flush.",
)
@click.option(
    "--replay-dead-letter",
    is_flag=True,
    default=False,
    help="Re-process failed chunks recorded in the dead-letter JSONL.",
)
@click.option(
    "--backfill-checkpoints",
    is_flag=True,
    default=False,
    help="Backfill :Checkpoint rows for a legacy graph.",
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Maximum number of dead-letter records to replay (only with --replay-dead-letter).",
)
@click.option(
    "--source-id",
    default=None,
    help="Target source id (corpus:source) for replay or backfill.",
)
@click.option(
    "--dry-run/--apply",
    default=False,
    show_default=True,
    help=(
        "Dry-run reports candidates; --apply writes checkpoints (only with --backfill-checkpoints)."
    ),
)
@click.option(
    "--approval",
    "approval_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Approval file for destructive operations.",
)
@click.option(
    "--force-reprocess",
    is_flag=True,
    default=False,
    help="Allow replay to overwrite currently-PROCESSED chunks.",
)
def index(
    pdf_path: Path,
    corpus: str | None,
    source: str | None,
    resume: bool,
    replay_dead_letter: bool,
    backfill_checkpoints: bool,
    limit: int | None,
    source_id: str | None,
    dry_run: bool,
    approval_path: Path | None,
    force_reprocess: bool,
) -> None:
    """Index a PDF book into the knowledge graph.

    PDF_PATH is the book PDF to process. Pass ``--corpus`` and ``--source``
    together to scope book/entity ids under a catalog namespace
    (``corpus:source``); omitting both keeps legacy non-namespaced ids.

    Phase 2 adds resumable indexing, dead-letter replay, and checkpoint
    backfill. Use ``--replay-dead-letter`` or ``--backfill-checkpoints`` to
    run the corresponding admin command.
    """
    try:
        settings = Settings.model_validate({})
        validate_llm_provider_settings(settings)
    except Exception as exc:  # noqa: BLE003
        click.echo(f"Configuration error: {exc}", err=True)
        sys.exit(1)

    try:
        namespace = _resolve_namespace(settings, corpus, source)
    except (UnknownNamespaceError, CatalogLoadError) as exc:
        click.echo(f"Namespace error: {exc}", err=True)
        sys.exit(2)

    if replay_dead_letter and backfill_checkpoints:
        raise click.UsageError(
            "--replay-dead-letter and --backfill-checkpoints are mutually exclusive"
        )
    if replay_dead_letter and not resume:
        raise click.UsageError("--replay-dead-letter cannot be combined with --no-resume")
    if backfill_checkpoints and not resume:
        raise click.UsageError("--backfill-checkpoints cannot be combined with --no-resume")
    if limit is not None and not replay_dead_letter:
        raise click.UsageError("--limit is only valid with --replay-dead-letter")
    if limit is not None and limit < 1:
        raise click.UsageError("--limit must be a positive integer")
    if dry_run and not backfill_checkpoints:
        raise click.UsageError("--dry-run is only valid with --backfill-checkpoints")

    if replay_dead_letter:
        mode = "replay_dead_letter"
    elif backfill_checkpoints:
        mode = "backfill_checkpoints"
    elif resume:
        mode = "resume"
    else:
        mode = "no_resume"

    command = ReplayCommand(
        mode=mode,  # type: ignore[arg-type]
        force_reprocess=force_reprocess,
        limit=limit,
        source_id=source_id,
        dry_run=dry_run,
    )

    if namespace is None:
        pdf_adapter = PDFAdapter(settings)
        llm_adapter = LLMAdapter(settings)
    else:
        pdf_adapter = PDFAdapter(settings, namespace)
        llm_adapter = LLMAdapter(settings, namespace)
    neo4j_command_adapter = Neo4jCommandAdapter(settings)

    effective_source_id = command.source_id or (
        namespace.source_id if namespace is not None else None
    )

    checkpoint_enabled = bool(getattr(settings, "checkpoint_enabled", False))
    versions = None
    if checkpoint_enabled or command.mode in {"replay_dead_letter", "backfill_checkpoints"}:
        versions = compute_version_dimensions(pdf_path.read_bytes(), settings)

    if command.mode in {"resume", "no_resume"}:
        if checkpoint_enabled and versions is not None:
            checkpoint_adapter = Neo4jCheckpointAdapter(settings)
            dead_letter_port = JSONLDeadLetter(
                settings.dead_letter_path, settings.dead_letter_path_chunks
            )
            index_use_case = IndexBookUseCase(
                pdf_port=pdf_adapter,
                llm_port=llm_adapter,
                graph_db_port=neo4j_command_adapter,
                max_concurrency=settings.llm_max_concurrency,
                batch_size=settings.processing_batch_size,
                dead_letter_path=settings.dead_letter_path,
                checkpoint_port=checkpoint_adapter,
                dead_letter_port=dead_letter_port,
                versions=versions,
                checkpoint_enabled=True,
                resume=command.mode == "resume",
                max_attempts=getattr(settings, "checkpoint_max_attempts", 3),
                stale_lease_seconds=getattr(settings, "checkpoint_stale_lease_seconds", 300),
            )
        else:
            index_use_case = IndexBookUseCase(
                pdf_port=pdf_adapter,
                llm_port=llm_adapter,
                graph_db_port=neo4j_command_adapter,
                max_concurrency=settings.llm_max_concurrency,
                batch_size=settings.processing_batch_size,
                dead_letter_path=settings.dead_letter_path,
            )

        asyncio.run(index_use_case.execute(str(pdf_path)))
        return

    if command.mode == "replay_dead_letter":
        if versions is None:
            raise click.UsageError("Could not compute version dimensions")

        checkpoint_adapter = Neo4jCheckpointAdapter(settings)
        dead_letter_port = JSONLDeadLetter(
            settings.dead_letter_path, settings.dead_letter_path_chunks
        )
        chunks = list(pdf_adapter.extract_chunks(str(pdf_path)))
        chunk_by_index = {chunk.chunk_index: chunk for chunk in chunks}

        async def chunk_loader(source_id_param: str, chunk_index: int) -> Any:
            try:
                return chunk_by_index[chunk_index]
            except KeyError as exc:
                raise ValueError(f"Chunk {chunk_index} not found in {pdf_path}") from exc

        replay_use_case = ReplayDeadLetterUseCase(
            checkpoint_port=checkpoint_adapter,
            graph_db_port=neo4j_command_adapter,
            llm_port=llm_adapter,
            dead_letter_port=dead_letter_port,
            versions=versions,
            chunk_loader=chunk_loader,
            dead_letter_path=settings.dead_letter_path_chunks,
            max_attempts=getattr(settings, "checkpoint_max_attempts", 3),
        )

        try:
            try:
                processed = asyncio.run(
                    replay_use_case.execute(
                        source_id=effective_source_id,
                        limit=command.limit,
                        force_reprocess=command.force_reprocess,
                    )
                )
            finally:
                asyncio.run(replay_use_case.close())
        except Exception as exc:  # noqa: BLE001
            click.echo(f"Replay error: {exc}", err=True)
            sys.exit(3)
        click.echo(f"Replayed {processed} dead-letter chunk(s)")
        return

    if command.mode == "backfill_checkpoints":
        if effective_source_id is None:
            raise click.UsageError("--source-id is required for --backfill-checkpoints")
        if versions is None:
            raise click.UsageError("Could not compute version dimensions")

        checkpoint_adapter = Neo4jCheckpointAdapter(settings)
        backfill_use_case = BackfillCheckpointsUseCase(
            checkpoint_port=checkpoint_adapter,
            graph_db_port=neo4j_command_adapter,
            versions=versions,
            run_id=f"cli-{uuid4().hex[:12]}",
        )

        try:
            try:
                report = asyncio.run(
                    backfill_use_case.execute(
                        effective_source_id,
                        apply=not command.dry_run,
                        approval_path=approval_path,
                    )
                )
            finally:
                asyncio.run(backfill_use_case.close())
        except Exception as exc:  # noqa: BLE001
            click.echo(f"Backfill error: {exc}", err=True)
            sys.exit(3)
        click.echo(report.model_dump_json(indent=2))
        return


def _build_graph_query(query_type: str, params: dict[str, Any]) -> GraphQueryUnion:
    """Build a concrete GraphQuery from the CLI type and parsed JSON params."""
    match query_type:
        case "entity":
            return EntityQuery(
                name=params["name"],
                entity_type=params.get("entity_type"),
                limit=params.get("limit", 100),
            )
        case "relation":
            return RelationQuery(
                source_id=params["source_id"],
                rel_type=params.get("rel_type"),
                depth=params.get("depth", 1),
            )
        case "path":
            return PathQuery(
                start_id=params["start_id"],
                end_id=params["end_id"],
                max_depth=params.get("max_depth", 3),
            )
        case "batch_entity":
            return BatchEntityQuery(ids=params["ids"])
        case _:
            raise ValueError(f"Unsupported query type: {query_type}")


@cli.command("audit")
@click.option("--target", required=True, type=click.Choice(["bookgraph-neo4j"]))
@click.option("--sample-limit", default=50, type=click.IntRange(min=0), show_default=True)
@click.option("--output", type=click.Path(dir_okay=False, path_type=Path), default=None)
def audit(target: str, sample_limit: int, output: Path | None) -> None:
    """Run the configured project's static, read-only graph audit."""
    try:
        settings = Settings.model_validate({})
        audit_target = build_audit_target(target, settings.neo4j_uri, settings.neo4j_database)
        adapter = Neo4jAuditAdapter(settings)
    except Exception:
        payload = json.dumps(
            {
                "report_schema_version": "1.0",
                "state": "failed",
                "reason": "configuration_or_audit_failure",
            }
        )
        click.echo(payload)
        raise click.exceptions.Exit(13) from None

    async def _run_audit() -> Any:
        try:
            return await AuditGraphUseCase(adapter).execute(audit_target, sample_limit)
        finally:
            if hasattr(adapter, "close"):
                await adapter.close()

    try:
        report = asyncio.run(_run_audit())
    except Exception:
        payload = json.dumps(
            {
                "report_schema_version": "1.0",
                "state": "failed",
                "reason": "configuration_or_audit_failure",
            }
        )
        click.echo(payload)
        raise click.exceptions.Exit(13) from None

    payload = report.model_dump_json(indent=2)
    click.echo(payload)
    if output is not None:
        output.write_text(payload + "\n", encoding="utf-8")
    execution = getattr(report, "execution", None)
    code = (
        execution.exit_code
        if execution
        else {
            "passed": 0,
            "violations": 10,
            "incomplete": 11,
            "unreachable": 12,
            "failed": 13,
        }.get(str(report.state), 13)
    )
    raise click.exceptions.Exit(code)


@cli.command("query")
@click.option(
    "--type",
    "query_type",
    required=True,
    type=click.Choice(["entity", "relation", "path", "batch_entity"]),
)
@click.option("--query", "query_json", required=True, help="JSON query parameters")
def query(query_type: str, query_json: str) -> None:
    """Query the knowledge graph. Output is pure JSON."""
    try:
        settings = Settings.model_validate({})
    except Exception as exc:  # noqa: BLE001
        click.echo(f"Configuration error: {exc}", err=True)
        sys.exit(1)

    try:
        params = json.loads(query_json)
    except json.JSONDecodeError as exc:
        click.echo(f"Invalid JSON: {exc}", err=True)
        sys.exit(2)

    graph_query = _build_graph_query(query_type, params)

    query_adapter = Neo4jQueryAdapter(settings)
    use_case = QueryKnowledgeGraphUseCase(port=query_adapter)

    async def _run() -> Any:
        try:
            return await use_case.execute(graph_query)
        finally:
            await query_adapter.close()

    try:
        result = asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        click.echo(f"Query error: {exc}", err=True)
        sys.exit(3)

    click.echo(result.model_dump_json(indent=2))


def _book_scope(book_id: str) -> BookScope:
    fingerprint = "sha256:" + hashlib.sha256(book_id.encode()).hexdigest()
    return BookScope(
        book_id=book_id,
        source_identity=book_id,
        path_fingerprint=fingerprint,
    )


@cli.command("validate")
@click.option("--book-id", required=True, help="Intended current book identity")
@click.option(
    "--manifest",
    "manifest_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Path to the versioned smoke manifest",
)
@click.option("--sample-limit", default=50, type=click.IntRange(min=0), show_default=True)
@click.option("--output", type=click.Path(dir_okay=False, path_type=Path), default=None)
@click.option(
    "--approval",
    "approval_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
)
def validate(
    book_id: str,
    manifest_path: Path | None,
    sample_limit: int,
    output: Path | None,
    approval_path: Path | None,
) -> None:
    """Run the read-only pre-reindex graph validation protocol."""
    try:
        settings = Settings.model_validate({})
        validate_llm_provider_settings(settings)
    except Exception as exc:  # noqa: BLE003
        click.echo(f"Configuration error: {exc}", err=True)
        sys.exit(1)

    evidence_adapter = JSONEvidenceAdapter()
    try:
        manifest = (
            evidence_adapter.read_smoke_manifest(manifest_path)
            if manifest_path is not None
            else SmokeManifest(manifest_id="empty", version="1.0.0", book_id=book_id, cases=())
        )
    except Exception as exc:  # noqa: BLE003
        click.echo(f"Manifest error: {exc}", err=True)
        sys.exit(2)

    target = TargetScope(
        name="bookgraph-neo4j",
        database=settings.neo4j_database,
        environment="production",
    )
    book_scope = _book_scope(book_id)

    validation_adapter = Neo4jValidationAdapter(settings)
    query_adapter = Neo4jQueryAdapter(settings)
    retrieval_adapter = Neo4jRetrievalSmokeAdapter(query_adapter)

    use_case = ValidateGraphUseCase(
        validation=validation_adapter,
        retrieval=retrieval_adapter,
        evidence_writer=evidence_adapter,
        manifest_reader=evidence_adapter,
    )

    async def _run() -> Any:
        try:
            return await use_case.execute(
                run_id="validate-" + uuid4().hex[:12],
                target=target,
                book_scope=book_scope,
                configuration=settings.model_dump(),
                audit_version="p1-static-v1",
                smoke_manifest=manifest,
                sample_limit=sample_limit,
                output_path=output or Path("evidence-bundle.json"),
                approval_path=approval_path,
            )
        finally:
            await validation_adapter.close()
            await query_adapter.close()

    try:
        bundle, policy = asyncio.run(_run())
    except Exception as exc:  # noqa: BLE003
        click.echo(f"Validation error: {exc}", err=True)
        raise click.exceptions.Exit(13) from None

    click.echo(bundle.model_dump_json(indent=2))
    raise click.exceptions.Exit(policy.exit_code)


def main() -> None:
    """Script entrypoint for `book-graph-rag` console command."""
    cli()


if __name__ == "__main__":
    main()
