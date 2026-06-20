# Fase 05 — CLI Entrypoint

## Directiva para el Agente:
"Crea el punto de entrada en src/book_graph_rag/main.py usando click o typer.

    El comando principal debe instanciar Settings.
    Debe instanciar los Adaptadores de infraestructura.
    Debe inyectar los adaptadores en el Caso de Uso.
    Debe ejecutar el caso de uso con asyncio.run()."

---

## ✅ Criterios de Aceptación (Definition of Done)

> No marcar esta fase como completada hasta que TODOS los AC pasen.
> Estos AC son obligatorios y prevalecen sobre cualquier "se ve bien".

- **AC-05.1 — Ayuda funcional:** `uv run python -m book_graph_rag.main --help`
  muestra la descripción del comando y las opciones disponibles, y termina
  con código de salida 0.
- **AC-05.2 — Fail-Fast en CLI:** Ejecutar el comando sin un `.env` válido
  (o con variables requeridas ausentes) muestra un mensaje de error claro
  (no un traceback críptico) y termina con código de salida `!= 0`.
- **AC-05.3 — Composición correcta:** El punto de entrada (verificable por
  inspección del archivo `main.py` y/o un smoke test de integración):
  1. Instancia `Settings` (lo que dispara el Fail-Fast si falta `.env`).
  2. Crea los adaptadores concretos de `infrastructure`, pasándoles `Settings`.
  3. Los inyecta en `IndexBookUseCase` (via los tipos de puertos).
  4. Ejecuta el caso de uso con `asyncio.run(...)`.
- **AC-05.4 — Integración end-to-end (manual, con servicios locales):**
  Con Neo4j local y Ollama corriendo, `uv run python -m book_graph_rag.main index
  <pdf>` produce nodos/relaciones en Neo4j sin error de runtime.