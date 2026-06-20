from pathlib import Path

from pydantic import SecretStr, model_validator
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
    llm_max_retries: int = 3
    llm_retry_wait_multiplier: float = 1.0
    llm_retry_wait_max: float = 30.0
    # wait = min(multiplier * 2^(intento-1), max)  → 1s, 2s, 4s ... hasta 30s

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @model_validator(mode="after")
    def _overlap_menor_que_max_chunk(self) -> "Settings":
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
        return self
