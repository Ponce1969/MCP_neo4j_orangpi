# Close book 2 (graphrag-agentic): communities + full index + cross-namespace + commit

## Objetivo
Cerrar el libro 2 (GraphRAG agéntico, c3468) completo en producción y dejar el repo
al día con los artefactos operativos.

## Tareas
- [ ] Decidir estrategia de re-index del libro completo (A: clean + re-index 692p, receta del ancla; B: conservar piloto)
- [ ] Lanzar re-index completo del libro 2 (si A): clean namespace GA + `book-graph-rag index` PDF completo (nohup, ~4-6h, resumible)
- [ ] Commit artefactos: branch `feature/close-book2-ops` + units (scripts-ops/, docs/ops/graph_query_notes.md, evidence-bundles/, odd/tasks/, .gitignore .engram) + gates locales
- [ ] (Post-corrida) Verificar 587+ chunks PROCESSED + audit scoped GA 0/0/0
- [ ] (Post-corrida) Resolución intra del libro completo (reusar resolve_redo_intra.py con el nuevo audit)
- [ ] Comunidades/Leiden del libro GA (runner filtrado por namespace; esencial ya tiene las suyas)
- [ ] Cross-namespace resolution (20+ conceptos compartidos: LLM, Agentic RAG, Neo4j...) — dry-run → backup → aprobación → apply
- [ ] Audit global 0/0/0 + smoke MCP + actualizar ancla/docs/memoria

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