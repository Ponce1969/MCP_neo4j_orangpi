# Tool intent router — mode / scope / intent over the MCP

Feature: an orchestration layer **above** the MCP that decides evidence vs synthesis, keeps scope
explicit, accepts an optional intent, and turns graph abstention into text fallback or top-2 fan-out.
The MCP surface, its scope gate and its logging are untouched; the namespace router (already
delivered, `odd/tasks/namespace-router-call-site.md`) is its scope sub-router.

## Specs

### Regla 1 — Modo / Alcance / Intención

- **S1** — Modo: evidencia (search_rag, $0) vs síntesis (ask_global, caro). Default: evidencia. La
  síntesis es opt-in explícito del llamador.
- **S2** — Alcance: los 4 libros por defecto. Acotar es decisión explícita del llamador, nunca
  inferencia del router.
- **S3** — Intención: el agente externo declara la intención como parámetro opcional (intent).
  Valores: `fundamento` | `diseño` | `implementacion`. La intención ajusta k, si se recorren
  relaciones, y el estilo de cita. No decide de qué libro leer.
- **S4** — Regla negativa: el router no filtra por libro. El libro lo elige el ranking de la
  evidencia (o el namespace router, en su defecto).

### Regla 2 — Fallback (cuando el grafo no cubre)

- **S5** — Si la pregunta requiere cobertura total o citas textuales, siempre se piden pasajes
  (BTRAG), independientemente de lo que diga el grafo.
- **S6** — Si el grafo no encuentra la entidad, el orquestador no debe inventar. Debe devolver un
  estado explícito de "cobertura insuficiente" y derivar la consulta al texto.
- **S7** — El fallback no es un reintento ciego. Es una decisión basada en la evidencia: si el grafo
  devuelve abstain o low_score, se va al texto.
- **S8** — Dado el hallazgo de los márgenes chicos: cuando el namespace router devuelve abstain por
  low_margin, el fallback no es un error. Es la señal para hacer fan-out a los dos namespaces top-2
  + merge, no para rendirse.

### Regla 3 — Clave de caché y cuándo no cachear

- **S9** — Clave de caché: se reutiliza la `route_cache_key` existente (sha256 de 6 dimensiones) y se
  le agrega el modo (evidencia/síntesis): pregunta_normalizada + namespace/scope + modo + model_id +
  profile_version + catalog_version + graph_snapshot. El `graph_version` se corresponde con el
  `graph_snapshot` de la clave.
- **S10** — Cuándo NO cachear: respuestas del grafo construidas sobre entidades incompletas (si hubo
  fallback a texto, no se cachea como respuesta del grafo).
- **S11** — Cuándo NO cachear: respuestas de síntesis (`ask_global`) que no tengan procedencia clara.
- **S12** — Invalidación: si cambia `graph_version`, cambia la clave. La caché nunca sirve respuestas
  de un grafo anterior.
- **S13** — Valores del ajuste por intención (tabla del maintainer, verbatim):
  `foundation` → k = **3**, ¿recorre relaciones? **No**, cita **literal (chunk_id)**;
  `design` → k = **5**, ¿recorre relaciones? **Sí** (grafo + pasajes), cita **concepto + referencia**;
  `implementation` → k = **4**, ¿recorre relaciones? **No (solo código)**, cita **con código + chunk_id**.

## Tasks

