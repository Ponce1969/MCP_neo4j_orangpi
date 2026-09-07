import re
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_SEMVER_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")
_ISO_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

LLMRole = Literal["graph", "query"]

_APPROVED_EMBEDDING_MODELS = frozenset({
    "paraphrase-multilingual-MiniLM-L12-v2",
    "distiluse-base-multilingual-cased-v2",
    "all-MiniLM-L6-v2",
})


def validate_llm_provider_settings(
    settings: "Settings", *, roles: tuple[LLMRole, ...] = ("graph", "query")
) -> None:
    """Ensure each selected LLM role has a provider URL and model name.

    API keys intentionally are not required because local OpenAI-compatible
    providers may not authenticate requests.
    """
    missing: list[str] = []
    for role in roles:
        prefix = role.upper()
        if not getattr(settings, f"{role}_llm_base_url").strip():
            missing.append(f"{prefix}_LLM_BASE_URL")
        if not getattr(settings, f"{role}_llm_model_name").strip():
            missing.append(f"{prefix}_LLM_MODEL_NAME")

    if missing:
        missing_settings = ", ".join(missing)
        raise ValueError(f"Missing required LLM settings: {missing_settings}")


class Settings(BaseSettings):
    # ── Infraestructura externa ────────────────────────────────────────
    neo4j_uri: str
    neo4j_user: str
    neo4j_password: SecretStr
    neo4j_database: str = "neo4j"
    # Graph construction and community summaries use this independent provider.
    graph_llm_api_key: SecretStr | None = None  # None for local providers
    graph_llm_base_url: str = ""
    graph_llm_model_name: str = ""
    # Text-to-Cypher, relevance scoring, and answer composition use this provider.
    query_llm_api_key: SecretStr | None = None  # None for local providers
    query_llm_base_url: str = ""
    query_llm_model_name: str = ""

    # ── Procesamiento de PDF (consumidos por PDFAdapter) ────────────────
    # Chunking semántico TORO: el driver principal es el TOC del PDF.
    # Estos valores son TECHO de seguridad cuando una sección del TOC es
    # demasiado larga para un solo chunk; se subdivide por chars con overlap.
    pdf_max_chunk_size: int = 1500  # techo: si una sección > esto, sub-dividir
    pdf_chunk_overlap: int = 150  # overlap de la sub-división

    # ── Orquestación (consumidos por IndexBookUseCase como PRIMITIVOS) ─
    llm_max_concurrency: int = 3  # tope de llamadas LLM concurrentes
    processing_batch_size: int = 5  # tamaño de lote del caso de uso
    dead_letter_path: Path = Path("data/dead_letter.log")  # chunks fallidos
    relationship_orphan_policy: Literal["fail_loud", "log_orphan"] = "log_orphan"
    dead_letter_path_orphans: Path = Path("data/dead_letter_orphans.jsonl")

    # ── Catálogo de namespaces (STAGE 1) ────────────────────────────────
    catalog_path: Path = Path("catalog.yaml")

    # ── Canonicalización de entidades (REQ-CANON-04) ─────────────────────
    canonical_match_mode: Literal["slug", "fuzzy"] = "slug"
    canonical_fuzzy_threshold: float = 0.92
    canonical_stoplist: list[str] = Field(default_factory=list)

    # ── Reintentos (consumidos por LLMAdapter — backoff EXPONENCIAL) ───
    llm_max_retries: int = 5
    llm_retry_wait_multiplier: float = 1.0
    llm_retry_wait_max: float = 30.0
    # wait = min(multiplier * 2^(intento-1), max)  → 1s, 2s, 4s ... hasta 30s

    # ── MCP server (Fase 07) ────────────────────────────────────────────
    mcp_port: int = 8003
    mcp_log_path: Path = Path("logs/mcp_queries.jsonl")
    mcp_log_retention_days: int = 7

    # ── Text2Cypher fallback (REQ-GR.4) ───────────────────────────────────
    text2cypher_timeout: int = 10  # seconds, whole pipeline budget

    # ── Checkpoint / resumable indexing (Phase 2) ─────────────────────────
    checkpoint_enabled: bool = True
    checkpoint_stale_lease_seconds: int = 300
    checkpoint_max_attempts: int = 3
    pipeline_version: str = "1.0.0"
    schema_version: str = "1.0.0"
    graph_llm_model_date: str = "2026-09-01"
    # Distinct path for re-addressable failed-chunk dead-letter records.
    dead_letter_path_chunks: Path = Path("data/dead_letter_chunks.jsonl")

    # ── Phase 3: Semantic Entity Resolution (NEW) ─────────────────────────
    embedding_model_id: str = "paraphrase-multilingual-MiniLM-L12-v2"
    embedding_local_path: str | None = None
    embedding_input_variant: Literal["A", "B"] = "A"
    embedding_top_k: int = 20
    embedding_min_similarity: float = 0.60
    embedding_dim: int | None = None
    band_high_cosine: float = 0.90
    band_high_context: float = 0.50
    band_medium_cosine: float = 0.80
    band_conflict_floor: float = 0.10
    candidate_retrieval_strategy: Literal["brute_force", "neo4j_vector"] = "brute_force"
    vector_index_name: str = "entity_embedding_index"
    resolution_dataset_path: Path = Path("tests/fixtures/resolution/pairs.yaml")
    resolution_manifest_path: Path = Path("tests/fixtures/resolution/manifest.json")
    resolution_baseline_report_path: Path = Path("tests/fixtures/resolution/baseline_report.json")
    quarantine_path: Path = Path("data/resolution/quarantine.jsonl")
    merge_ledger_path: Path = Path("data/resolution/merge_ledger.jsonl")
    merge_embedding_cache_path: Path = Path("data/resolution/entity_embeddings.jsonl")
    resolution_schema_version: str = "1.0.0"
    ledger_genesis_sha256: str = "0" * 64
    allow_extra_embedding_model: bool = False

    # ── Community summaries (REQ-GR.1) ────────────────────────────────────
    max_cluster_size: int = 10
    summary_max_concurrency: int = 3  # max concurrent LLM calls for summarization
    community_max_calls: int = 150  # hard guard on total community summaries per run
    # Minimum seconds to wait between LLM calls for community summaries.
    # Set to 12.0 for NVIDIA NIM free tier (~5 RPM limit).
    # Set to 0.0 to disable throttling (local Ollama or paid tiers).
    summary_request_delay: float = 0.0

    # Max input tokens per summary LLM call. Communities larger than this are
    # split into chunks (each summarized, then combined) so a single call never
    # exceeds the model context window. DeepSeek-chat has a 64K context; keep a
    # safe margin for the generated summary. ~12000 tokens ≈ 48K chars.
    summary_chunk_tokens: int = 12000

    # Instructor internal retries for JSON self-healing only. When the LLM returns
    # malformed JSON (trailing characters, etc.), instructor re-prompts with the
    # parse error so it can recover. Keep this LOW (1): transport errors (503/429/
    # timeout) are retried by tenacity with exponential backoff, and the OpenAI SDK
    # client is configured with max_retries=0. A high value here would burst retries
    # immediately on a saturated endpoint, worsening 503 storms.
    llm_instructor_max_retries: int = 1

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator("text2cypher_timeout")
    @classmethod
    def _validate_text2cypher_timeout(cls, value: int) -> int:
        if not 1 <= value <= 60:
            raise ValueError(f"text2cypher_timeout ({value}) debe estar entre 1 y 60")
        return value

    @field_validator("max_cluster_size")
    @classmethod
    def _validate_max_cluster_size(cls, value: int) -> int:
        if value < 1:
            raise ValueError(f"max_cluster_size ({value}) debe ser mayor o igual a 1")
        return value

    @field_validator("summary_max_concurrency")
    @classmethod
    def _validate_summary_max_concurrency(cls, value: int) -> int:
        if value < 1:
            raise ValueError(f"summary_max_concurrency ({value}) debe ser mayor o igual a 1")
        return value

    @field_validator("community_max_calls")
    @classmethod
    def _validate_community_max_calls(cls, value: int) -> int:
        if value < 1:
            raise ValueError(f"community_max_calls ({value}) debe ser mayor o igual a 1")
        return value

    @field_validator("summary_chunk_tokens")
    @classmethod
    def _validate_summary_chunk_tokens(cls, value: int) -> int:
        if not 1000 <= value <= 60000:
            raise ValueError(f"summary_chunk_tokens ({value}) debe estar entre 1000 y 60000")
        return value

    @field_validator("llm_instructor_max_retries")
    @classmethod
    def _validate_llm_instructor_max_retries(cls, value: int) -> int:
        if not 0 <= value <= 10:
            raise ValueError(f"llm_instructor_max_retries ({value}) debe estar entre 0 y 10")
        return value

    @field_validator("canonical_fuzzy_threshold")
    @classmethod
    def _validate_canonical_fuzzy_threshold(cls, value: float) -> float:
        if not 0.5 <= value <= 1.0:
            raise ValueError(f"canonical_fuzzy_threshold ({value}) debe estar entre 0.5 y 1.0")
        return value

    @field_validator("checkpoint_stale_lease_seconds")
    @classmethod
    def _validate_checkpoint_stale_lease_seconds(cls, value: int) -> int:
        if not 10 <= value <= 86_400:
            raise ValueError(
                f"checkpoint_stale_lease_seconds ({value}) must be between 10 and 86400"
            )
        return value

    @field_validator("checkpoint_max_attempts")
    @classmethod
    def _validate_checkpoint_max_attempts(cls, value: int) -> int:
        if not 1 <= value <= 10:
            raise ValueError(f"checkpoint_max_attempts ({value}) must be between 1 and 10")
        return value

    @field_validator("pipeline_version", "schema_version")
    @classmethod
    def _validate_semantic_version(cls, value: str) -> str:
        if not _SEMVER_PATTERN.match(value):
            raise ValueError(f"version ({value!r}) must match semver x.y.z")
        return value

    @field_validator("graph_llm_model_date")
    @classmethod
    def _validate_graph_llm_model_date(cls, value: str) -> str:
        if not _ISO_DATE_PATTERN.match(value):
            raise ValueError(f"graph_llm_model_date ({value!r}) must be yyyy-mm-dd")
        try:
            datetime.strptime(value, "%Y-%m-%d")  # noqa: DTZ007
        except ValueError as exc:
            raise ValueError(f"graph_llm_model_date ({value!r}) is not a valid date") from exc
        return value

    @field_validator("embedding_top_k")
    @classmethod
    def _validate_embedding_top_k(cls, value: int) -> int:
        if not 1 <= value <= 100:
            raise ValueError(f"embedding_top_k ({value}) must be between 1 and 100")
        return value

    @field_validator("embedding_min_similarity")
    @classmethod
    def _validate_embedding_min_similarity(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError(
                f"embedding_min_similarity ({value}) must be between 0.0 and 1.0"
            )
        return value

    @field_validator(
        "band_high_cosine",
        "band_high_context",
        "band_medium_cosine",
        "band_conflict_floor",
    )
    @classmethod
    def _validate_band_threshold_range(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"band threshold ({value}) must be between 0.0 and 1.0")
        return value

    @model_validator(mode="after")
    def _validate_settings(self) -> "Settings":
        # Validación cross-field: requiere AMBOS valores ya validados.
        # En Pydantic v2 el orden de validación de @field_validator depende del
        # orden de definición, lo que vuelve frágil info.data.get(...).
        # model_validator(mode="after") se ejecuta cuando todos los campos
        # ya tienen su valor final — robusto ante reordenamientos de Settings.
        if self.pdf_chunk_overlap >= self.pdf_max_chunk_size:
            raise ValueError(
                f"pdf_chunk_overlap ({self.pdf_chunk_overlap}) debe ser "
                f"estrictamente menor que pdf_max_chunk_size "
                f"({self.pdf_max_chunk_size})"
            )
        if not 1 <= self.mcp_port <= 65535:
            raise ValueError(f"mcp_port ({self.mcp_port}) debe ser entre 1 y 65535")
        if self.mcp_log_retention_days < 1:
            raise ValueError(
                f"mcp_log_retention_days ({self.mcp_log_retention_days}) debe ser mayor o igual a 1"
            )
        if self.band_high_cosine <= self.band_medium_cosine:
            raise ValueError(
                f"band_high_cosine ({self.band_high_cosine}) must be strictly greater "
                f"than band_medium_cosine ({self.band_medium_cosine})"
            )
        if (
            not self.allow_extra_embedding_model
            and self.embedding_model_id not in _APPROVED_EMBEDDING_MODELS
        ):
            approved = ", ".join(sorted(_APPROVED_EMBEDDING_MODELS))
            raise ValueError(
                f"embedding_model_id ({self.embedding_model_id!r}) is not in the "
                f"approved list: {approved}. "
                "Set allow_extra_embedding_model=True to bypass this guard."
            )
        if self.candidate_retrieval_strategy == "neo4j_vector" and self.embedding_dim is None:
            raise ValueError(
                "embedding_dim is required when candidate_retrieval_strategy is 'neo4j_vector'"
            )
        return self
