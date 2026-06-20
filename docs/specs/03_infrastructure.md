# Fase 03 — Infrastructure

## Directiva para el Agente:
"Implementa los adaptadores de infraestructura.

    PDF Adapter: Implementa PDFReaderPort usando PyMuPDF (fitz). Estrategia de chunking: SEMÁNTICO usando el TOC del PDF (doc.get_toc()), bajando hasta el nivel más profundo disponible por rama. Cada chunk lleva metadatos editoriales (Chapter, Section, PageRef). Si una sección excede settings.pdf_max_chunk_size, se subdivide por chars con la función `chunk_text` (de abajo), conservando los metadatos del padre. Fallback: si el PDF no tiene TOC, usar chunk_text sobre todo el texto con settings.pdf_max_chunk_size y settings.pdf_chunk_overlap. Lee `pdf_max_chunk_size` y `pdf_chunk_overlap` de `Settings`.
    LLM Adapter: Implementa LLMProviderPort usando instructor y openai (AsyncOpenAI). Recibe el chunk con metadatos editoriales; el prompt del LLM debe incluir el contexto bibliográfico (capítulo + sección + página) para que las entidades extraídas tengan citación de fuente.
        Regla de oro: El adaptador debe recibir el Settings en su __init__. Debe configurar el cliente de OpenAI para que apunte a settings.llm_base_url (para que funcione con Ollama/Llama-3 o OpenAI sin cambiar el código).
        Usa tenacity para reintentos con backoff EXPONENCIAL (leer `llm_max_retries`, `llm_retry_wait_multiplier`, `llm_retry_wait_max` de Settings).
    Neo4j Adapter: Implementa GraphDatabasePort usando el driver asíncrono de neo4j. Usa MERGE en Cypher para evitar duplicados. Persiste tanto la estructura editorial (capítulo→sección→chunk) como las entidades de conocimiento extraídas. Asegúrate de deserializar el SecretStr de los settings al crear el driver."

> **🔧 Corrección del arquitecto:** Cuando `settings.llm_api_key` sea `None` (modo Ollama local), el SDK de `openai` exige un `api_key` no-None o levanta en runtime, no en arranque. Instanciar `AsyncOpenAI(base_url=..., api_key=settings.llm_api_key.get_secret_value() if settings.llm_api_key else "ollama")`. El string `"ollama"` es ignorado por Ollama; así no rompe el Fail-Fast y queda documentado el modo local.

### Chunking Semántico — algoritmo de referencia

El `PDFAdapter` sigue este flujo:

1. **Leer el TOC** via `doc.get_toc()`. Cada entry es `(level, title, page_number)`.
2. **Construir un árbol jerárquico** agrupando entries por nivel (L1→L2→L3). Las entries de "indumentaria" (Foreword, Contributors, TOC, Preface) se marcan como `front_matter` (no se indexan como capítulos numerados; se saltean o se tratan como capítulo 0).
3. **Para cada rama**, identificar el `Chapter` (capítulo numerado más cercano hacia arriba) y la cadena de `Section`s hasta el **nivel más profundo disponible**.
4. **Determinar el rango de páginas** de cada nodo-leaf del TOC: desde su `page_number` hasta el `page_number` del siguiente bookmark hermano (o fin del libro).
5. **Extraer el texto** de ese rango de páginas con `doc[page].get_text()`.
6. **Si `len(texto) > settings.pdf_max_chunk_size`**: sub-dividir con `chunk_text` (de abajo), conservando los metadatos editoriales del chunk padre. Cada sub-chunk hereda `Chapter`, `Section` y `PageRef` (con offset relativo al starting page).
7. **Si el PDF no tiene TOC** (`doc.get_toc()` retorna `[]`): fallback a `chunk_text` sobre todo el texto concatenado,Chunks sin `Chapter`/`Section`, solo `PageRef`.

### Fórmula de sub-división (fallback / secciones largas)

