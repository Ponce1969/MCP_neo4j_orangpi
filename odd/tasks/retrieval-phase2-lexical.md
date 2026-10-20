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