| ID | Spec | Route | Commit |
|----|------|-------|--------|
| T1 | S1, S2, S3, S4 | done — contract only: `Mode`, `Intent`, frozen `OrchestrationRequest` (question verbatim, `mode` defaulting to evidence, explicit `scope` or `None` = all books) and a fail-closed `parse_orchestration_request`; `extra="forbid"` makes S4 structural | — (uncommitted) |
| T6 | S3, S13 | done — `IntentTuning` + `tuning_for`; `intent=None` applies no tuning (S13 defines no row for it) | — |
| T2 | S6, S7, S8 | in progress — `GraphEvidencePort` + `TextPassagePort` and one outcome with three explicit states (`covered` / `fanout` / `insufficient_coverage`) | — |
| T3 | S5 | pending — always fetch passages when textual coverage or citations are required | — |
| T4 | S9 | done — `route_cache_key` gained a required keyword-only `mode`; seven dimensions, one test per dimension; blank mode rejected, never normalized | — (uncommitted) |
| T5 | S10, S11, S12 | pending — cache write policy: never store fallback-derived graph answers or unprovenanced synthesis; key rotates with `graph_version` | — |

## Log

- **L1** (maintainer, verbatim — Reglas 1, 2 y 3, "ya cerradas y consensuadas"):

  **REGLA 1 — Modo / Alcance / Intención**
  Modo: evidencia (search_rag, $0) vs síntesis (ask_global, caro). Default: evidencia. La síntesis es
  opt-in explícito del llamador. Alcance: los 4 libros por defecto. Acotar es decisión explícita del
  llamador, nunca inferencia del router. Intención: el agente externo declara la intención como
  parámetro opcional (intent). Valores: fundamento | diseño | implementacion. La intención ajusta k,
  si se recorren relaciones, y el estilo de cita. No decide de qué libro leer. Regla negativa: el
  router no filtra por libro. El libro lo elige el ranking de la evidencia (o el namespace router, en
  su defecto).

  **REGLA 2 — Fallback (Cuando el grafo no cubre)**
  Si la pregunta requiere cobertura total o citas textuales, siempre se piden pasajes (BTRAG),
  independientemente de lo que diga el grafo. Si el grafo no encuentra la entidad, el orquestador no
  debe inventar. Debe devolver un estado explícito de "cobertura insuficiente" y derivar la consulta
  al texto. El fallback no es un reintento ciego. Es una decisión basada en la evidencia: si el grafo
  devuelve abstain o low_score, se va al texto. Dado el hallazgo de los márgenes chicos: cuando el
  namespace router devuelve abstain por low_margin, el fallback no es un error. Es la señal para
  hacer fan-out a los dos namespaces top-2 + merge, no para rendirse.

  **REGLA 3 — Clave de Caché y Cuándo No Cachear**
  Clave de Caché: se reutiliza la route_cache_key existente (sha256 de 6 dimensiones) y se le agrega
  el modo (evidencia/síntesis). Queda así: pregunta_normalizada + namespace/scope + modo + model_id +
  profile_version + catalog_version + graph_snapshot (Nota: el graph_version se corresponde con el
  graph_snapshot de tu clave). Cuándo NO cachear: respuestas del grafo construidas sobre entidades
  incompletas (si hubo fallback a texto, no se cachea como respuesta del grafo); respuestas de
  síntesis (ask_global) que no tengan procedencia clara. Invalidación: si cambia graph_version,
  cambia la clave. La caché nunca sirve respuestas de un grafo anterior.

  Y la consecuencia que el maintainer sacó del smoke: *"si el router se abstiene casi siempre,
  entonces la Regla 2 (Fallback) no es una opción, es el camino normal. Eso cambia el diseño: el
  fallback tiene que convertir el abstain en fan-out a los dos namespaces + merge, no en un error."*

- **L2** (evidencia medida, 2026-11-04, artefacto real del OrangePi): 5 preguntas por
  `book-graph-rag route`, **5/5 abstain por `low_margin`**, con márgenes reales 0.0049 / 0.0429 /
  0.0242 / 0.0067 / 0.0063 frente a `min_margin=0.05`. El libro correcto quedó primero en 3 de 4
  preguntas temáticas; la pregunta fuera de dominio quedó en 0.1009, sobre el piso `min_top_score=0.10`.
  Conclusión que origina S8: con los umbrales vigentes el router de alcance **casi nunca se
  compromete**, y el fan-out top-2 es el camino normal, no la excepción.
