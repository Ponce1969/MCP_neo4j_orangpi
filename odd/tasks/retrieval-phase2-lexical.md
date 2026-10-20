# Retrieval phase 2 (lexical, option B) — re-opened with a fixed cut criterion

**Owner**: parent. **Branch**: `feat/retrieval-phase2-lexical`. **Risk**: none for the instrument (script + settings + docs);
**item 3** if a variant is ever shipped into the production query path, because that changes what `search_rag` returns.

The objective `precision@k >= 0.15` is **unmet and declared** at `0.1250`. Phase 0 measured the vector hybrid (A) and
abandoned it: equal-weight fusion lost (−0.0167), the best lexical-heavy fusion gained exactly one reference
(+1/120) and stayed under the objective (0.1333). The ADR records "A is not built and B is not pursued for now".
The operator re-opens B under the conditions below. **B's ceiling is exactly `0.150` with zero margin** — the three
references that are in the pool but outside the top-10.

## Specs

**S1 — the ADR is amended, never overwritten.** Append a decision record to `docs/spec/09-retrieval-pool-recall.md`:
phase 2 is re-opened on 2026-10-19 by the owner, under S2's cut criterion, because the objective is unmet, the
experiment needs **no data write** and B's own bound is known. The historical phase-0 result and the previous owner
decision stay untouched; the amendment says what changed (nothing measured — a decision to measure) and what would
void it (a variant that fails S2).

