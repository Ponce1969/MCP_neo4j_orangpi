# Fase 01 — Foundation

## Directiva para el Agente:
"Configura el entorno base.

    Inicializa uv init.
    Añade dependencias: pydantic, pydantic-settings, instructor, neo4j, pymupdf, tenacity, click (para CLI).
    Añade dependencias de dev: mypy, ruff, pytest, pre-commit.
    Configura pyproject.toml para que mypy sea estricto (strict = true, disallow_untyped_defs = true) y ruff use las reglas E, F, I, N, UP, B, A, C4, PT, RET, SIM.
    Crea el archivo .env.example con las variables necesarias (ej. NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD, LLM_API_KEY, LLM_MODEL_NAME).
    Crea la clase Settings en src/book_graph_rag/domain/config.py usando pydantic-settings y SecretStr como se detalla abajo."

> **🔧 Corrección del arquitecto:** `Settings` va en `src/book_graph_rag/config.py` (raíz del paquete), **NO** en `domain/config.py`. Motivo: `pydantic_settings` es una dependencia externa (paquete distinto de `pydantic`), y la regla 2 del AGENTS.md prohíbe importar librerías externas en `domain`. La configuración de arranque no es lógica de negocio, es infraestructura. Mantenerla fuera de `domain/` preserva la pureza del dominio y deja pasar el `validate_architecture.py` sin contradicción.

### Política de inyección (POLÍTICA — documentar y respetar)

En este proyecto hay **dos patrones válidos conviviendo** y NINGÚN agente debe mezclarlos:

- **Adaptadores (`infrastructure/`)** reciben `Settings` en su `__init__`.
  Viven en el borde de infra y pueden conocer el mecanismo de carga de config.
  → `PDFAdapter(settings)`, `LLMAdapter(settings)`, `Neo4jAdapter(settings)`.

- **Casos de uso (`application/`)** reciben **primitivos** en su `__init__`.
  Viven en el núcleo puro y NUNCA dependen de `Settings` (eso sería acoplar
  application a infraestructura). Lo que necesitan se les pasa como `int`,
  `float`, etc., y la CLI se encarga de extraerlo de `Settings`.
  → `IndexBookUseCase(llm_port, graph_db_port, pdf_port, max_concurrency=3, batch_size=5)`.

> No usar `Settings` dentro de `application/`. `validate_architecture.py`
> podría ampliarse para atrapar esto; mientras tanto, el principio es manual
> y la revisión por PR lo debe hacer cumplir.

Snippet de referencia para el agente (Settings):
```python
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
    pdf_max_chunk_size: int = 1500   # techo: si una sección > esto, sub-dividir
    pdf_chunk_overlap: int = 150     # overlap de la sub-división

    # ── Orquestación (consumidos por IndexBookUseCase como PRIMITIVOS) ─
    llm_max_concurrency: int = 3   # tope de llamadas LLM concurrentes
    processing_batch_size: int = 5  # tamaño de lote del caso de uso

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
```

> **Nota técnica importante:** NO usar `@field_validator("pdf_chunk_overlap")`
> con `info.data["pdf_max_chunk_size"]`. En Pydantic v2 el orden de validación de
> campos depende del orden de *definición*, por lo que `info.data` puede no
> contener `pdf_max_chunk_size` todavía — la validación se silenciaría. Usar
> siempre `@model_validator(mode="after")` para validaciones que cruzan campos.

---

## ✅ Criterios de Aceptación (Definition of Done)

> No marcar esta fase como completada hasta que TODOS los AC pasen.
> Estos AC son obligatorios y prevalecen sobre cualquier "se ve bien".

- **AC-01.1 — Validación de entorno:** `uv run ruff check .` y `uv run mypy .` devuelven cero errores.
- **AC-01.2 — Fail-Fast de configuración:** Ejecutar
  `python -c "from book_graph_rag.config import Settings; Settings()"`
  sin un archivo `.env` presente debe lanzar una `pydantic.ValidationError`.

  > 🔧 **Ajuste del arquitecto:** el path del import es `book_graph_rag.config` (no `book_graph_rag.domain.config`), consistente con la corrección 🔧 de arriba (Settings fuera de `domain/`).

- **AC-01.3 — Carga correcta de secretos:** Con un `.env` válido,
  `Settings().neo4j_password.get_secret_value()` retorna el string correcto,
  y `repr(Settings().neo4j_password)` / `str(...)` NO exponen el valor plano
  (deben mostrar `SecretStr('**********')` o equivalente enmascarado).
- **AC-01.4 — Estructura de carpetas:** Existen las carpetas
  `src/book_graph_rag/{domain,ports,application,infrastructure}/`,
  cada una con su `__init__.py`.