# Fase 02 — Domain & Ports

## Estrategia de chunking — CHUNKING SEMÁNTICO (TORO)

> Decisiones previas:
> - Chunking semántico usando el TOC jerárquico del PDF (bookmarks).
> - Granularidad: **nivel más profundo disponible** por rama del TOC.
> - `PDF_MAX_CHUNK_SIZE` actúa como TECHO de seguridad: si una sección excede
>   ese tamaño, se subdivide por chars (conservando metadatos del padre).
> - Fallback a chunking por chars si el PDF no tiene TOC.

## Directiva para el Agente:
"Define el núcleo del dominio y los puertos.

    En domain/models.py, crea los modelos Pydantic:
      — Entidades de conocimiento (extraídas por el LLM): Entity, Relationship, KnowledgeGraphChunk (usa Literal para los tipos de nodos y relaciones).
      — Entidades editoriales (estructura inherente del libro, NO extraídas por el LLM): Book, Chapter, Section, PageRef. Estas modelan el TOC del PDF y dan citación de fuente (capítulo + sección + página) a cada chunk.
    En ports/llm_port.py, crea la interfaz LLMProviderPort con un método async def extract_graph(self, chunk: KnowledgeGraphChunk) -> KnowledgeGraphChunk (recibe el chunk CON metadatos editoriales, no texto plano).
    En ports/graph_db_port.py, crea GraphDatabasePort con métodos async def upsert_entities(...), async def upsert_relationships(...), async def upsert_editorial_structure(...) (este último persiste capítulo→sección→chunk con sus page refs).
    En ports/pdf_port.py, crea PDFReaderPort con un método def extract_chunks(self, file_path: str) -> Iterator[KnowledgeGraphChunk] que respeta el TOC del PDF y devuelve chunks con metadatos editoriales (Chapter, Section, PageRef)."

### Detalle de entidades editoriales (referencia para el agente)

```python
class PageRef(BaseModel):
    """Rango de páginas que ocupa un chunk dentro del PDF."""
    model_config = ConfigDict(frozen=True)
    start: int
    end: int

class Chapter(BaseModel):
    model_config = ConfigDict(frozen=True)
    number: int | None        # ej. 1, 2, 3 — None para prefacios/índices
    title: str
    page_start: int

class Section(BaseModel):
    model_config = ConfigDict(frozen=True)
    chapter_number: int | None
    level: int                # profundidad en el TOC: 2 = sección, 3 = subsección
    title: str
    page_start: int
    parent_section_title: str | None  # título de la sección padre (None si es hija directa de capítulo)
```

> `Chapter` y `Section` son INMUTABLES (frozen=True) porque representan
> estructura heredada del libro; no mutan en runtime.

---

## ✅ Criterios de Aceptación (Definition of Done)

> No marcar esta fase como completada hasta que TODOS los AC pasen.
> Estos AC son obligatorios y prevalecen sobre cualquier "se ve bien".

- **AC-02.1 — Validación de modelos de conocimiento:** Instanciar un `Entity`
  con un `type` fuera del `Literal` definido (ej. `type="COSO_INVENTADO"`) debe
  lanzar `ValidationError`.
- **AC-02.2 — Inmutabilidad de entidades editoriales:** Instanciar `Chapter`
  o `Section` sin los campos obligatorios (ej. `title`) lanza `ValidationError`.
  Intentar mutar un campo (`.title = "otro"`) lanza `ValidationError` por
  `frozen=True`.
- **AC-02.3 — Inmutabilidad de puertos:** Intentar instanciar directamente
  `LLMProviderPort()`, `GraphDatabasePort()` o `PDFReaderPort()` debe lanzar
  `TypeError` (son clases abstractas — `abc.ABC` con `@abstractmethod`).
- **AC-02.4 — Asincronía correcta:** Los métodos de `LLMProviderPort` y
  `GraphDatabasePort` están decorados con `async def`. El método de
  `PDFReaderPort` es `def` (síncrono) y la anotación de retorno es
  `Iterator[KnowledgeGraphChunk]` (NO `Iterator[str]`, NO `list[str]`).
- **AC-02.5 — Aislamiento del dominio:** `uv run python scripts/validate_architecture.py`
  termina con código de salida 0 (sin violaciones).