- **L3** (decision): T4 (S9) toca `route_cache_key`, un contrato ya consumido por telemetría y tests.
  Se hace como unidad propia, con un test por dimensión, y nunca reutilizando el nombre viejo con
  semántica nueva.
- **L4** (T4 delivered, 2026-11-05): `route_cache_key` is now seven dimensions; `mode` is a required
  keyword-only `str`, stored verbatim and rejected with `ValueError` when blank (`""` or `"   "`),
  never `.strip()`-normalized. No production caller existed (`grep route_cache_key src/ scripts/`
  finds only the definition), so nothing in the running system changed shape. 11 tests in
  `tests/unit/test_routing_domain.py`: determinism (64 lowercase hex), one per dimension, two blank
  rejections, verbatim storage; the telemetry test call site passes `mode="evidence"` without any
  assertion being weakened. Gates: 50 tests, ruff, mypy (493 files), `validate_architecture.py`.
  Deliberate deferral: `mode` is a plain `str` with no enum, because the request contract that fixes
  the vocabulary is T1, and `RoutingEvent` does not carry `mode` yet — that belongs to the telemetry
  wiring spec, not to S9.
- **L5** (T1 delivered, 2026-11-05; maintainer chose the delivery form: *"Módulo importable en el
  repo"*, no HTTP client, no CLI, no MCP call). Files: `src/book_graph_rag/domain/orchestration_models.py`,
  `src/book_graph_rag/application/orchestration/{__init__.py,parse_request.py}`,
  `tests/unit/test_orchestration_request.py` (20 tests). Contract: `question` stored verbatim but
  rejected when blank; `mode` defaults to `evidence` (S1, synthesis is opt-in); `intent` optional
  (S3); `scope=None` means all books, `scope=[]` rejected pointing at omission, duplicates rejected
  (S2); `extra="forbid"` plus a field set with no single-book key, and no validator deriving `scope`
  from `intent`, make S4 structural rather than a promise. Decisions taken where the spec was silent,
  all reversible because nothing consumes them yet: (a) wire tokens are **English** (`evidence`,
  `synthesis`, `foundation`, `design`, `implementation`) per the repo's English-artifact convention,
  with the Spanish mapping documented in each enum docstring; (b) `StrEnum` over `Literal`, matching
  11 existing domain enums; (c) scope wire shape is a list of `{corpus, source}` objects.
- **L6** (open requirement, not a defect): S3's second half — *"la intención ajusta k, si se recorren
  relaciones, y el estilo de cita"* — names what it tunes but not the values. T6 is created and left
  **blocked** on the maintainer's table: no k, traversal depth or citation style has been invented.
  T2, T3 and T5 do not depend on it.
- **L7** (maintainer, verbatim — valores de T6, 2026-11-05): *"Acá van las tablas que faltaban. Si ves
  algo raro, decime, pero estos son los valores que quiero:*
  *foundation · k = 3 · ¿Recorre relaciones? No · Estilo de cita: Cita literal (chunk_id)*
  *design · k = 5 · ¿Recorre relaciones? Sí (grafo + pasajes) · Estilo de cita: Concepto + referencia*
  *implementation · k = 4 · ¿Recorre relaciones? No (solo código) · Estilo de cita: Cita con código + chunk_id"*
  Registrado como S13. `intent=None` no recibe tuning: se devuelve `None` y el consumidor conserva
  sus propios defaults, porque la tabla no define un valor para "sin intención" y inventarlo no
  corresponde. Nota abierta para el maintainer: *"solo código"* se implementa como **estilo de cita**,
  no como filtro de fuente, porque el corpus es de prosa arquitectónica y no hay un conjunto de
  chunks de código contra el que filtrar; si la intención fuera un filtro, hoy devolvería vacío.
- **L8** (T6 delivered, 2026-11-05): `domain/intent_tuning.py` + `tests/unit/test_intent_tuning.py`
  (10 tests). S13 is a module-level `INTENT_TUNING_TABLE` so the values live once;
  `tuning_for(None) is None` because S13 defines no row for "no intent" and inventing one would be
  ours, not the maintainer's. **Four things the maintainer should look at** (reported, not silently
  fixed): (a) the design row says *"Sí (grafo + pasajes)"* but passages are not relationship
  traversal — one boolean cannot carry both, and the passage half belongs to S5/T3; (b) the
  implementation row says *"No (solo código)"*, which in a traversal column is incoherent, and as a
  source filter it would return nothing (the corpus is architectural prose, there is no code-chunk
  set) — encoded as the citation style `code_with_chunk` only; (c) S3 writes the intent values in
  Spanish while S13 uses English, already reconciled by L5's wire-token rule; (d) the k values
  3/5/4 carry no stated rationale — recorded verbatim as given.