**S2 — the cut criterion, fixed before any run** (operator's verbatim rules): the objective stays **`0.150`**; a variant
**ships** only if it beats `0.1250` by **at least one net reference** (+1/120 = 0.0083) **without losing any current
reference** — a monotonic gain. A change that gains one and loses one is not an improvement. If no variant qualifies,
**option C stays** and there is no third attempt. **At most two variants are pre-registered**: one query shape, one
analyzer.

**S3 — the control row stays in the same run.** The pure-fulltext leg reproduces `0.1250` exactly; if it does not, the
run is **void** — that is the canary for measurement drift, not a result to interpret.

**S4 — variant 1 (query shape) is caller-side.** The fulltext query string is never composed in this repository:
`Neo4jQueryAdapter.search_chunks` binds the caller's string verbatim as a Cypher parameter
(`infrastructure/neo4j_query_adapter.py:526-582`). So a phrase/proximity variant is a **transform of the question before
the call**, not an adapter change — a new flag in `scripts/eval_fulltext_recall.py`, whose three modes today pass the
raw question (`:101`, `:170`, `:315`). **The transform must escape the question first**: no escaping exists anywhere, so
an unescaped shape variant would measure a Lucene syntax error rather than retrieval quality. If it wins, the shape
becomes a setting mirroring `rerank_enabled` + its single `reranker_for()` factory rule — **not**
`candidate_retrieval_strategy`, which is declared and validated but consumed nowhere.

**S5 — variant 2 (analyzer) has preconditions before it can be measured, and they are worth doing even if it loses.**
The analyzer `standard-no-stop-words` is a snowflake: `ensure_indexes` creates `chunk_text_index` with **no `OPTIONS`
and no analyzer** (`neo4j_query_adapter.py:725`), and no code in the repository declares it, so a later rebuild could
silently revert any change. And **no `DROP INDEX chunk_text_index` exists anywhere** — `IF NOT EXISTS` makes re-running
the DDL a no-op. Therefore: (a) declare the **current** analyzer explicitly in the DDL (same behaviour, zero change to
the number) so the repository owns the fact; (b) provide a **scripted, idempotent** recreation (drop → create with the
chosen analyzer → repopulate) with a dry-run, following the project's gated-operation pattern.

**S6 — the latency budget, measured honestly.** Report the **p95 of the graph path** (`fetch_contexts`, the same run
that yields precision) as a second `LayerMetricValue` named `latency_p95_graph_floor_ms`, threshold **250**, comparator
`<=`, `n = 12` and the warm-up caveat stated in the rationale. It is a **floor**, not the `search_rag` number: the MCP
envelope adds scope resolution, budget, logging and `find_entity`. Decision rule: if the floor exceeds 250 ms the
variant is **discarded immediately**; if it sits comfortably below, the envelope needs **its own** benchmark with
representative traffic before the budget is claimed as met. The layer stays non-blocking either way.

**S7 — the two measurement drifts are closed.** (a) The script's hardcoded `COMMITTED_BASELINE = 0.1250`
(`scripts/eval_fulltext_recall.py:55`) is a second source of truth for the number the cut criterion is applied
against; it must read the committed artifact `data/evaluation/retrieval_baseline.json` (`0.125`). (b) The gate divides
by `len(list)` while the script divides by `len(set)`; verify chunk ids are unique per question (or make both sides
use the same denominator) so the two are provably equal.

**S8 — the determinism caveat is reported, not hidden.** `ORDER BY score DESC LIMIT $limit` has **no tie-breaker**
(`neo4j_query_adapter.py:551`, `:570`); a Lucene score tie at the top-10 boundary can change membership and the metric
is set-based. The control row (S3) is the canary; the report says so instead of assuming stability.

**S9 — documentation.** Spec 09 amended per S1; `AGENTS.md` §7.4 gains the measured value (if any) and the latency
budget as a new line; the backlog records the run. Historical records are **appended**, never rewritten.

## Tasks

- **T1** [S5] declare the current analyzer in `ensure_indexes`' DDL + a test pinning the DDL text. → commit 1
- **T2** [S5] the scripted, idempotent index recreation with a dry-run (not executed here). → commit 1
- **T3** [S4] the query-shape flag in the eval script, **with escaping**, and the control leg in the same run. → commit 1
- **T4** [S7] close both drifts: read the committed baseline; unify the denominator (or pin equality). → commit 1
- **T5** [S6] the latency floor metric: p95 over `fetch_contexts`, `n` and warm-up in the rationale, threshold 250. → commit 1
- **T6** [S2,S8] the run harness prints the cut decision (ship / stay on C) against S2's rule and the control row's
  reproduction; the ADR amendment records that the run is the operator's. → commit 1
- **T7** [S1,S9] the docs: spec 09 amendment, `AGENTS.md` §7.4, backlog. → commit 2
- **T8** (parent) verification: suite, gates, and a spot check that the escaping and the control row are real. The
  **run itself belongs to the operator** (production graph, read-only) — the implementation delivers the instrument.
- **T9** [S5, L4] the DDL emitted a Cypher syntax error (`'fulltext.analyzer'` instead of the backtick form): fix the
  single source, re-record the three byte pins with the statement production **accepted**, and add the general guard
  (`tests/unit/test_cypher_map_keys.py`) that makes the next DDL inherit the check. → commit 3
- **T10** [S1-S3] the operator's run on production hardware, both variants, the measured rollback and the
  closure under option C — recorded in `docs/spec/09-retrieval-pool-recall.md` §11 and `odd/backlog.md`.
  → operator, not a commit here

## Log

**L1** (2026-10-19, operator, verbatim): "1. Objetivo (Confirmado): Se mantiene el objetivo formal en 0.150 (requiere
capturar las 3 referencias faltantes). Condición de envío/mejora: Aceptamos variantes que superen el baseline de 0.1250
en al menos una referencia neta (+0.0083) sin perder ninguna de las actuales (ganancia monotónica). Si ninguna lo
supera, se asume Opción C sin forzar terceros intentos. 2. Recreación del índice fulltext (Confirmada): Aprobada la
operación DDL (DROP + CREATE con nuevo analyzer). Es reversible y no toca datos ni vectores. 3. Presupuesto de latencia
para search_rag (Fijado): Límite objetivo: p95 <= 250 ms (techo estricto en 500 ms por consulta). Debe reportarse junto
al precision@k. 4. Control permanente (Confirmado): Se mantiene la fila de control de fulltext puro en la misma corrida
para garantizar comparabilidad exacta. 5. Regla de variantes (Aprobada): Máximo 2 variantes pre-registradas (forma de
consulta / analyzer)."

