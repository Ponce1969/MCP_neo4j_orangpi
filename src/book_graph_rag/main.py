"""CLI entrypoint for book-graph-rag."""

from __future__ import annotations

import asyncio
import getpass
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import click

from book_graph_rag.application.approve_quarantine_use_case import (
    QuarantineDecisionOutcome,
)
from book_graph_rag.application.audit_graph_use_case import (
    AuditGraphUseCase,
    build_audit_target,
)
from book_graph_rag.application.backfill_checkpoints_use_case import (
    BackfillCheckpointsUseCase,
)
from book_graph_rag.application.enqueue_cross_namespace_quarantine_use_case import (
    EnqueueCrossNamespaceResult,
)
from book_graph_rag.application.evaluate_command_use_case import EvaluateCommandUseCase
from book_graph_rag.application.evaluate_extraction_layer_use_case import (
    EvaluateExtractionLayerUseCase,
)
from book_graph_rag.application.evaluate_gate_use_case import GateEvaluatorUseCase
from book_graph_rag.application.evaluate_generation_layer_use_case import (
    EvaluateGenerationLayerUseCase,
)
from book_graph_rag.application.evaluate_resolution_layer_use_case import (
    EvaluateResolutionLayerUseCase,
)
from book_graph_rag.application.evaluate_retrieval_layer_use_case import (
    EvaluateRetrievalLayerUseCase,
)
from book_graph_rag.application.evaluation_harness import EvaluationHarness
from book_graph_rag.application.index_book_use_case import IndexBookUseCase
from book_graph_rag.application.query_knowledge_graph_use_case import (
    QueryKnowledgeGraphUseCase,
)
from book_graph_rag.application.readiness_gate_evaluator_use_case import (
    ReadinessGateEvaluatorUseCase,
)
from book_graph_rag.application.replay_dead_letter_use_case import (
    ReplayDeadLetterUseCase,
)
from book_graph_rag.application.resolve_audit_scope import resolve_audit_scope
from book_graph_rag.application.validate_graph_use_case import ValidateGraphUseCase
from book_graph_rag.config import Settings, validate_llm_provider_settings
from book_graph_rag.domain.audit_models import AuditScope
from book_graph_rag.domain.checkpoint_models import ReplayCommand
from book_graph_rag.domain.cross_namespace_decision_models import (
    CrossNamespaceDecision,
    DecisionKind,
)
from book_graph_rag.domain.gate_models import GatePolicy, UnknownGateError
from book_graph_rag.domain.models import (
    BatchEntityQuery,
    EntityQuery,
    GraphQueryUnion,
    PathQuery,
    RelationQuery,
)
from book_graph_rag.domain.namespaces import SourceNamespace, UnknownNamespaceError
from book_graph_rag.domain.quarantine_review_models import (
    DecisionSheet,
    QuarantineListRow,
    RiskLevel,
    format_list,
    format_sheet,
)
from book_graph_rag.domain.resolution_errors import ResolutionError
from book_graph_rag.domain.resolution_models import ConfidenceBand
from book_graph_rag.domain.rollback_plan_models import (
    EdgeCensus,
    RollbackComparison,
    RollbackMeasurement,
    RollbackPlan,
    compare_census,
)
from book_graph_rag.domain.s4_band_assignment import BandThresholds
from book_graph_rag.domain.validation_models import BookScope, SmokeManifest, TargetScope
from book_graph_rag.infrastructure.brute_force_candidate_retrieval import (
    BruteForceCandidateRetrieval,
)
from book_graph_rag.infrastructure.catalog_loader import CatalogLoader, CatalogLoadError
from book_graph_rag.infrastructure.dead_letter import JSONLDeadLetter
from book_graph_rag.infrastructure.evaluation_baseline_loader import (
    JsonEvaluationBaselineLoader,
)
from book_graph_rag.infrastructure.evaluation_dataset_loader import (
    JsonlManifestEvaluationDatasetLoader,
)
from book_graph_rag.infrastructure.gate_policy_loader import GatePolicyLoader, GatePolicyLoadError
from book_graph_rag.infrastructure.json_evidence_adapter import JSONEvidenceAdapter
from book_graph_rag.infrastructure.jsonl_cross_namespace_decisions import (
    JSONLCrossNamespaceDecisions,
)
from book_graph_rag.infrastructure.jsonl_merge_ledger import JSONLMergeLedger
from book_graph_rag.infrastructure.llm_adapter import LLMAdapter
from book_graph_rag.infrastructure.llm_claim_validator import LLMClaimValidator
from book_graph_rag.infrastructure.llm_pairwise_judge import LLMPairwiseJudge
from book_graph_rag.infrastructure.neo4j_audit_adapter import Neo4jAuditAdapter
from book_graph_rag.infrastructure.neo4j_checkpoint_adapter import Neo4jCheckpointAdapter
from book_graph_rag.infrastructure.neo4j_command_adapter import Neo4jCommandAdapter
from book_graph_rag.infrastructure.neo4j_query_adapter import Neo4jQueryAdapter
from book_graph_rag.infrastructure.neo4j_retrieval_adapter import Neo4jRetrievalAdapter
from book_graph_rag.infrastructure.neo4j_retrieval_smoke_adapter import Neo4jRetrievalSmokeAdapter
from book_graph_rag.infrastructure.neo4j_validation_adapter import Neo4jValidationAdapter
from book_graph_rag.infrastructure.pdf_adapter import PDFAdapter
from book_graph_rag.infrastructure.resolution_wiring import (
    build_approve_quarantine_use_case,
    build_enqueue_cross_namespace_use_case,
    build_plan_rollback_use_case,
    build_resolve_entities_use_case,
    build_review_quarantine_use_case,
    build_rollback_apply_use_case,
)
from book_graph_rag.infrastructure.sentence_transformer_adapter import (
    SentenceTransformerAdapter,
)
from book_graph_rag.infrastructure.subprocess_ragas_runner import SubprocessRAGASRunner
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
                force_reprocess=command.force_reprocess,
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

        async def _run_replay() -> int:
            try:
                return await replay_use_case.execute(
                    source_id=effective_source_id,
                    limit=command.limit,
                    force_reprocess=command.force_reprocess,
                )
            finally:
                await replay_use_case.close()

        try:
            processed = asyncio.run(_run_replay())
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

        async def _run_backfill() -> Any:
            try:
                return await backfill_use_case.execute(
                    effective_source_id,
                    apply=not command.dry_run,
                    approval_path=approval_path,
                )
            finally:
                await backfill_use_case.close()

        try:
            report = asyncio.run(_run_backfill())
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
@click.option("--scope", default=None, help="Scoped corpus[:source] (whole-graph if omitted).")
@click.option("--output", type=click.Path(dir_okay=False, path_type=Path), default=None)
def audit(target: str, sample_limit: int, scope: str | None, output: Path | None) -> None:
    """Run the configured project's static, read-only graph audit."""
    try:
        settings = Settings.model_validate({})
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

    audit_scope: AuditScope | None = None
    if scope is not None:
        try:
            catalog = CatalogLoader(settings.catalog_path).load()
            audit_scope = resolve_audit_scope(scope, catalog)
        except (ValueError, CatalogLoadError) as exc:
            click.echo(f"Scope error: {exc}", err=True)
            sys.exit(2)

    try:
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
            return await AuditGraphUseCase(adapter).execute(
                audit_target, sample_limit, scope=audit_scope
            )
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


