# Close book 2 (graphrag-agentic): communities + full index + cross-namespace + commit

## Objetivo
Cerrar el libro 2 (GraphRAG agéntico, c3468) completo en producción y dejar el repo
al día con los artefactos operativos.

## Tareas
- [x] Decidir estrategia de re-index (A: clean + re-index 692p, receta del ancla — elegida por mantenedor)
- [x] Lanzar re-index completo del libro 2: clean namespace GA APLICADO (backup 20260926T123229Z) + `nohup book-graph-rag index /tmp/book2_full.pdf --corpus knowledge --source graphrag-agentic --no-resume` (PID 324264, 657 chunks, resumible)
- [x] Commit artefactos: branch `feature/close-book2-ops` + units SIN exenciones de gates:
  `feb44f1` fix(pipeline) DateTime backup · `f2370de` chore(config) gitignore · `241bc73` docs(ops) · `95b258e` chore(ops) scripts-ops lint/type-clean
  Gates locales VERDES sin exenciones: ruff 0 errores, mypy 329 files OK, validate_architecture OK (los ~64 errores de ruff + 64 de mypy en scripts-ops fueron reparados por gentle-ai-worker, no eximidos)
- [x] (Post-corrida) VERIFICADO 2026-09-26 16:02: **657/657 PROCESSED**, 657 chunks, 6.078 entidades, book "Agentic GraphRAG". Audit scoped GA pre-resolución: **0/0/136** (todos DUPLICATE_ENTITY_LOGICAL)
- [x] (Post-corrida) Resolución intra del libro completo: backup fresco `bookgraph_backup_20260926T191048Z.json` + dry-run OK (279 ids activos, 6.078→5.935) + aprobación mantenedor + **APPLY 136 merges ledger seq 167→302** (approver human:gonzalo, band exact, soft-delete merged_into). Post: audit scoped GA **0/0/0**, GA activas 5.935 / 143 merged_into. Scripts: `scripts-ops/gen_resolve_redo_groups.py` (ya existía) + **`scripts-ops/resolve_full_book2_intra.py` (nuevo, lee /tmp/resolve_redo_groups.json, flags --dry-run/--apply, SIN grupos hardcodeados)**
- [x] **Comunidades/Leiden libro GA**: runner scoped **`scripts-ops/run_communities_scoped.py`** (nuevo; subgrafo GA filtrado por prefix+merged_into, orquestador bottom-up autónomo porque el global recarga toda la gráfica). **191 summaries = nivel0 1 / 48 / 62 / 80, 0 vacíos, 0 failed** (deepseek-v4-flash, ~45 min, verificado en grafo). Guard `community_max_calls` 150→220 env inline. PID 776361, log /tmp/communities_ga.log.
- [x] **Cross-namespace COMPLETO (302 grupos)**: detector **`scripts-ops/gen_cross_groups.py`** (302 grupos name+kind en 2+ libros) + resolver **`scripts-ops/resolve_cross_namespace.py`** (band EXACT, cross_namespace=True, evidencia por par con su namespace; path del JSON parametrizable vía `CROSS_GROUPS_JSON`). 
  - **Fase 1 firmes (57)**: tool 34 + framework 12 + agent 10 + mcp 1 = 63 dups. Backup `bookgraph_backup_20260926T201253Z.json` + dry-run 120/120 + aprobación + **APPLY ledger 303→359**.
  - **Fase 2 restantes (245)**: concept 169 + component 45 + pattern 16 + risk 12 + llmops 3 = 259 dups. Backup `bookgraph_backup_20260926T221319Z.json` + dry-run 504/504 + aprobación + **APPLY ledger 360→604**.
  - **Post (total)**: audits global + 3 scoped **0/0/0** (aislamiento preservado). Ledger final: 604 seq.
- [x] **Fix retrieval merged_into** (derivado del smoke): `find_entity`/`find_entities_batch` devolvían entidades soft-deleted como hits. Fix `80f68f1` (tests en test_neo4j_query_adapter: tiers + batch) desplegado + re-smoke OK (fantasma eliminado: 'MCP server' GA 2→1). Observación: tier fulltext de alias en libro 1 ahora expone candidatos fuzzy (diseño tiers correcto).
- [x] Comunidades/Leiden del libro GA (runner filtrado por namespace; verificadas en grafo 191)
- [x] Cross-namespace resolution (firmes: tool/framework/mcp/agent, 57 grupos) — backup → dry-run → aprobación → apply (ledger 303-359)
- [x] Audit global 0/0/0 + audits scoped 3 libros 0/0/0 (post-cross). Pendiente: smoke MCP (service ya corriendo) + actualizar ancla/docs/memoria

## Estado inicial (2026-09-26, gate §7.3 del piloto cerrado)
- Piloto REDO sellado: 322 chunks, 3.023 entidades activas, 4.852 RELATED, 0/0/0 audits, smoke OK. Ledger 166 seq.
- PDF completo del libro en server: `data/GraphRAG agéntico - Anthony Alcaraz, Sam Julien (EN).pdf` — 692 págs, TOC 240.
- Checkpoint key = (source_id, chunk_index): un parcial del "resto" colisionaría índices con el piloto (TOC distinto) → la receta oficial (clean+completo) es la única limpia.
- Comunidades: loader global; `get_summaries_by_level(level, scope=)` ya filtra por namespace (ANY entity_ids STARTS WITH prefix). Libro esencial tiene summaries; GA = 0 (hueco del ancla).
- Cross-ns: pendiente de detección (entidades con name+kind en 2+ namespaces). Mutación → backup → dry-run → aprobación.

## Gates
- AGENTS.md §7: mutaciones con backup → dry-run → aprobación; NUNCA --fresh; solo bookgraph-neo4j.
- §7.2: merges de resolución (intra y cross) = mismo gate; ledger append-only; soft-delete merged_into.
- Commit: ruff/mypy/validate_architecture verdes.