**L2** (2026-10-19, operator, verbatim): "Enmienda del ADR (Aprobada): Sí, enmendar formalmente el ADR en la spec
mediante un addendum o registro de decisión (S#) que documente la reapertura de la fase 2 bajo las condiciones de corte
fijadas, preservando la trazabilidad histórica sin sobreescribir el registro previo. Declaración explícita del analyzer
en el DDL (Aprobada): Sí, incorporar en ensure_indexes el analyzer actual (standard-no-stop-words) de forma explícita
en las opciones del índice. Eliminar este copo de nieve le da determinismo al repo ante cualquier eventual
reconstrucción, independientemente del resultado de la variante 2. Latencia del camino de grafo como piso (Aprobada):
De acuerdo con medir inicialmente el p95 de fetch_contexts como piso declarado (con su métrica secundaria
latency_p95_graph_floor_ms, n = 12 y advertencia de warm-up/muestra). Si este piso excede los 250 ms la variante queda
descartada de inmediato; si se mantiene holgadamente por debajo, se deja asentado que el envelope completo de search_rag
requerirá su propio benchmark con tráfico representativo. Adicionalmente, queda autorizado unificar los drifts
detectados en el script (COMMITTED_BASELINE = 0.125 vs 0.1250 y la verificación de unicidad en los IDs de chunks para
blindar el denominador)."

**L3** (parent, the map's findings that shaped S4-S8): the query string is caller-owned and unescaped; the analyzer is
declared by no code and no `DROP INDEX chunk_text_index` exists; the eval script cannot A/B shapes today;
`candidate_retrieval_strategy` is a dead flag; the script hardcodes its own baseline; the gate and the script use
different denominator shapes; there is no tie-breaker; and the only measurable latency is the graph path, as a floor,
over n = 12.

**Implementation (2026-10-19, package-owned writer).** T1-T7 delivered on
`feat/retrieval-phase2-lexical`.

- **commit 1 (T1-T6): `094ac55`** — analyzer declared in `ensure_indexes` DDL with the DDL pins
  strengthened on both sides (adapter + recreation script); gated idempotent recreation script
  (`scripts-ops/recreate_chunk_text_index.py`, dry-run default, approval word + reviewed-plan
  fingerprint, **not executed**); `--shape-ab` with Lucene escaping (`_escape_lucene` /
  `_shape_query`), control leg verbatim in the same run; committed baseline read from
  `data/evaluation/retrieval_baseline.json` (`COMMITTED_BASELINE` removed); per-leg chunk-id
  uniqueness guard making `len(list) == len(set)` provable (S7b); `latency_p95_graph_floor_ms`
  in the retrieval layer (nearest-rank p95 over `fetch_contexts`, threshold 250, `<=`, n and
  warm-up in the rationale, floor stated as floor); the S2 cut printer (ship / hold / VOID with
  the control reproduction row) wired into `--shape-ab` and `--simulate-hybrid`, plus the S8
  no-tie-breaker caveat in the shape-mode output.
- **commit 2 (T7): this docs commit** — spec 09 §10 amendment (appended, history untouched),
  `AGENTS.md` §7.4 latency/cut line, backlog entry, and this Log. Its own hash cannot be
  embedded in the commit that carries it; the parent handoff records it alongside `094ac55`.
- **RED captured before implementation** (the genuine behavioural one, S4 escaping):
  `AttributeError: module 'eval_fulltext_recall' has no attribute '_escape_lucene'`; also
  `FileNotFoundError` for the not-yet-existing recreation script, and failed latency-metric and
  DDL pins. **Verification run:** `uv run ruff check .`, `uv run ruff format .`,
  `uv run mypy .`, and the full suite
  `uv run --no-sync pytest tests/ -q --ignore=tests/integration --deselect tests/property/interruption_fuzz_test.py`
  → 2251 passed, 3 skipped, 1 deselected. Pre-commit gates (ruff, mypy, architecture
  validation, format) passed on commit 1.
- **Not run, by contract:** the index recreation, the `--shape-ab` measurement, and any
  evaluation against production — those belong to the operator.

**Independent-verification fixes (2026-10-19, package-owned writer, branch
`fix/recreate-index-defects`).** Five defects an independent verification found in
`scripts-ops/recreate_chunk_text_index.py` before it may ever be applied to production, plus the
`neo4j_uri` fingerprint binding. Commit 1 = fixes + pins; commit 2 = this Log entry.

- **F1 — la ventana de indisponibilidad queda declarada** en el dry-run, en la salida del
  `--apply` y en el JSON (`_WINDOW_WARNING`): (a) el plan es DROP → CREATE, así que entre
  ambas sentencias `chunk_text_index` no existe y la pata de chunks queda sin fuente; el nombre
  está hardcodeado en el camino de lectura (`neo4j_query_adapter.py:536` y `:554`), luego **no
  hay shadow-swap posible** y la ventana es inherente; (b) tras la DDL una corrida puede leer
  `precision@k = 0.0000` porque el índice está **ausente o repoblando**, no porque el analyzer
  destruya la retrieval — `neo4j_retrieval_adapter.py:92-95` usa `return_exceptions=True` y sólo
  procede `if isinstance(chunks, list)`, de modo que un índice ausente colapsa la pata a `[]`
  y la precisión lee 0.0 en silencio; (c) la evaluación no debe correr con la ventana abierta.
- **F2 — camino de falla:** `_apply` ya no puede dejar al operador con un traceback crudo.
  `main` captura la excepción, imprime el estado observado (`absent`), dice con claridad que el
  índice debe recrearse y da la recuperación exacta — **dry-run nuevo + fingerprint nuevo**,
  porque el plan incluye `current_index_state` y ahora lee `absent` — y devuelve código 2.
- **F3 — el analyzer como post-condición:** la consulta de estado ahora proyecta `options`
  (`SHOW INDEXES YIELD name, type, state, populationPercent, options`) y la verificación exige
  `options.indexConfig['fulltext.analyzer'] == <analyzer aplicado>` (evidencia del operador en
  Neo4j 5.23: **sí está expuesto** — la nota del script que afirmaba lo contrario era falsa y se
  borró). Esto cierra el agujero no-op (`DROP … IF EXISTS` + `CREATE … IF NOT EXISTS` ya no
  pueden pasar `ONLINE`/`100%` sin que el analyzer pedido esté realmente aplicado). La población
  acepta ambos grafos (`100.0` y `100`): el `startswith("ONLINE (population=100.0%")` anterior
  falsa-alarma con el entero. La línea de éxito ahora es verdadera: dice `verificado vía SHOW
  INDEXES` en lugar de implicar el analyzer.
- **F4 — rollback como protocolo (el único):** documentado en el docstring y anunciado en la
  salida. Como el default de `--analyzer` reproduce `DECLARED_ANALYZER`, recrear con el analyzer
  original **es este mismo script sin `--analyzer`**, con la MISMA puerta (dry-run fresco →
  aprobación → fingerprint nuevo). Verificación posterior: re-correr
  `scripts/eval_fulltext_recall.py --shape-ab` y leer la fila de control. **Trampa declarada:**
  después de la DDL esa fila declara `RUN VOID`, porque corre contra el analyzer nuevo y ya no
  reproduce la línea base commiteada — es **esperado, no drift de medición**; la comparación
  post-DDL es **por pregunta contra el registro pre-DDL**, nunca contra la línea base commiteada.
- **F5 — una sola fuente del analyzer y del DDL:** `chunk_text_index_ddl(analyzer)` y
  `DECLARED_CHUNK_ANALYZER` viven en el adapter; `ensure_indexes` **lo llama** y el script
  **deriva** su sentencia de la misma función (no guarda copia). El bloque vive al final del
  archivo a propósito, para que las líneas `:536`/`:554` citadas por F1 no se muevan. Pins:
  delegación con centinela + igualdad de bytes con la función del adapter + **el pin que
  faltaba**: la sentencia ejecutada por `ensure_indexes` contra la sentencia del script, byte a
  byte (`tests/test_neo4j_query_adapter.py`).
- **Fingerprint:** `neo4j_uri` viaja dentro del plan ⇒ un fingerprint minteado en dev no puede
  validar un `--apply` contra prod (ni al revés).
- **Sin tocar, por contrato:** el chequeo de la palabra (`"approve" in content`) — es el contrato
  de este proyecto en toda puerta par (`scripts-ops/resolve_intra_ns.py:480`,
  `scripts-ops/cleanup_namespace.py:113`); se reporta aquí en lugar de debilitarlo.
- **RED observado (por comportamiento, antes de implementar):**
  `AttributeError: … has no attribute 'chunk_text_index_ddl'`;
  `ImportError: cannot import name 'chunk_text_index_ddl'`;
  `assert 'options' in 'SHOW INDEXES YIELD name, type, state, populationPercent …'`;
  `assert {'index_state': 'absent'} == {… 'analyzer': None, 'population': None, 'state': None}`;
  `AttributeError: … has no attribute '_population_at_100'`;
  `TypeError: build_plan() got an unexpected keyword argument 'neo4j_uri'` (×2);
  `assert 'shadow-swap' in <salida del dry-run>` y lo mismo en la salida del `--apply`;
  `KeyError: 'unavailability_window'`; `RuntimeError: CREATE falló` escapando **sin capturar**
  (el defecto F2 en carne); `assert 0 == 1` (un analyzer incorrecto pasaba la post-condición);
  `assert 1 == 0` (la población entera era rechazada); `assert 'Rollback' in <docstring>`. El pin
  cruzado de bytes nació **verde** (ambos literales son idénticos hoy): es la cerradura contra
  futuras divergencias, no una conducta nueva.
- **Verificación:** `uv run ruff check .` → `All checks passed!`; `uv run ruff format .` →
  `481 files left unchanged`; `uv run mypy .` → `Success: no issues found in 481 source files`;
  suite completa → **2266 passed, 3 skipped, 1 deselected**.
- **Sigue sin correr, por contrato:** la recreación del índice y cualquier evaluación — del
  operador.

**L4** (2026-10-20, operator, verbatim): "Causa raíz del error en apply: CypherSyntaxError: Invalid
input ''fulltext.analyzer'': expected an identifier or '}' — Neo4j Cypher no permite comillas
simples en las claves de mapas de configuración. Requiere backticks:
OPTIONS {indexConfig: {`fulltext.analyzer`: 'standard'}}". Estado reportado en la misma corrida:
`chunk_text_index` restaurado a ONLINE con `standard-no-stop-words`, y la corrida de control
reproduce **0.1250 exacto** (15 hits en 12 preguntas, cero desvío).

**F6 — el DDL del índice fulltext era Cypher inválido, y ningún gate podía verlo.** El análisis del
incidente, porque el defecto es de método y no de una línea:

1. **La causa**: `chunk_text_index_ddl` emitía `OPTIONS {indexConfig: {'fulltext.analyzer': …}}`.
   En Cypher las claves de un mapa son identificadores o backtick-escaped; una clave punteada entre
   comillas simples es un **error de parseo**. La evidencia de producción es el mensaje del
   operador, no mi razonamiento.
2. **El patrón correcto ya estaba en el repo**: `_neo4j_vector_cypher._ENSURE_VECTOR_INDEX` usa
   `` `vector.dimensions` `` desde siempre. Convivían las dos formas y nada las distinguía.
3. **Por qué los gates no lo vieron**: los pines comparaban el DDL **consigo mismo**. Un statement
   sintácticamente inválido es byte-idéntico a su propia copia, así que el pin verbatim, el pin de
   paridad script↔adapter recién instalado en #54 y la suite entera pasaban en verde. **La paridad
   no es corrección**: comparar dos copias de un string equivocado mide la consistencia del error.
4. **Por qué el failure path sí sirvió**: el `--apply` falló en el `CREATE` tras el `DROP`, el
   script reportó el estado y el operador restauró el índice a mano con la forma correcta. La ventana
   declarada en F1 y la recuperación de F2 no evitaron el fallo; evitaron que fuera silencioso.
5. **Latente en producción**: `ensure_indexes` emitía el mismo statement, así que el próximo
   `--fresh` de indexación habría fallado igual —con el agravante de que un índice ya existente no
   protege de un error de parseo, porque el parseo ocurre antes de que `IF NOT EXISTS` decida nada.

**Corrección (T9).** Una fuente (`chunk_text_index_ddl`) pasa a emitir backticks; los tres pines de
bytes registran el statement corregido, que es el que producción **aceptó**. Y se agrega el guard que
faltaba, `tests/unit/test_cypher_map_keys.py`: una propiedad general —ningún DDL emitido por el repo
usa una clave de mapa entre comillas simples— más el pin del key exacto que falló. El guard nació
**ROJO** sobre el DDL del chunk (3 fallos) y **verde** sobre el vectorial, que es la evidencia de que
discrimina en vez de prohibir todo.

**Lección para el archivo**: un pin de bytes fija lo que el código **dice**, no lo que el sistema
**acepta**. Cuando el artefacto es un lenguaje que el repo no puede parsear, el único pin honesto es
una propiedad sintáctica, y la única validación real es el servidor. Este DDL estuvo verde tres PRs
seguidos sin haber visto nunca un Neo4j.

**L5** (2026-10-20, operator, verbatim): "Rollback verificado en hardware real (Neo4j 5.23 sobre Orange
Pi) — chunk_text_index recreado con analyzer 'standard-no-stop-words' (ONLINE al 100%, 3468 chunks).
Corrida de control post-rollback ejecutada: precision@10 = 0.1250 (15 hits sobre 120 slots de
evaluación), reproduciendo exactamente el baseline commiteado. Cero drift." Y sobre las variantes:
"Slot 1 (Forma - Frase escapada): Colapso a 0.0000 (-15 referencias netas). Descartado. Slot 2
(Analyzer - standard): Subió a 0.1333 (16/120 hits, +1 ganado en ret-014, 0 perdidos). Monotonía
estricta verificada, pero objetivo 0.1500 (18 hits) no alcanzado. Descartado para proteger el canario
de drift, evitar divergencia de configuración (snowflake) y mantener el baseline commiteado."

**Cierre de la fase (Option C).** Ambas variantes pre-registradas consumidas, ninguna alcanza `0.150`.
La variante 2 satisfizo la barra antirruido de S2 y falló el objetivo; adoptarla habría dejado el
objetivo unmet bajo otro nombre a cambio de un canario de drift muerto (la fila de control declara
`RUN VOID` en cada corrida, porque el baseline se midió con `standard-no-stop-words`), una divergencia
entre `DECLARED_CHUNK_ANALYZER` y el servidor —el snowflake que S5 eliminó— y una segunda ventana de
DDL. Rollback medido: `0.1250`, 15/120, cero drift. **Acta en `docs/spec/09-retrieval-pool-recall.md`
§11 y en `odd/backlog.md`.** Sin tercer intento.

**Lo que la fase deja medido**: el analyzer mueve una referencia de 120 y los tres cross-encoders
quedaron por debajo del orden fulltext puro ⇒ el cuello de botella está en el **recall del pool**, no
en el ranking ni en el análisis del texto. El único camino no construido sigue siendo la alternativa A
(híbrido vectorial nativo), con el resultado de fase 0 en contra y `RERANK_ENABLED` en `false`.

**Estado de tareas**: T1-T10 cerradas. T1-T8 se entregaron en el PR #53, squash-mergeado como
`98bf449` (los commits de la rama no sobreviven al squash, así que se cita el merge). T9 en el PR #55,
squash-mergeado como `3d23653`; la interfaz de recreación endurecida de la que dependía, en el PR #54
(`ef6ab7b`). T10 es la corrida del operador: no tiene commit, su evidencia es el acta de spec 09 §11.