@cli.command("gate")
@click.argument("name")
@click.option("--target", required=True, type=click.Choice(["bookgraph-neo4j"]))
@click.option("--sample-limit", default=50, type=click.IntRange(min=0), show_default=True)
@click.option("--scope", default=None, help="Scoped corpus[:source] (whole-graph if omitted).")
@click.option("--output", type=click.Path(dir_okay=False, path_type=Path), default=None)
def gate(
    name: str,
    target: str,
    sample_limit: int,
    scope: str | None,
    output: Path | None,
) -> None:
    """Evaluate a readiness gate over a scoped or whole-graph audit."""
    try:
        settings = Settings.model_validate({})
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

    try:
        policy = GatePolicyLoader(settings.gates_policy_path).load()
    except GatePolicyLoadError as exc:
        click.echo(f"Gate policy error: {exc}", err=True)
        sys.exit(2)

    audit_scope: AuditScope | None = None
    if scope is not None:
        try:
            catalog = CatalogLoader(settings.catalog_path).load()
            audit_scope = resolve_audit_scope(scope, catalog)
        except (ValueError, CatalogLoadError) as exc:
            click.echo(f"Scope error: {exc}", err=True)
            sys.exit(2)

    try:
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

    async def _run() -> Any:
        try:
            report = await AuditGraphUseCase(adapter).execute(
                audit_target, sample_limit, scope=audit_scope
            )
            if name in {g.name for g in policy.readiness_gates}:
                readiness_use_case = _build_readiness_gate_evaluator_use_case(settings, policy)
                return await readiness_use_case.execute(name, report, scope=scope)
            return GateEvaluatorUseCase(policy).evaluate(name, report)
        finally:
            if hasattr(adapter, "close"):
                await adapter.close()

    try:
        result = asyncio.run(_run())
    except UnknownGateError as exc:
        click.echo(f"Unknown gate: {exc}", err=True)
        sys.exit(10)
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

    payload = result.model_dump_json(indent=2)
    click.echo(payload)
    if output is not None:
        output.write_text(payload + "\n", encoding="utf-8")
    raise click.exceptions.Exit(result.exit_code)


def _build_evaluate_command_use_case(settings: Settings) -> EvaluateCommandUseCase:
    """Wire the evaluation layer use cases for the ``evaluate`` command."""
    dataset_port = JsonlManifestEvaluationDatasetLoader(settings.evaluation_manifest_path)
    baseline_port = JsonEvaluationBaselineLoader(settings.evaluation_baseline_dir)

    harness = EvaluationHarness(
        embedding=SentenceTransformerAdapter(settings),
        retrieval=BruteForceCandidateRetrieval(),
        thresholds=BandThresholds(),
        dataset_path=settings.resolution_dataset_path,
        manifest_path=settings.resolution_manifest_path,
        baseline_path=settings.resolution_baseline_report_path,
        output_path=settings.evaluation_dir / "resolution_metrics.json",
    )
    resolution_layer = EvaluateResolutionLayerUseCase(
        dataset_port=dataset_port,
        baseline_port=baseline_port,
        harness=harness,
    )

    retrieval_port = Neo4jRetrievalAdapter(settings)
    ragas_port = SubprocessRAGASRunner()
    retrieval_layer = EvaluateRetrievalLayerUseCase(
        dataset_port=dataset_port,
        retrieval_port=retrieval_port,
        ragas_port=ragas_port,
        baseline_port=baseline_port,
    )

    claim_port = LLMClaimValidator(settings)
    pairwise_port = LLMPairwiseJudge(settings)
    generation_layer = EvaluateGenerationLayerUseCase(
        dataset_port=dataset_port,
        retrieval_port=retrieval_port,
        claim_port=claim_port,
        pairwise_port=pairwise_port,
        ragas_port=ragas_port,
        baseline_port=baseline_port,
        settings=settings,
    )

    extraction_layer = EvaluateExtractionLayerUseCase()

    return EvaluateCommandUseCase(
        resolution_layer=resolution_layer,
        generation_layer=generation_layer,
        retrieval_layer=retrieval_layer,
        extraction_layer=extraction_layer,
    )


