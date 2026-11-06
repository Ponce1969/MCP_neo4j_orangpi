# Namespace router — first production call site

Feature: give the already-built namespace router a reachable call site outside the MCP.
Audited state: router COMPLETED and archived (`openspec/changes/namespace-question-routing`,
2026-09-29), `router_enabled=false`, `build_routing_caller` called only from tests.
Audit evidence: Engram obs 1807 (`architecture/namespace-router-state`).

## Specs

- **S1** — The maintainer's decision, verbatim: *"Sí, conectamos el namespace router. La deuda
  técnica del README ya la nombró. Si es barato (solo falta el call site y el archivo de
  perfiles), lo hacemos. Es un paso necesario para que el nuevo Router de herramientas tenga un
  'sub-routeador' que le diga de dónde leer."*
- **S2** — Verbatim: *"El Router de herramientas va AFUERA del MCP. Va como una capa de
  orquestación por encima. No tocamos mcp_server_adapter.py todavía. La seguridad del MCP se
  mantiene intacta."*
- **S3** — Verbatim: *"La Regla 3 REUTILIZA route_cache_key (6 dimensiones). No inventemos la
  rueda. Esa clave ya está diseñada y probada. Lo único que vamos a hacer es cablearla al nuevo
  flujo y asegurarnos de que incluya lo necesario para la síntesis (como el modo evidencia/síntesis
  que agregamos en la Regla 1)."*
- **S4** — Verbatim: *"Sobre el archivo faltante: Sí, por favor, chequeá por el MCP de la
  OrangePi si existe data/router/namespace_profiles.json. Si existe: lo usamos. Si no existe: lo
  generamos con el script y lo dejamos listo."*

## Tasks

| ID | Spec | Route | Outcome |
|----|------|-------|---------|
| T1 | S1, S2 | inline | `book-graph-rag route QUESTION` read-only CLI command over `build_routing_caller`; no MCP change |
| T2 | S4 | inline | Artifact availability: found on the Orange Pi (44,355 B); local generation path recorded |
| T3 | S3 | — | Extend `route_cache_key` with the evidence/synthesis mode; needs the Regla 1 + Regla 3 text |
| T4 | S1, S2 | inline | `_abstain` reports the real margin; the abstention's distance to a commit is measurable |

## Log

- **L1** (maintainer, verbatim): *"Sí, conectamos el namespace router. La deuda técnica del README
  ya la nombró. Si es barato (solo falta el call site y el archivo de perfiles), lo hacemos. Es un
  paso necesario para que el nuevo Router de herramientas tenga un 'sub-routeador' que le diga de
  dónde leer."* … *"El Router de herramientas va AFUERA del MCP. Va como una capa de orquestación
  por encima. No tocamos mcp_server_adapter.py todavía. La seguridad del MCP se mantiene intacta."*
  … *"La Regla 3 REUTILIZA route_cache_key (6 dimensiones). No inventemos la rueda."* … *"Próximo
  paso: Después del chequeo del archivo, arrancamos con la implementación. Vos escribís el código
  para cablear el namespace router, y mientras tanto, yo te paso el borrador de la Regla 2
  (Fallback) y la Regla 3 (Caché) para que las conviertas en Specs."*
- **L2** (evidence, Orange Pi read-only via `mcp-oranpi`): repo root on the host is
  `/home/gonzalo/Gonzalo_codigo/Mcp_libro/MCP_neo4j_orangpi`; `data/router/namespace_profiles.json`
  **exists**, 44,355 bytes. Local checkout has no `data/router/` and no `ROUTER_*` in `.env`
  (documented in `.env.example:98-99`). Open for T2: generating locally requires a Neo4j the local
  `.env` can reach, or copying the Pi artifact when `catalog_version`/`profile_version` match.
- **L3** (decision): the call site is the shipped Click CLI (`src/book_graph_rag/main.py`), not the
  MCP adapter (S2) and not the future intent layer (which depends on Regla 1-3 text). The intent
  layer will reuse the same `build_routing_caller` seam, so T1 is not throwaway work.
- **L4** (T1 delivered): `book-graph-rag route QUESTION` added at `src/book_graph_rag/main.py`
  (`_route_payload` + the `route` command), tests in `tests/unit/test_route_command.py` (6 cases),
  contract documented in `docs/ops/namespace-routing-caller-integration.md` ("CLI call site").
  Deviation from the original brief, recorded on purpose: `build_routing_caller` is imported at
  module top like every other infrastructure adapter in that file, because `main.py` already
  imports `SentenceTransformerAdapter` eagerly, so a lazy import inside the command would buy
  nothing and break the file's one convention. Gates: ruff, mypy (493 files),
  `validate_architecture.py` and the six tests all pass.
- **L5** (T2 state): `data/*` is already git-ignored (`.gitignore:29`), so a generated artifact can
  never be committed — no ignore change is needed. Generating it *here* is not possible: the local
  `.env` carries **no** `NEO4J_*` variable at all, so `scripts/build_namespace_profiles.py` has no
the graph to read. Per S4 ("si existe: lo usamos") the artifact is the Orange Pi one; pointing the
  CLI at a local copy is an operational step, not a code change.
- **L6** (T4 delivered, maintainer-authorized: *"Sí, autorizado. Es una línea y un test"*):
  `_abstain` hardcoded `margin=0.0`, so every abstention reported zero and hid the distance to a
  commit (measured real margins in the smoke: 0.0049-0.0429). Both decision paths now share
  `_ranked_margin` (`domain/routing_models.py`). Tests: `test_low_margin_abstention_reports_the_real_margin`,
  `test_low_score_abstention_reports_the_real_margin`,
  `test_abstention_without_profiles_reports_zero_margin`. `confidence` stays `0.0` on purpose (an
  abstention has no route to be confident about). `build_routing_event` consumes `decision.margin`
  unchanged, so telemetry now records the true value; it stays opt-in and off.
- **L7** (collateral defect found by the suite, fixed): `test_build_routing_caller_flag_off_by_default`
  read the ambient `.env`, so it failed the moment `ROUTER_ENABLED=true` landed there — the test was
  not deterministic. It now asserts the declared default
  (`Settings.model_fields["router_enabled"].default is False`) and pins the flag explicitly when it
  composes the caller.
