# Fase 04 — Application

## Estrategia — STREAMING CON MINI-BATCHES (opción B)

> Decisiones previas:
> - Opción B: cada chunk que termina su paso por el LLM se encola en una
>   `asyncio.Queue`. Cuando la queue acumula `batch_size` chunks listos, el
>   consumer hace un mini-batch upsert a Neo4j. Evita bloquear el pipeline
>   esperando al chunk más lento de un lote — clave para indexar libros grandes
>   (574 páginas → >1000 chunks estimados) contra Groq rate-limited (~30 req/min).
> - Política de fallo (dead-letter): cuando un chunk falla todos los retries
>   del LLM (max_retries=3), se SKIPea del flujo principal y se persiste en un
>   `dead_letter.log` (JSONL) con chunk_index, page_ref, error_message, para
>   reintentar manualmente después. El pipeline NO aborta por chunks fallidos
>   individuales.

## Directiva para el Agente:
"Crea los casos de uso en application/.

    Crea IndexBookUseCase.
    Este caso de uso debe recibir los Puertos (Interfaces) en su constructor, NUNCA las implementaciones concretas (Inyección de Dependencias).
    Además recibe dos PRIMITIVOS: `max_concurrency: int` y `batch_size: int`.
    NUNCA recibe `Settings` (ver Política de inyección en `docs/specs/01_foundation.md`).
    El método execute(pdf_path: str) debe:
        Usar el PDFReaderPort para obtener los chunks (sync iterator — for chunk in pdf_port.extract_chunks(path)).
        Usar asyncio.Semaphore(self.max_concurrency) para limitar concurrencia LLM.
        Lanzar tasks para cada chunk con el LLMProviderPort (concurrentes según Semaphore).
        Cada task exitosa POPEA el chunk a una asyncio.Queue.
        Una corutina CONSUMER drena la queue: cuando acumula batch_size chunks, agrupa y llama GraphDatabasePort.upsert_book (si existe) + upsert_editorial_structure + upsert_entities + upsert_relationships. Luego limpia el buffer.
        En shutdown: poner sentinel None en la queue, drenar lo recursive, hacer flush final del buffer parcial.
        En error de chunk (lnstructorRetryException o Exception genérica tras retries exhaustos en el adapter): logear chunk a dead_letter (JSONL), SKIPear del pipeline, seguir con el resto.
        La corutina execute espera a que todos los producers + el consumer finalicen."

### Detalle del consumer pattern

- Consumer corutina se initial en `execute()` con `asyncio.create_task(self._consumer(...))`.
- Producer corutinas (una por chunk) obtienen semaphore, llaman extract_graph, awaitean queue.put(extracted_chunk), released semaphore.
- Sentinel: cuando todos los producers terminan, se hace `await queue.put(None)` una sola vez. La消费品coroutina rompe el loop `while True` cuando recibe `None`.
- Dead letter: archivo JSONL con líneas `{"chunk_index": int, "page_ref": {"start": int, "end": int}, "error": str, "timestamp": ISO8601}`. Se appendea con open(..., mode='a', encoding='utf-8'). La ruta del dead letter se CAPTURE en __init__ como parámetro obligatorio adicional `dead_letter_path: Path`. Si ese Path existe, se appendea; si no existe, se crea.

---

## ✅ Criterios de Aceptación (Definition of Done)

> No marcar esta fase como completada hasta que TODOS los AC pasen.
> Estos AC son obligatorios y prevalecen sobre cualquier "se ve bien".
> Se validan con **fakes de los puertos**, no con adaptadores reales
> (la aplicación jamás debe depender de infraestructura concreta para testearse).

- **AC-04.1 — Inyección de puertos y primitivos:** `IndexBookUseCase` recibe
  en su constructor instancias de los **puertos** (`PDFReaderPort`,
  `LLMProviderPort`, `GraphDatabasePort`), no de los adaptadores concretos, más
  tres primitivos `max_concurrency: int`, `batch_size: int`, y
  `dead_letter_path: Path`. **NO recibe `Settings`** (acoplaría application a
  infraestructura). Verificable: la signatura de `__init__` está tipada con
  interfaces ABC + primitivos, y `uv run mypy .` rechazaría pasar `Settings`.
- **AC-04.2 — Control de concurrencia:** Al ejecutar `execute()` con 10 chunks
  y `max_concurrency=3` (pasado al constructor del caso de uso), el adaptador
  de LLM nunca recibe más de 3 llamadas simultáneas. Verificable con un
  mock/fake que registre timestamps de entrada y salida y permite afirmar
  `max_concurrency_observed <= 3`.
- **AC-04.3 — Procesamiento por mini-batches:** Con `batch_size=5` y 12 chunks
  entrantes, el consumer produce exactamente 3 upsert batch calls al
  `GraphDatabasePort` (5 + 5 + 2). Verificable con un fake del
  `GraphDatabasePort` que registre cuántos `upsert_entities(...)` ocurren y
  cuántos items por llamado (5, 5, 2 = 3 batches).
- **AC-04.4 — Flujo completo con mocks:** Usando mocks de los tres puertos,
  `await use_case.execute("dummy.pdf")` completa sin errores y llama a
  `upsert_entities()` y `upsert_relationships()` al menos una vez.
- **AC-04.5 — Dead-letter en fallo:** Si el LLM fake Lanza una Exception en
  ciertos chunks (simular fallo post-retry-exhausted), esos chunks NO llegan
  al `GraphDatabasePort` (no se upsertean como entities/relationships), pero
  SÍ se persisten en el archivo `dead_letter.log` como JSONL con chunk_index,
  page_ref, error. El pipeline no aborta — los chunks restantes siguen el
  flujo normal.