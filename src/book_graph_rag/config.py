from pathlib import Path

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # ── Infraestructura externa ────────────────────────────────────────
    neo4j_uri: str
    neo4j_user: str
    neo4j_password: SecretStr
    llm_api_key: SecretStr | None = None  # None si usamos Ollama local
    llm_base_url: str = "http://localhost:11434/v1"
    llm_model_name: str = "llama3:70b"

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

    # ── Community summaries (REQ-GR.1) ────────────────────────────────────
    community_model_name: str | None = None  # defaults to llm_model_name
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
            raise ValueError(
                f"text2cypher_timeout ({value}) debe estar entre 1 y 60"
            )
        return value

    @field_validator("max_cluster_size")
    @classmethod
    def _validate_max_cluster_size(cls, value: int) -> int:
        if value < 1:
            raise ValueError(
                f"max_cluster_size ({value}) debe ser mayor o igual a 1"
            )
        return value

    @field_validator("summary_max_concurrency")
    @classmethod
    def _validate_summary_max_concurrency(cls, value: int) -> int:
        if value < 1:
            raise ValueError(
                f"summary_max_concurrency ({value}) debe ser mayor o igual a 1"
            )
        return value

    @field_validator("community_max_calls")
    @classmethod
    def _validate_community_max_calls(cls, value: int) -> int:
        if value < 1:
            raise ValueError(
                f"community_max_calls ({value}) debe ser mayor o igual a 1"
            )
        return value

    @field_validator("summary_chunk_tokens")
    @classmethod
    def _validate_summary_chunk_tokens(cls, value: int) -> int:
        if not 1000 <= value <= 60000:
            raise ValueError(
                f"summary_chunk_tokens ({value}) debe estar entre 1000 y 60000"
            )
        return value

    @field_validator("llm_instructor_max_retries")
    @classmethod
    def _validate_llm_instructor_max_retries(cls, value: int) -> int:
        if not 0 <= value <= 10:
            raise ValueError(
                f"llm_instructor_max_retries ({value}) debe estar entre 0 y 10"
            )
        return value

    @model_validator(mode="after")
    def _validate_settings(self) -> "Settings":
        # Validación cross-field: requiere AMBOS valores ya validados.
        # En Pydantic v2 el orden de validación de @field_validator depende del
        # orden de definición, lo que vuelve frágil info.data.get(...).
        # model_validator(mode="after") se ejecuta cuando todos los campos
        # ya tienen su valor final — robusto ante reordenamientos de Settings.
        if self.community_model_name is None:
            self.community_model_name = self.llm_model_name
        if self.pdf_chunk_overlap >= self.pdf_max_chunk_size:
            raise ValueError(
                f"pdf_chunk_overlap ({self.pdf_chunk_overlap}) debe ser "
                f"estrictamente menor que pdf_max_chunk_size "
                f"({self.pdf_max_chunk_size})"
            )
        if not 1 <= self.mcp_port <= 65535:
            raise ValueError(
                f"mcp_port ({self.mcp_port}) debe ser entre 1 y 65535"
            )
        if self.mcp_log_retention_days < 1:
            raise ValueError(
                f"mcp_log_retention_days ({self.mcp_log_retention_days}) "
                f"debe ser mayor o igual a 1"
            )
        return self