Esta función se usa cuando una sección del TOC excede `pdf_max_chunk_size`. NO es la estrategia principal — es la válvula de seguridad:

```python
def chunk_text(text: str, chunk_size: int, overlap: int) -> list[str]:
    chunks: list[str] = []
    step = chunk_size - overlap
    if step <= 0:
        raise ValueError(
            f"step (chunk_size - overlap) debe ser > 0; "
            f"recibido chunk_size={chunk_size}, overlap={overlap}"
        )
    for start in range(0, len(text), step):
        # Guard: si start >= len(text) el chunk sería vacío (edge case cuando
        # len(text) es múltiplo de step). Lo descartamos.
        if start >= len(text):
            break
        chunks.append(text[start : start + chunk_size])
    return chunks
```

**Ejemplo verificable** (happy path, `len(text)=1000`):
- `chunk_size=500`, `overlap=100` → `step = 400`
- `range(0, 1000, 400)` → `[0, 400, 800]`
- 3 chunks: `[0:500]`, `[400:900]`, `[800:1000]` ✅

**Edge case** (`len(text)=800`): el guard descarta el chunk vacío → 2 chunks ✅.

---

## ✅ Criterios de Aceptación (Definition of Done)

> No marcar esta fase como completada hasta que TODOS los AC pasen.
> Estos AC son obligatorios y prevalecen sobre cualquier "se ve bien".
> Para los AC que tocan I/O real (Neo4j/Ollama), se validan contra servicios
> locales levantados (Docker + Ollama). En CI se ejecutan con fakes/stand-ins.

- **AC-03.1 — Inyección de dependencias:** Los tres adaptadores
  (`PDFAdapter`, `LLMAdapter`, `Neo4jAdapter`) reciben `Settings` como único
  parámetro obligatorio en su `__init__`. Sin `Settings`, no se construyen.
- **AC-03.2 — Chunking semántico (PDF):** Al procesar un PDF CON TOC, el
  adaptador devuelve chunks con metadatos editoriales (`Chapter`, `Section`,
  `PageRef`). Verificable con el libro real: cada chunk tiene `chapter_number`
  y `section.title` no-None (excepto front matter). El número total de chunks
  es >= al número de bookmarks hoja del TOC. **Sub-división/secciones largas:**
  al procesar un texto de 5000 chars con `pdf_max_chunk_size=1500` y
  `pdf_chunk_overlap=150`, la función `chunk_text` devuelve 4 chunks (no 3 con
  uno vacío, no 5). **Edge case guard:** con `len(text)=3000` (múltiplo de
  step=1350) el guard descarta el chunk vacío → 3 chunks.
- **AC-03.3 — Fallback sin TOC:** Si el PDF no tiene `get_toc()` (devuelve []),
  el adaptador cae a chunking por chars puro: usa `chunk_text` sobre el texto
  completo, chunks SIN `Chapter`/`Section` (solo `PageRef`).
- **AC-03.4 — Reintentos con backoff exponencial (LLM):** Si el LLM falla las
  primeras 2 veces, el adaptador reintenta automáticamente gracias a `tenacity`
  con `wait_exponential(multiplier=settings.llm_retry_wait_multiplier,
  max=settings.llm_retry_wait_max)` y devuelve un `KnowledgeGraphChunk` válido
  en el tercer intento. Verificable con un fake del cliente OpenAI que falle N
  veces y luego devuelva JSON válido, midiendo que las esperas respetan la
  secuencia exponencial (1s, 2s, 4s, ...).
- **AC-03.5 — Idempotencia (Neo4j):** Ejecutar `upsert_entities()` dos veces con
  la misma entidad no crea duplicados en la base de datos (verificar con
  `MATCH (n:Entity {id:$id}) RETURN count(n) == 1`).
- **AC-03.6 — Deserialización de secretos:** El adaptador de Neo4j usa
  `settings.neo4j_password.get_secret_value()` para autenticarse contra el
  driver; nunca loguea ni imprime el valor plano del `SecretStr`.