- **L9** (T2 delivered, 2026-11-05): `domain/orchestration_outcomes.py`,
  `application/orchestration/{ports.py,fallback.py}`, `tests/unit/test_fallback_orchestration.py`
  (7 async tests). One outcome, three states. Decisions where S6-S8 were silent, all reversible:
  the `reason` always starts with the router reason so any consumer parses it the same way; the
  citation-id merge keeps first-seen scope order, strips already-seen ids and keeps citation-less
  items; a fan-out where **every** scope returns `None` derives to text (S8's *"not on its own"*
  governs the abstention signal, not a total absence of evidence); a `clear` route whose namespace
  was rejected by the catalog takes the S7 path instead of raising, because the graph is never
  queried with an unvalidated namespace (S4 posture). No infrastructure adapter implements the two
  ports yet: they are contracts, and wiring them is a later unit.
- **L10** (maintainer ruling, verbatim — the S7 passage budget, 2026-11-05): *"Me parece correcto que
  se tome el k de la tabla (foundation → 3) y no el default de 5. La lógica es: si el usuario pidió un
  fundamento, quiere una respuesta concisa. Si el grafo falla y cae al texto, no tiene sentido
  castigarlo con 5 pasajes y llenarlo de ruido. Mantenelo así."* Requirement added to the code as an
  explicit `INTENTIONAL, not a bug` comment in `application/orchestration/fallback.py`, next to the
  `limit` resolution, so the next reader does not "fix" it back to `passage_limit`.
- **L11** (T3 and T5 delivered, 2026-11-05): T3 (S5) adds the caller-declared `requires_passages` to
  `OrchestrationRequest` and fetches passages in every outcome when it is set — the state never
  changes, so there is no fourth state; passages ride alongside the evidence. The maintainer's ruling
  on S13's ambiguity is now executable: `intent=design` alone does **not** imply passages
  (`test_design_intent_alone_does_not_imply_passages`). T5 (S10-S12) is `domain/cache_policy.py`:
  `should_cache(outcome, mode) -> CacheDecision(cacheable, reason)`, decided on `state` and never on
  `passages` being non-empty — that distinction is the whole point, since caller-demanded passages on
  a covered outcome are not "entidades incompletas". S11 is synthesis-scoped and deliberately not
  widened to evidence mode. S12 is not reimplemented: `route_cache_key`'s `graph_snapshot` dimension
  already rotates the key. The real cache lives in `agentic-memory`, so this repo owns the contract
  and that repo owns enforcement.
- **L12** (verification, 2026-11-05): the integrated suite on the committed tree is **2513 passed,
  4 skipped, 107 deselected in 286.83s** — exactly the projection (2493 + 11 from T3 + 9 from T5).
  Three review units are committed on `feat/tool-intent-router`: `dea93d7` (router call site, margin
  fix, cache-key mode), `ccac78a` (request contract, intent tuning, fallback), `d309df2` (passages on
  demand, cache policy). Working tree clean.

## Next unit — T7 (composition), not yet specified