def _build_readiness_gate_evaluator_use_case(
    settings: Settings, policy: GatePolicy
) -> ReadinessGateEvaluatorUseCase:
    """Wire the readiness gate evaluator for the ``gate`` command."""
    dataset_port = JsonlManifestEvaluationDatasetLoader(settings.evaluation_manifest_path)
    baseline_port = JsonEvaluationBaselineLoader(settings.evaluation_baseline_dir)

    harness = EvaluationHarness(
        embedding=SentenceTransformerAdapter(settings),
        retrieval=BruteForceCandidateRetrieval(),
        thresholds=BandThresholds(),
        dataset_path=settings.resolution_dataset_path,
        manifest_path=settings.resolution_manifest_path,
        baseline_path=settings.resolution_baseline_report_path,
        output_path=settings.evaluation_dir / "resolution_metrics.json",
    )
    resolution_layer = EvaluateResolutionLayerUseCase(
        dataset_port=dataset_port,
        baseline_port=baseline_port,
        harness=harness,
    )

    retrieval_port = Neo4jRetrievalAdapter(settings)
    ragas_port = SubprocessRAGASRunner()
    retrieval_layer = EvaluateRetrievalLayerUseCase(
        dataset_port=dataset_port,
        retrieval_port=retrieval_port,
        ragas_port=ragas_port,
        baseline_port=baseline_port,
    )

    claim_port = LLMClaimValidator(settings)
    pairwise_port = LLMPairwiseJudge(settings)
    generation_layer = EvaluateGenerationLayerUseCase(
        dataset_port=dataset_port,
        retrieval_port=retrieval_port,
        claim_port=claim_port,
        pairwise_port=pairwise_port,
        ragas_port=ragas_port,
        baseline_port=baseline_port,
        settings=settings,
    )

    extraction_layer = EvaluateExtractionLayerUseCase()

    return ReadinessGateEvaluatorUseCase(
        gate_policy=policy,
        audit_evaluator=GateEvaluatorUseCase(policy),
        resolution_layer=resolution_layer,
        generation_layer=generation_layer,
        retrieval_layer=retrieval_layer,
        extraction_layer=extraction_layer,
    )


@cli.command("evaluate")
@click.option(
    "--layer",
    required=True,
    type=click.Choice(["resolution", "retrieval", "generation", "all"]),
    help="Evaluation layer to run.",
)
@click.option("--dataset", default=None, help="Reserved: dataset id override.")
@click.option("--baseline", default=None, help="Reserved: baseline path override.")
@click.option(
    "--output",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Write the JSON report to this file.",
)
@click.option(
    "--json-only",
    is_flag=True,
    default=False,
    help="Emit only JSON to stdout; suppress human summary.",
)
def evaluate(
    layer: str,
    dataset: str | None,
    baseline: str | None,
    output: Path | None,
    json_only: bool,
) -> None:
    """Run an evaluation layer and emit a JSON report."""
    del dataset, baseline  # reserved for future slicing; not wired in T-C.5
    try:
        settings = Settings.model_validate({})
    except Exception as exc:  # noqa: BLE001
        click.echo(f"Configuration error: {exc}", err=True)
        sys.exit(1)

    use_case = _build_evaluate_command_use_case(settings)
    try:
        report, exit_code = asyncio.run(use_case.execute(layer=layer))
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc

    payload = report.model_dump_json(indent=2)
    if not json_only:
        click.echo(
            f"Layer: {layer} -> status: {report.overall_status.value}",
            err=True,
        )
    click.echo(payload)
    if output is not None:
        output.write_text(payload + "\n", encoding="utf-8")
    raise click.exceptions.Exit(exit_code)


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


@cli.command("resolve-entities")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Analyze without persisting quarantine or applying merges.",
)
def resolve_entities(dry_run: bool) -> None:
    """Run semantic entity resolution over the active graph."""
    try:
        settings = Settings.model_validate({})
    except Exception as exc:  # noqa: BLE003
        click.echo(f"Configuration error: {exc}", err=True)
        sys.exit(1)

    async def _run() -> dict[str, Any]:
        use_case, closables = await build_resolve_entities_use_case(settings)
        try:
            result = await use_case.analyze(dry_run=dry_run)
        finally:
            for closable in closables:
                if hasattr(closable, "close"):
                    await closable.close()
        return {
            "strategy": "hybrid",
            "dry_run": dry_run,
            "auto_merge_groups": len(result.auto_merge_groups),
            "quarantine_records": len(result.quarantine_records),
            "no_merge_candidates": len(result.no_merge_candidates),
            "total_pairs_evaluated": result.total_pairs_evaluated,
            "merged_entities": sum(len(g.duplicate_ids) for g in result.auto_merge_groups),
        }

    try:
        summary = asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        click.echo(f"Resolution error: {exc}", err=True)
        sys.exit(3)

    click.echo(json.dumps(summary, indent=2))


@cli.group("quarantine")
def quarantine() -> None:
    """Human-review queue: list/render (read-only), enqueue/decide (T6b).

    ``enqueue`` only reads the graph and appends JSONL records;
    ``approve`` mutates behind the AGENTS.md §7.2 gate (fresh backup +
    approval file, explicit ``--seq`` values, never "approve all");
    ``reject`` records the mandatory ``--reason`` without touching the graph.
    """


