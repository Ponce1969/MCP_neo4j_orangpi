# Graph Query Notes — Operator Runbook

Esquema real y trampas de consulta del grafo Neo4j de producción
(`bookgraph-neo4j`, OrangePi). Todo lo de abajo está verificado contra el
grafo en 2026-09-26 (libros: `knowledge:agentic-architecture`, `knowledge:essential-graphrag`,
`knowledge:graphrag-agentic`).

## 1. Dónde NO busca nadie más

**`RELATED` NO lleva `source_id`.** Sus únicas propiedades son
`source_page`, `chunk_index`, `type`, `description`.

```cypher
// Esto devuelve SIEMPRE 0 para cualquier namespace — no es un error, es el esquema:
MATCH ()-[r:RELATED]->() WHERE r.source_id STARTS WITH 'knowledge:x' RETURN count(*)
```

Filtrar por `source_id` en nodos `Entity`/`Chunk` funciona (ahí el `id`/`book_id`
sí es namespaced), pero en relaciones **NO**: no hay un id propio porque la
provenance se ancla a `source_page` + `chunk_index` (por eso el fix 1369dc0
propaga `chunk_index`; sin él la regla `PROVENANCE_RELATIONSHIP_MISSING`
dispara). Así los edges no colisionan entre libros: dos libros pueden citar la
misma página/índice de su propio mundo sin pisarse.

**Contar relaciones de un namespace — query correcto:**

```cypher
// Entidades del namespace
MATCH (e:Entity)-[r:RELATED]->(e2:Entity) WHERE e.id STARTS WITH $ns RETURN count(*)

// Relaciones que tocan los chunks del namespace
MATCH (c:Chunk {book_id:$ns})<-[r]-() RETURN type(r), count(*)
```

Valores conocidos (2026-09-26): `knowledge:essential-graphrag` = 3.106 RELATED,
`knowledge:graphrag-agentic` = 4.852 RELATED; global 23.392.

## 2. Esquema jerárquico real

- `(Book)-[:CONTAINS]->(Chapter)` — el tipo `HAS_CHAPTER` **NO existe** (Neo4j
  responde warning `01N51` si lo usás).
- `(Chapter)-[:HAS_SECTION]->(Section)`, `(Section)-[:HAS_SUBSECTION]->(Section)`.
- `(Chapter|Section)-[:HAS_CHUNK]->(Chunk)`.
- `(Chunk)-[:MENTIONS]->(Entity)`, `(Entity)-[:RELATED]->(Entity)`.
- `Chapter` y `Section` llevan `book_id` — **siempre** incluir `book_id` en
  cualquier MERGE/MATCH de jerarquía (bug 4652bf9: sin él, dos libros comparten
  secciones genéricas como "Summary").

## 3. Criterio de éxito de una corrida de indexado

El log del indexador es buffered y puede quedar en **0 bytes aunque el run
termine** (o muera justo después de escribir todo). El criterio de verdad es el
grafo: `:Checkpoint {source_id}` con `131/131` o `322/322` PROCESSED, no el log.

```cypher
MATCH (c:Checkpoint) WHERE c.source_id CONTAINS $source
RETURN c.status, count(*) ORDER BY c.status
```

Monitoreo en vivo: `pgrep -af "book-graph-rag index"`.

## 4. Cuentas por namespace (fórmula)

```cypher
MATCH (e:Entity) WHERE e.id STARTS WITH $ns AND (e.merged_into IS NULL OR e.merged_into = '')
RETURN count(*)
```

Los `merged_into` quedan en el grafo (soft-delete) — los duplicados resueltos
siguen contando si no filtrás por `merged_into IS NULL`.

## 5. Archivos de referencia

- Auditor (queries estáticas por regla): `src/book_graph_rag/infrastructure/neo4j_audit_adapter.py`
- Backup/restore: `scripts/run_full_pipeline.py` (`_backup` = dump JSON a `~/backups_neo4j/`)
- Probes read-only operativos: `scripts-ops/probe_redo_state*.py` (scp a `/tmp` del server)