# Reset pilot graphrag-agentic (c3468) y relanzar libro 2 con receta corregida

## Objetivo
Deshacer el piloto contaminante del libro 2 (GraphRAG agéntico) indexado el 24/09 en
`knowledge:graphrag-agentic` y relanzarlo con la receta protocolar (MERGE por book_id ya
desplegado en `4652bf9`). Restaurar el grafo al estado 2 libros limpio primero.

## Evidencia del incidente (reconstruida el 2026-09-25, read-only)
- El piloto (4 capítulos, 3.068 entidades) quedó en el namespace `knowledge:graphrag-agentic`.
- 45 warnings `DUPLICATE_ENTITY_LOGICAL` (todos en GA) + **7 secciones con >1 padre** (contaminación
  cross-book del MERGE sin book_id).
- Essential limpio (0/0/0, 8 capítulos) pero con page_starts pisadas por el piloto en secciones compartidas.
- Libro 1 (AAP) intacto (19 cap, 7.111 entidades).
- Backups: `20260924T231159Z` = PRE-PILOTO limpio (1.822 chunks, 0 GA); `20260925T020527Z` = pre-cleanup (con piloto, 2.144 chunks).
- Scripts del incidente (sin commitear): `scripts-ops/{cleanup_c3468_ns.py,probe_*.py,resolve_c3468_intra.py,mcp_smoke2.py}` + `data/graphrag-agentic-pilot-c3468.pdf`.
- El restore del pipeline es MERGE aditivo idempotente (no wipea) → el desarme requiere DELETER el namespace + restaurar.

## Tareas
- [ ] Backup fresco del estado actual (seguridad, AGENTS.md §7)
- [ ] Dry-run cleanup_c3468_ns.py (read-only) + revisión del plan
- [ ] Aprobación humana explícita del apply
- [ ] Apply cleanup: borrar Checkpoints/Chunks/Entities/Book+Chapters de GA + secciones huérfanas
- [ ] Restore `bookgraph_backup_20260924T231159Z.json` (MERGE del estado 2 libros; arregla page_starts compartidas)
- [ ] Verificar: audit global 0/0/0, GA vacío, secciones >1 parent = 0, chunks = 1.822, book1+Essential intactos
- [ ] Rehacer piloto libro 2: decisión parcial (TOC ch 3-4-6-8) vs completo; receta del ancla obs 1343
  (dry-run → backup → index con book-graph-rag index --corpus knowledge --source graphrag-agentic →
  audit scoped → resolución intra-namespace → smoke MCP)

## Gates
- AGENTS.md: mutaciones con backup → dry-run → aprobación humana; NUNCA --fresh; solo contenedor bookgraph-neo4j.
- Post-restore: 4 gates locales verdes (ruff/mypy/arch/validate_architecture) no aplican a mutación de datos, pero
  audit del grafo sí: blocking=0 / warning=0 / incomplete=0.

## Estado
- 2026-09-25: verificación read-only completa (audits + probes). Backup pre-piloto identificado.
- 2026-09-25 noche: aplicado cleanup (APROBADO por usuario) — grafo de vuelta EXACTO al estado sellado: 1.822 chunks, 8.352 entidades, 2 libros, GA vacío, audit global 0/0/0, audit scoped GA 0/0/0. 7 secciones >1 padre = baseline pre-existente del libro AAP (no del piloto). Restore descartado (formato viejo sin start_props incompatible con _restore actual + innecesario: conteos cuadraron).
- 2026-09-26 00:39: lanzado el REDO parcial (PDF c3468, 305 págs, caps 3-8) con la receta corregida: `nohup ~/.local/bin/uv run book-graph-rag index data/graphrag-agentic-pilot-c3468.pdf --corpus knowledge --source graphrag-agentic > /tmp/pilot_redo_index.log 2>&1 &` (PID 3937868, resumible). Backups frescos: 20260926T032340Z (pre-cleanup) + 20260926T033702Z (pre-run).
- MAÑANA: verificar corrida (checkpoints PROCESSED → 322), luego gate §7.3: audit scoped GA → aislados/duplicados → resolución intra con ledger si hace falta → audit global 0/0/0 → smoke MCP post-restart → decidir comunidades/Leiden del libro nuevo.

## Cierre 2026-09-26 — GATE §7.3 COMPLETO (piloto REDO sellado)
- Corrida verificada en grafo (el proceso murió sin flush de log, pero el grafo manda): **322/322 checkpoints PROCESSED**, 322 chunks, 3.078 entidades, 4.852 RELATED (provenance OK), jerarquía book + 4 capítulos (3,4,6,8) + 104 secciones aislada por book_id (fix 4652bf9 OK).
- Audit scoped GA pre-resolución: 0 blocking / 0 incomplete / **53 warning** DUPLICATE_ENTITY_LOGICAL.
- Resolución intra APROBADA y aplicada: **53 merges (seq 114-166, chained, approver human:gonzalo)** desde `/tmp/resolve_redo_intra.py` (grupos generados de la query del auditor sin truncar: `scripts-ops/gen_resolve_redo_groups.py` + `resolve_redo_groups.json`; canonical = id más corto, band exact, soft-delete). 55 duplicados → merged_into. Backup fresco: `~/backups_neo4j/bookgraph_backup_20260926T121536Z.json`.
- **Audit scoped GA post-resolución: 0/0/0** (19 reglas). **Audit global: 0/0/0** (2.144 chunks, 3 books, salud preservada).
- **Smoke MCP OK sin restart** (service corre desde 23/09 con catálogo cargado): `count_entities` GA = 3.078, `search_rag` responde con entidades del libro nuevo + provenance, aislamiento por scope verificado.
- Decisión mantenedor: el ledger NO se compensa para seq 67-113 (merges del piloto viejo borrado) — el rollback nativo re-crearía entidades fantasma; el ledger queda como historial verdadero. Nota menor: 1 hit de retrieval "Tool Orchestration p189" con provenance [None] (no bloquea, audits OK).
- Pendientes post-piloto: comunidades/Leiden del libro nuevo (community summaries = 0, hueco del ancla), indexar el resto del libro (capa que faltan), cross-namespace resolution (20+ conceptos), commitear scripts-ops/ + evidence-bundles/.