@quarantine.command("list")
@click.option(
    "--all",
    "show_all",
    is_flag=True,
    default=False,
    help="Include decided records (default: pending only).",
)
@click.option(
    "--band",
    type=click.Choice(["medium", "high"]),
    default=None,
    help="Only records in this confidence band.",
)
@click.option(
    "--namespace",
    default=None,
    help="Only records touching this corpus:source namespace.",
)
@click.option(
    "--limit",
    type=click.IntRange(min=1),
    default=None,
    help="Maximum number of rows printed.",
)
@click.option(
    "--generic-only",
    is_flag=True,
    default=False,
    help=(
        "Only single-word labels (the generic-collision batch; unchanged T6a "
        "meaning — see --risk for the combined marker)."
    ),
)
@click.option(
    "--risk",
    type=click.Choice(["high", "medium", "none"]),
    default=None,
    help=(
        "Only rows at this combined risk level: high = single word AND ≥3 "
        "namespaces, medium = exactly one of the two (reason printed per row)."
    ),
)
@click.option("--json", "json_output", is_flag=True, default=False, help="Emit rows as JSON.")
def quarantine_list(
    show_all: bool,
    band: str | None,
    namespace: str | None,
    limit: int | None,
    generic_only: bool,
    risk: str | None,
    json_output: bool,
) -> None:
    """Print one row per quarantine record (pending unless --all).

    Two independent filters over the same rows: ``--generic-only`` keeps
    single-word labels (its approved T6a meaning), while ``--risk`` filters
    the combined marker — high = single word AND ≥3 namespaces, medium =
    exactly one of the two, none = neither.
    """
    try:
        settings = Settings.model_validate({})
    except Exception as exc:  # noqa: BLE001
        click.echo(f"Configuration error: {exc}", err=True)
        sys.exit(1)

    band_value = ConfidenceBand(band) if band is not None else None

    async def _run() -> list[QuarantineListRow]:
        use_case, closables = build_review_quarantine_use_case(settings)
        try:
            return await use_case.list_records(
                show_all=show_all,
                band=band_value,
                namespace=namespace,
                limit=limit,
                generic_only=generic_only,
                risk=RiskLevel(risk) if risk is not None else None,
            )
        finally:
            for closable in closables:
                if hasattr(closable, "close"):
                    await closable.close()

    try:
        rows = asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        click.echo(f"Quarantine list error: {exc}", err=True)
        sys.exit(3)

    if json_output:
        click.echo(
            json.dumps(
                [row.model_dump(mode="json") for row in rows],
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        click.echo(format_list(rows))


@quarantine.command("render")
@click.option("--seq", type=int, default=None, help="Quarantine record sequence number.")
@click.option(
    "--pair",
    nargs=2,
    default=None,
    metavar="ENTITY_ID ENTITY_ID",
    help="Two entity ids without a record (retro-audit of applied merges).",
)
@click.option("--json", "json_output", is_flag=True, default=False, help="Emit sheet as JSON.")
def quarantine_render(
    seq: int | None,
    pair: tuple[str, str] | None,
    json_output: bool,
) -> None:
    """Print the decision sheet for one record or one entity pair."""
    if (seq is None) == (pair is None):
        raise click.UsageError("Provide exactly one of --seq or --pair")

    try:
        settings = Settings.model_validate({})
    except Exception as exc:  # noqa: BLE001
        click.echo(f"Configuration error: {exc}", err=True)
        sys.exit(1)

    async def _run() -> DecisionSheet:
        use_case, closables = build_review_quarantine_use_case(settings)
        try:
            return await use_case.render_sheet(seq=seq, pair=pair)
        finally:
            for closable in closables:
                if hasattr(closable, "close"):
                    await closable.close()

    try:
        sheet = asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        click.echo(f"Quarantine render error: {exc}", err=True)
        sys.exit(3)

    if json_output:
        click.echo(sheet.model_dump_json(indent=2))
    else:
        click.echo(format_sheet(sheet))


# ── T6b: producer + gated decisions ─────────────────────────────────────────

#: Hours after which a backup no longer satisfies the AGENTS.md §7.2 gate
#: (same threshold as scripts-ops/repoint_merged_endpoint_edges.py).
STALE_BACKUP_HOURS = 24

#: Word the approval file must contain (same as the ops apply gate).
APPROVAL_WORD = "approve"


def _validate_approve_gates(
    *,
    backup: Path | None,
    approval: Path | None,
    allow_stale_backup: bool,
) -> dict[str, Any]:
    """Valida el gate §7.2 ANTES de escribir nada (sale con mensaje y código≠0).

    Mirror exacto de
    ``scripts-ops/repoint_merged_endpoint_edges.py::_validate_apply_gates``:
    mismo orden (approval → palabra → backup → antigüedad), misma redacción y
    mismos umbrales (``APPROVAL_WORD``, ``STALE_BACKUP_HOURS``).
    """
    if approval is None or not approval.exists():
        sys.exit("APPROVE abortado: falta --approval <archivo existente>")
    content = approval.read_text(encoding="utf-8").strip().lower()
    if APPROVAL_WORD not in content:
        sys.exit(f"APPROVE abortado: {approval} debe contener la palabra '{APPROVAL_WORD}'")

    if backup is None or not backup.exists():
        sys.exit("APPROVE abortado: falta --backup <json de backup fresco ya hecho>")
    age_hours = (datetime.now(UTC).timestamp() - backup.stat().st_mtime) / 3600
    size_mb = backup.stat().st_size / 1e6
    if age_hours > STALE_BACKUP_HOURS and not allow_stale_backup:
        sys.exit(
            f"APPROVE abortado: el backup tiene {age_hours:.1f} h (> {STALE_BACKUP_HOURS} h). "
            "Hacer uno fresco con scripts-ops/backup_only.py o pasar --allow-stale-backup."
        )
    return {
        "path": str(backup),
        "age_hours": round(age_hours, 2),
        "size_mb": round(size_mb, 1),
        "approval_file": str(approval),
    }


def _format_enqueue_result(result: EnqueueCrossNamespaceResult) -> str:
    """Render found/enqueued/skipped counts, the seq range and the honesty line."""
    seq_part = (
        f" · seq {result.seq_first}–{result.seq_last}"
        if result.seq_first is not None and result.seq_last is not None
        else ""
    )
    run_kind = " (dry-run: nothing written)" if result.dry_run else ""
    lines = [
        (
            f"cross-namespace enqueue: groups found: {result.groups_found} · "
            f"candidate pairs: {result.pairs_found}"
        ),
        f"  enqueued: {result.enqueued}{seq_part}{run_kind}",
        f"  skipped (already recorded): {result.skipped_existing}",
    ]
    if result.forced_existing:
        lines.append(f"  forced (already recorded): {result.forced_existing}")
    lines.append("  cosine: not computed (band=medium; evidence = S0 + S2 + three overlaps)")
    if result.failed:
        lines.append(f"  failed (unreadable entities): {result.failed}")
    return "\n".join(lines)


@quarantine.command("enqueue")
@click.option(
    "--cross-namespace",
    "cross_namespace",
    is_flag=True,
    default=False,
    help="Detect cross-namespace groups exactly as DUPLICATE_ENTITY_CROSS_NAMESPACE does.",
)
@click.option(
    "--limit",
    type=click.IntRange(min=1),
    default=None,
    help="Maximum new records this run (candidates ordered by description overlap desc).",
)
@click.option(
    "--generic-only",
    is_flag=True,
    default=False,
    help="Only single-word labels (the high-risk generic-collision batch).",
)
@click.option(
    "--namespace",
    default=None,
    help="Only pairs touching this corpus:source namespace.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Report what would be written without writing anything.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Re-enqueue pairs that already have a record (rejected pairs only return with it).",
)
@click.option(
    "--json", "json_output", is_flag=True, default=False, help="Emit the outcome as JSON."
)
def quarantine_enqueue(
    cross_namespace: bool,
    limit: int | None,
    generic_only: bool,
    namespace: str | None,
    dry_run: bool,
    force: bool,
    json_output: bool,
) -> None:
    """Produce pending records for cross-namespace duplicate candidates.

    Reads the graph and appends to the quarantine file only — the graph is
    never mutated. Idempotent: any pair that already has a record (whatever
    its decision) is skipped unless ``--force`` is given.
    """
    if not cross_namespace:
        raise click.UsageError("--cross-namespace is required (the only detection mode)")

    try:
        settings = Settings.model_validate({})
    except Exception as exc:  # noqa: BLE001
        click.echo(f"Configuration error: {exc}", err=True)
        sys.exit(1)

    async def _run() -> EnqueueCrossNamespaceResult:
        use_case, closables = build_enqueue_cross_namespace_use_case(settings)
        try:
            return await use_case.enqueue(
                limit=limit,
                generic_only=generic_only,
                namespace=namespace,
                dry_run=dry_run,
                force=force,
            )
        finally:
            for closable in closables:
                if hasattr(closable, "close"):
                    await closable.close()

    try:
        result = asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        click.echo(f"Quarantine enqueue error: {exc}", err=True)
        sys.exit(3)

    if json_output:
        click.echo(json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False))
    else:
        click.echo(_format_enqueue_result(result))


@quarantine.command("approve")
@click.option(
    "--seq",
    "seqs",
    type=int,
    multiple=True,
    help="Quarantine record seq (repeatable; explicit values only, never 'approve all').",
)
@click.option(
    "--backup",
    type=click.Path(path_type=Path),
    default=None,
    help="Fresh backup JSON required by the AGENTS.md §7.2 gate.",
)
@click.option(
    "--approval",
    type=click.Path(path_type=Path),
    default=None,
    help="Approval file containing the word 'approve' (AGENTS.md §7.2).",
)
@click.option(
    "--reviewer",
    default=None,
    help="Reviewer id (default: human:<current user>).",
)
@click.option(
    "--allow-stale-backup",
    is_flag=True,
    default=False,
    help=f"Permite un backup de mas de {STALE_BACKUP_HOURS} h.",
)
def quarantine_approve(
    seqs: tuple[int, ...],
    backup: Path | None,
    approval: Path | None,
    reviewer: str | None,
    allow_stale_backup: bool,
) -> None:
    """Approve explicit pending records and apply their merges (§7.2 gate).

    The gate (approval file with 'approve', backup ≤ 24 h) is validated
    before anything is written; every seq is validated before the first
    write, so a refusal leaves the quarantine file untouched.
    """
    if not seqs:
        raise click.UsageError("Provide at least one --seq (approvals are always explicit)")
    gate = _validate_approve_gates(
        backup=backup,
        approval=approval,
        allow_stale_backup=allow_stale_backup,
    )
    click.echo(
        f"gate §7.2 ok: backup {gate['path']} ({gate['age_hours']} h, "
        f"{gate['size_mb']} MB) · approval {gate['approval_file']}"
    )

    try:
        settings = Settings.model_validate({})
    except Exception as exc:  # noqa: BLE001
        click.echo(f"Configuration error: {exc}", err=True)
        sys.exit(1)
    resolved_reviewer = reviewer or f"human:{getpass.getuser()}"

    async def _run() -> list[QuarantineDecisionOutcome]:
        use_case, closables = await build_approve_quarantine_use_case(settings)
        try:
            return await use_case.approve_many(
                seqs=seqs,
                reviewer=resolved_reviewer,
                approver_for_apply=resolved_reviewer,
            )
        finally:
            for closable in closables:
                if hasattr(closable, "close"):
                    await closable.close()

    try:
        outcomes = asyncio.run(_run())
    except ResolutionError as exc:
        click.echo(f"Quarantine approve error: {exc}", err=True)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        click.echo(f"Quarantine approve error: {exc}", err=True)
        sys.exit(3)

    for outcome in outcomes:
        click.echo(f"seq {outcome.seq}: {outcome.decision} → ledger seq {outcome.ledger_seq}")
    click.echo("Reminder: run the scoped and global audits afterwards (not run automatically):")
    click.echo("  book-graph-rag audit --scope <corpus:source>  # per touched namespace")
    click.echo("  book-graph-rag audit  # global")


@quarantine.command("reject")
@click.option(
    "--seq",
    "seqs",
    type=int,
    multiple=True,
    help="Quarantine record seq (repeatable; explicit values only).",
)
@click.option(
    "--reason",
    default=None,
    help="Mandatory non-empty reason; stored in the record's review_note.",
)
@click.option(
    "--reviewer",
    default=None,
    help="Reviewer id (default: human:<current user>).",
)
def quarantine_reject(
    seqs: tuple[int, ...],
    reason: str | None,
    reviewer: str | None,
) -> None:
    """Reject explicit pending records; records the reason, no graph mutation."""
    if not seqs:
        raise click.UsageError("Provide at least one --seq (rejections are always explicit)")
    if reason is None or not reason.strip():
        raise click.UsageError("--reason is mandatory and must not be empty")

    try:
        settings = Settings.model_validate({})
    except Exception as exc:  # noqa: BLE001
        click.echo(f"Configuration error: {exc}", err=True)
        sys.exit(1)
    resolved_reviewer = reviewer or f"human:{getpass.getuser()}"

    async def _run() -> list[QuarantineDecisionOutcome]:
        use_case, closables = await build_approve_quarantine_use_case(settings)
        try:
            return use_case.reject(
                seqs=seqs,
                reviewer=resolved_reviewer,
                reason=reason,
            )
        finally:
            for closable in closables:
                if hasattr(closable, "close"):
                    await closable.close()

    try:
        outcomes = asyncio.run(_run())
    except ResolutionError as exc:
        click.echo(f"Quarantine reject error: {exc}", err=True)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        click.echo(f"Quarantine reject error: {exc}", err=True)
        sys.exit(3)

    for outcome in outcomes:
        click.echo(f"seq {outcome.seq}: {outcome.decision} (reason stored in review_note)")


@cli.group("ledger")
def ledger() -> None:
    """Merge-ledger operations (read-only unless ``--apply`` says otherwise).

    ``rollback`` prints the plan — entry, inferred inverse map, predicted edge
    census and fingerprint — without writing anything; ``--apply`` executes it
    behind the AGENTS.md §7.2 gate (fresh backup + approval file, explicit
    ``--seq`` values, never "all") and refuses when ``--expect-fingerprint``
    does not match the recomputed plan. ``--candidate`` (repeatable, one
    ``--seq`` only) restricts the rollback to a subset of the entry's
    candidates (partial rollback).
    """


@ledger.command("rollback")
@click.option(
    "--seq",
    "seqs",
    type=int,
    multiple=True,
    help="Ledger seq to roll back (repeatable; explicit values only, never 'all').",
)
@click.option(
    "--candidate",
    "candidates",
    multiple=True,
    help=(
        "Candidate entity id to reverse within the single --seq entry "
        "(repeatable; requires exactly one --seq)."
    ),
)
@click.option(
    "--apply",
    "apply_changes",
    is_flag=True,
    default=False,
    help="Execute the reviewed plan (mutates; §7.2 gate required). Default: read-only plan.",
)
@click.option(
    "--backup",
    type=click.Path(path_type=Path),
    default=None,
    help="Fresh backup JSON required by the AGENTS.md §7.2 gate.",
)
@click.option(
    "--approval",
    type=click.Path(path_type=Path),
    default=None,
    help="Approval file containing the word 'approve' (AGENTS.md §7.2).",
)
@click.option(
    "--allow-stale-backup",
    is_flag=True,
    default=False,
    help=f"Permite un backup de mas de {STALE_BACKUP_HOURS} h.",
)
@click.option(
    "--expect-fingerprint",
    default=None,
    help=(
        "sha256 printed by the reviewed dry-run; refuse if the recomputed "
        "plan differs (binds the review to the mutation)."
    ),
)
@click.option(
    "--reviewer",
    default=None,
    help="Reviewer id (default: human:<current user>); echoed in the apply report.",
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    default=False,
    help="Emit the plan (and the apply report) as JSON.",
)
def ledger_rollback(
    seqs: tuple[int, ...],
    candidates: tuple[str, ...],
    apply_changes: bool,
    backup: Path | None,
    approval: Path | None,
    allow_stale_backup: bool,
    expect_fingerprint: str | None,
    reviewer: str | None,
    json_output: bool,
) -> None:
    """Roll back explicit ledger seqs, restoring the original edge directions.

    Read-only by default: prints the plan with its predicted edge census,
    every ``both``/``unknown`` fallback (with the reason legacy behaviour will
    apply) and the fingerprint, writing nothing. ``--candidate`` restricts the
    plan (and the later apply) to a subset of the entry's candidates; the
    report marks such a plan ``partial n/m`` and the ``next:`` hint repeats
    the ``--candidate`` flags so ``--apply`` recomputes the same plan and
    therefore the same fingerprint. ``--apply`` validates the
    §7.2 gate FIRST (before any use-case call), recomputes the plan, refuses a
    mismatched or missing-when-given ``--expect-fingerprint`` before any
    write, applies through the existing RollbackMergeUseCase (entry copy with
    inferred directions), then prints the actual census and compares it with
    the prediction — exiting non-zero on any post-apply mismatch.
    """
    if candidates and len(seqs) != 1:
        raise click.UsageError(
            "--candidate selects candidates inside one ledger entry: a candidate "
            f"selection belongs to exactly one --seq (got {len(seqs)} --seq values)"
        )
    if not seqs:
        raise click.UsageError(
            "Provide at least one --seq (rollbacks are always explicit; there is no 'all')"
        )
    if apply_changes:
        gate = _validate_approve_gates(
            backup=backup,
            approval=approval,
            allow_stale_backup=allow_stale_backup,
        )
        click.echo(
            f"gate §7.2 ok: backup {gate['path']} ({gate['age_hours']} h, "
            f"{gate['size_mb']} MB) · approval {gate['approval_file']}"
        )

    try:
        settings = Settings.model_validate({})
    except Exception as exc:  # noqa: BLE001
        click.echo(f"Configuration error: {exc}", err=True)
        sys.exit(1)
    resolved_reviewer = reviewer or f"human:{getpass.getuser()}"

    async def _run() -> tuple[RollbackPlan, EdgeCensus | None, RollbackMeasurement | None]:
        plan_use_case, closables = build_plan_rollback_use_case(settings)
        try:
            plan = await plan_use_case.plan(seqs, candidates=candidates or None)
            if not apply_changes:
                return plan, None, None
            if expect_fingerprint is None:
                click.echo(
                    "note: --expect-fingerprint not given "
                    "(recommended: it binds the reviewed plan to this mutation)"
                )
            elif expect_fingerprint != plan.fingerprint:
                sys.exit(
                    f"FINGERPRINT abortado: reviewed {expect_fingerprint}, "
                    f"recomputed {plan.fingerprint} "
                    "(the ledger or the graph changed since review)"
                )
            else:
                click.echo(f"fingerprint ok: {plan.fingerprint}")
            census_before = await plan_use_case.read_census(plan)
            apply_use_case, apply_closables = build_rollback_apply_use_case(settings, plan)
            try:
                for entry_plan in plan.plans:
                    await apply_use_case.rollback(seq=entry_plan.seq)
                    click.echo(
                        f"seq {entry_plan.seq}: rollback applied "
                        "(compensating ledger entry appended)"
                    )
            finally:
                for closable in apply_closables:
                    if hasattr(closable, "close"):
                        await closable.close()
            measurement = await plan_use_case.measure(plan)
            return plan, census_before, measurement
        finally:
            for closable in closables:
                if hasattr(closable, "close"):
                    await closable.close()

    try:
        plan, census_before, measurement = asyncio.run(_run())
    except ResolutionError as exc:
        click.echo(f"Ledger rollback error: {exc}", err=True)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        click.echo(f"Ledger rollback error: {exc}", err=True)
        sys.exit(3)

    if not apply_changes:
        if json_output:
            click.echo(json.dumps(plan.model_dump(mode="json"), indent=2, ensure_ascii=False))
        else:
            click.echo(_format_rollback_plan(plan))
        return

    if census_before is None or measurement is None:
        sys.exit("Ledger rollback error: --apply produced no post-apply measurement")

    comparison = compare_census(plan, census_before, measurement)
    if json_output:
        click.echo(
            json.dumps(
                {
                    "plan": plan.model_dump(mode="json"),
                    "census_before": census_before.model_dump(mode="json"),
                    "census_after": measurement.census.model_dump(mode="json"),
                    "outcomes": [o.model_dump(mode="json") for o in measurement.outcomes],
                    "comparison": comparison.model_dump(mode="json"),
                    "reviewer": resolved_reviewer,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        click.echo(f"reviewer: {resolved_reviewer}")
        click.echo(_format_rollback_result(plan, census_before, measurement, comparison))
    if not comparison.passed:
        click.echo(
            "post-apply census MISMATCH: the graph does not match the reviewed plan "
            "(inspect the MISMATCH lines before doing anything else)",
            err=True,
        )
        sys.exit(1)
    click.echo("Reminder: run the scoped and global audits afterwards (not run automatically):")
    click.echo("  book-graph-rag audit --scope <corpus:source>  # per touched namespace")
    click.echo("  book-graph-rag audit  # global")


def _format_rollback_plan(plan: RollbackPlan) -> str:
    """Human dry-run: per-seq census, every fallback with its reason, fingerprint."""
    lines = ["ledger rollback plan (read-only: nothing written)"]
    for entry_plan in plan.plans:
        predicted = entry_plan.predicted
        lines.append(f"seq {entry_plan.seq}: canonical {entry_plan.entry.canonical_id}")
        selected = entry_plan.selected_candidates
        if selected is None:
            lines.append(f"  candidates: {' · '.join(entry_plan.entry.candidate_ids)}")
        else:
            all_candidates = entry_plan.entry.candidate_ids
            lines.append(
                f"  candidates: {' · '.join(selected)} "
                f"(partial {len(selected)}/{len(all_candidates)} of "
                f"{' · '.join(all_candidates)})"
            )
        lines.append(
            f"  restore: MENTIONS {predicted.mentions_restored} · "
            f"RELATED {predicted.related_restored} "
            f"(single-direction {predicted.directions_inferred} · "
            f"fallback both {predicted.fallback_both} · unknown {predicted.fallback_unknown})"
        )
        lines.append(
            f"  predicted mirrors: {predicted.predicted_mirrors} "
            "(mirrors only come from both/unknown fallbacks)"
        )
        lines.append(f"  affected entities: {len(entry_plan.affected_entities)}")
        for inference in entry_plan.inferences:
            if inference.fallback:
                lines.append(
                    f"  FALLBACK seq {inference.seq} {inference.edge_type} "
                    f"{inference.duplicate_entity_id} -> "
                    f"{inference.original_other_endpoint_id}: {inference.reason}"
                )
    totals = plan.predicted
    lines.append(
        f"totals: MENTIONS {totals.mentions_restored} · RELATED {totals.related_restored} "
        f"(single-direction {totals.directions_inferred} · "
        f"fallback both {totals.fallback_both} · unknown {totals.fallback_unknown})"
    )
    lines.append(f"predicted mirrors: {totals.predicted_mirrors}")
    lines.append(f"fingerprint: {plan.fingerprint}")
    command_parts = ["book-graph-rag", "ledger", "rollback"]
    for entry_plan in plan.plans:
        command_parts.append(f"--seq {entry_plan.seq}")
    for entry_plan in plan.plans:
        for candidate in entry_plan.selected_candidates or ():
            command_parts.append(f"--candidate {candidate}")
    command_parts.extend(
        [
            "--apply",
            "--backup",
            "<fresh.json>",
            "--approval",
            "<file>",
            "--expect-fingerprint",
            plan.fingerprint,
        ]
    )
    lines.append(f"next: {' '.join(command_parts)}")
    return "\n".join(lines)


def _format_rollback_result(
    plan: RollbackPlan,
    census_before: EdgeCensus,
    measurement: RollbackMeasurement,
    comparison: RollbackComparison,
) -> str:
    """Post-apply report: prediction vs actual census plus every drift line."""
    before = census_before
    after = measurement.census
    lines = [
        "census before -> after:",
        (
            f"  mentions restored: {comparison.mentions_restored} "
            f"(predicted {plan.predicted.mentions_restored})"
        ),
        (
            f"  related restored: {comparison.related_restored} "
            f"(predicted {plan.predicted.related_restored})"
        ),
        (
            f"  mirrors created: {comparison.mirrors_created} "
            f"(predicted {plan.predicted.predicted_mirrors})"
        ),
        (
            f"  MENTIONS total: {before.total_mentions} -> {after.total_mentions} · "
            f"RELATED total: {before.total_related} -> {after.total_related} · "
            f"merged_into: {before.merged_into_count} -> {after.merged_into_count}"
        ),
    ]
    if comparison.drift:
        lines.append(f"  drift: {len(comparison.drift)} mismatch(es)")
        lines.extend(f"    MISMATCH {item}" for item in comparison.drift)
    else:
        lines.append("  drift: none")
    return "\n".join(lines)


# ── T9a: human cross-namespace decision registry ───────────────────────────


def _decision_row(decision: CrossNamespaceDecision) -> dict[str, Any]:
    """The five fields ``decisions list`` promises: key, decision, reviewer,
    reason and batch — one shape for the text rows and the ``--json`` rows."""
    return {
        "key": f"{decision.seq}|{decision.candidate_id}",
        "decision": decision.decision.value,
        "reviewer": decision.decided_by,
        "reason": decision.reason,
        "batch": decision.batch,
    }


@cli.group("decisions")
def decisions() -> None:
    """Human cross-namespace decisions (T9a): append-only, no graph writes.

    ``record`` validates against the merge ledger FIRST (unknown seq, a
    candidate outside the entry and a compensating entry are refused with a
    non-zero exit and nothing appended) and appends one decision line; the
    canonical is taken from the ledger entry. No §7.2 gate and no graph
    mutation — like ``quarantine reject``, recording a decision is free.
    ``list`` prints the registry (``--json`` for machine-readable rows).
    """


@decisions.command("record")
@click.option("--seq", type=int, required=True, help="Merge-ledger seq the decision reviews.")
@click.option("--candidate", required=True, help="Candidate entity id of that ledger entry.")
@click.option(
    "--decision",
    "decision_kind",
    type=click.Choice([kind.value for kind in DecisionKind]),
    required=True,
    help="keep = merged identity stands; separate = distinct concepts, stop warning.",
)
@click.option("--reason", required=True, help="Mandatory non-empty reason (both kinds).")
@click.option("--batch", default=None, help="Optional review-batch label (e.g. 2B).")
@click.option("--reviewer", default=None, help="Reviewer id (default: human:<current user>).")
def decisions_record(
    seq: int,
    candidate: str,
    decision_kind: str,
    reason: str,
    batch: str | None,
    reviewer: str | None,
) -> None:
    """Record one keep/separate decision for a merge-ledger candidate.

    Fail closed: every ledger precondition is checked before anything is
    appended, so a refusal leaves the decisions file untouched.
    """
    if not reason.strip():
        raise click.UsageError("--reason is mandatory and must not be empty")
    try:
        settings = Settings.model_validate({})
    except Exception as exc:  # noqa: BLE001
        click.echo(f"Configuration error: {exc}", err=True)
        sys.exit(1)

    entry = JSONLMergeLedger(settings.merge_ledger_path).read_by_seq(seq)
    if entry is None:
        sys.exit(f"decisions record refused: seq {seq} is not in the merge ledger")
    if entry.rollback_of is not None:
        sys.exit(
            f"decisions record refused: seq {seq} is a compensating entry "
            f"(rollback_of={entry.rollback_of}); decide against the original seq"
        )
    if candidate not in entry.candidate_ids:
        sys.exit(
            f"decisions record refused: {candidate} is not a candidate of seq {seq} "
            f"(candidates: {', '.join(entry.candidate_ids)})"
        )

    decision = CrossNamespaceDecision(
        seq=seq,
        candidate_id=candidate,
        canonical_id=entry.canonical_id,
        decision=DecisionKind(decision_kind),
        decided_by=reviewer or f"human:{getpass.getuser()}",
        decided_at=datetime.now(UTC),
        reason=reason,
        batch=batch,
    )
    written = JSONLCrossNamespaceDecisions(settings.cross_namespace_decisions_path).append(decision)
    status = "recorded" if written else "already recorded (no-op)"
    click.echo(f"decision {status}: seq {seq} {candidate} -> {decision_kind}")


@decisions.command("list")
@click.option("--json", "json_output", is_flag=True, default=False, help="Emit rows as JSON.")
def decisions_list(json_output: bool) -> None:
    """Print every recorded decision (key, decision, reviewer, reason, batch)."""
    try:
        settings = Settings.model_validate({})
    except Exception as exc:  # noqa: BLE001
        click.echo(f"Configuration error: {exc}", err=True)
        sys.exit(1)

    records = JSONLCrossNamespaceDecisions(settings.cross_namespace_decisions_path).read_all()
    if json_output:
        rows = [_decision_row(record) for record in records]
        click.echo(json.dumps(rows, indent=2, ensure_ascii=False))
        return
    for record in records:
        row = _decision_row(record)
        parts = [row["key"], row["decision"], row["reviewer"], row["reason"]]
        if row["batch"] is not None:
            parts.append(f"batch {row['batch']}")
        click.echo(" · ".join(str(part) for part in parts))


def main() -> None:
    """Script entrypoint for `book-graph-rag` console command."""
    cli()


if __name__ == "__main__":
    main()