The ports are contracts with no adapter, so the layer cannot answer a real question yet. T7 is the
use case that composes what exists: `OrchestrationRequest` → namespace route (`RoutingCaller`) →
port per `Mode` (S1: evidence ⇒ `search_rag`; synthesis ⇒ `ask_global`) → `resolve_with_fallback`
(S6-S8) → `should_cache` (S10-S12) → one result.

Derived from the specs, no decision needed:
- evidence mode routes through `GraphEvidencePort` and never silently upgrades to synthesis;
- the namespace route is a proposal, so an unvalidated namespace never reaches a port (S4 posture);
- the cache decision is computed and reported, never written by this repo (T5 boundary).

**Open decision (maintainer):** S8 governs a namespace-router abstention in *evidence* mode. What
happens in *synthesis* mode when the router abstains with `low_margin`? Three honest options:
(a) same fan-out — `ask_global` per top-2 namespace and merge the summaries, accepting two expensive
calls; (b) synthesis needs a committed namespace, so an abstention degrades to evidence mode with
passages; (c) no synthesis without scope — return `insufficient_coverage` and let the caller decide.
Not implemented either way: this is a product decision about spending money, not a technical detail.
- **L13** (maintainer ruling, verbatim — T7 synthesis abstention, 2026-11-05): *"Vamos con la opción
  (c): no hay síntesis sin alcance. Devolvemos insufficient_coverage y decide el llamador. Razón: si
  el router no está seguro de a qué libro ir, hacer una síntesis a ciegas es una invitación a la
  alucinación carísima. Prefiero que el sistema sea honesto y le diga al agente externo: 'No tengo
  cobertura suficiente para hacer un resumen confiable'. El agente externo (Claude Code) sabrá si le
  avisa al usuario o si prefiere pedir evidencia (que es barata y segura)."*
  Consequence for T7: `mode=synthesis` requires a **committed, catalog-validated namespace**. An
  abstention — `low_margin` included — never fans out to two `ask_global` calls and never degrades to
  evidence on its own: it returns `insufficient_coverage`. Authorized scope: T7 + the real adapters
  for `search_rag` and BTRAG.
- **L14** (T7 specified, 2026-11-05): composition use case + the two in-repo adapters, derived from the
  map at `mcp_server_adapter.py`, `global_query_use_case.py`, `ports/graph_query_port.py` and
  `mcp_server_main.py`. Decisions written down so they are not re-litigated:
  - **Scope precedence** (S2 + S4): an explicit `request.scope` wins and the namespace router is not
    consulted at all; otherwise the router proposes (single or the top-2 fan-out); an abstention with
    no fan-out means the caller gets `insufficient_coverage`, never "all books guessed at once".
    "Los 4 libros por defecto" is the caller's `scope=None` meaning, not licence for the router to
    pick all four after abstaining.
  - **Synthesis requires a committed scope** (L13): no validated namespace ⇒ `insufficient_coverage`
    before any port is touched, so no `ask_global` is ever spent on a guess.
  - **Evidence adapter** reuses `GraphQueryPort.search_chunks(query, limit, scope=...)` — the same port
    `search_rag` uses — and maps each chunk to provenance via `chunk_id`, with the namespace recovered
    with `SourceNamespace.parse_book_id`. Reaching into `McpServerAdapter` was rejected: that class is
    the MCP surface (16 construction parameters) and the whole point of T7 is to sit above it.
  - **Synthesis adapter** reuses `GlobalQueryUseCase.ask(question, detail_level, scope=...)`; its
    `citations` are community-summary ids, which is what `GraphEvidence.citations` carries.
  - **BTRAG has no wiring in this repo**: no client, no `httpx` dependency, no config field, and the
    service is not on the Orange Pi. `TextPassagePort` therefore stays contract-only until the
    maintainer supplies the endpoint and the auth shape; inventing a transport for an external service
    would be guesswork with a network round-trip attached.
