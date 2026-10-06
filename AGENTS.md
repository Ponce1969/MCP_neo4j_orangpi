# AGENTS.md - Directivas de Desarrollo para IA

## 1. Filosofía y Herramientas
- **Gestor de Paquetes:** Usamos EXCLUSIVAMENTE `uv`. PROHIBIDO usar `pip`, `poetry` o `pipenv`.
- **Tipado y Linting:** `mypy` en modo `strict` y `ruff` con reglas estrictas. No se considera una tarea terminada si `uv run ruff check .` o `uv run mypy .` fallan.
- **Pre-commit:** Todo commit debe pasar por los hooks de pre-commit.

## 2. Arquitectura Hexagonal (Puertos y Adaptadores)
- **Dominio (`domain/`):** Solo modelos de Pydantic y lógica de negocio pura. PROHIBIDO importar librerías externas (no `neo4j`, no `openai`, no `fitz`).
- **Puertos (`ports/`):** Interfaces abstractas (`abc.ABC`). Definen el "qué" se hace, no el "cómo".
- **Infraestructura (`infrastructure/`):** Implementaciones concretas de los puertos. Aquí viven las librerías externas.
- **Aplicación (`application/`):** Casos de uso. Solo dependen de los Puertos, NUNCA de la Infraestructura directamente.

## 3. Configuración y Secretos
- PROHIBIDO hardcodear URLs, API keys, usuarios o contraseñas.
- Toda configuración debe venir de `pydantic_settings.BaseSettings`.
- Las contraseñas y keys DEBEN usar `pydantic.SecretStr`.
- Si falta una variable en `.env`, la app debe fallar en el arranque (Fail-Fast).

## 4. Flujo de Trabajo SDD
1. Lee el `Spec.md` correspondiente en `docs/spec/` (normativo; histórico por fase en `docs/spec/archive/`).
2. Si necesitas crear un script de ayuda, ponlo en `scripts/`.
3. Escribe el código en `src/`.
4. Ejecuta los validadores (`uv run ruff check .`, `uv run mypy .`, `uv run python scripts/validate_architecture.py`).
5. Solo cuando todo pase, considera la tarea completada.

## 5. Gates de calidad (antes de entregar)
Antes de marcar una tarea como lista, los TRES deben pasar:
```bash
uv run ruff check .
uv run mypy .
uv run python scripts/validate_architecture.py
```
    Si alguno falla, no es done.

## 6. Política de exposición vía MCP (`agentic-patterns`)

Este proyecto (cuando la fase 07 del MCP server esté lista) se consume vía el
MCP **`agentic-patterns`** (remote SSE, hosteado en el OrangePi via Tailscale).

**El criterio de uso es OPT-IN, no por defecto.**

- **Estado por defecto: `enabled: false`** en el runtime del agente
  (`~/.config/opencode/opencode.json` -> `mcp.agentic-patterns.enabled`).
- **Se habilita on-demand**, sólo durante sesiones de **diseño de arquitectura
  multi-agente nueva** o casos específicos donde se necesita resolver patrones
  del libro contra el grafo Neo4j.
- **NO se porta a pi.** Pi queda acotado a los MCPs de trabajo diario
  (`oranpi` infra, `context7` docs). Este MCP vive únicamente en opencode para
  que el ruido en el system prompt de pi sea mínimo.
- **Menos es más:** un MCP habilitado inyecta TODAS sus tool descriptions en el
  system prompt en cada turno, gasta tokens, y distrae al modelo. No exponer lo
  que no aporta al 95 % de las sesiones es una decisión de ingeniería, no un
  capricho.

### Regla para cualquier agente que asista a este repo
- **NUNCA** auto-habilitar `agentic-patterns` sin confirmación explícita del humano.
- Si una tarea no requiere resolver patrones del libro contra el grafo, dejá el
  MCP apagado y usá las tools generales del runtime.
- Si una tarea SÍ lo requiere (ej. "diseñá una arquitectura multi-agente usando
  patrones del libro"), el humano debe confirmar el `enabled: true` antes de
  empezar; el agente no lo prende solo.
- Este criterio aplica también tras la fase 07: el MCP server se expone, pero
  el consumo es opt-in por sesión, no permanente.

## 7. REGLA DE ORO: Servidor OrangePi en PRODUCCIÓN (obligatorio)

Este proyecto corre en el OrangePi `100.106.85.109` (Tailscale; no hay puertos
abiertos). SSH directo: `ssh -i ~/.ssh/id_ed25519 -o BatchMode=yes gonzalo@100.106.85.109`

**En ese servidor hay 23 contenedores Docker en producción, todos de proyectos
distintos (incluyendo Postgres de otros proyectos). NO tocar nada que no sea
del proyecto `bookgraph` (contenedor `bookgraph-neo4j`).**

- **NUNCA borrar nada sin consentimiento explícito del humano:** no `docker
  prune`, no `docker rmi`, no `docker volume rm`, no `DROP DATABASE`, no borrar
  archivos arbitrarios. Preguntar SIEMPRE antes de cualquier borrado.
- **NUNCA** reiniciar, parar o reconstruir contenedores de otros proyectos.
- Operaciones sobre el grafo del proyecto: solo vía `scripts/run_full_pipeline.py`
  (que hace auto-backup a `~/backups_neo4j/` antes de un `--fresh`) o comandos
  explícitos aprobados por el humano.
- Copiar esta regla a cualquier subagente que vaya a tocar el servidor.

### 7.1 Flags administrativos de Phase 2 (resumable indexing)

Los comandos `--replay-dead-letter` y `--backfill-checkpoints` mutan checkpoints
en el grafo; aplican las mismas reglas de aprobación que cualquier operación
destructiva:

- `--backfill-checkpoints --apply` requiere `--approval <archivo>` cuyo
  contenido incluya la palabra `approve`.
- `--replay-dead-letter` y `--force-reprocess` pueden sobrescribir checkpoints
  `PROCESSED`; usar solo con `--source-id corpus:source` explícito y previa
  validación.
- Para `--backfill-checkpoints` siempre correr `--dry-run` antes de `--apply`.
### 7.2 Operaciones de Phase 3 (semantic entity resolution)

- Aplicar merges de resolución (`ResolveEntitiesUseCase` con apply, o
  `RESOLUTION_STRATEGY=hybrid` en el pipeline) MUTA el grafo de entidades: requiere el
  mismo gate de aprobación que cualquier operación destructiva (backup → dry-run →
  aprobación humana explícita).
- Los duplicados se marcan `merged_into` (soft-delete), nunca `DETACH DELETE`.
- El ledger `data/resolution/merge_ledger.jsonl` es append-only y tamper-evident; el
  rollback agrega entradas compensatorias, nunca edita historial.
- La cuarentena (`data/resolution/quarantine.jsonl`) y el ledger viven en la máquina que
  ejecuta el merge; respaldarlos junto con el grafo.
- **Lotes de merge intra-namespace (T10, `scripts-ops/resolve_intra_ns.py`)**: el plan se
  deriva de la regla del audit con la **misma expresión de agrupamiento importada** (nunca
  una copia, para que no quede ciego si la regla cambia), el canónico se elige por
  **riqueza** (menciones → grado → id más corto → lexicográfico) y `--apply` exige backup
  fresco + archivo con la palabra `approve` + `--expect-fingerprint` (sha256 del plan
  revisado, validado antes de la primera escritura). El censo antes/después **simula el
  orden del plan** para predecir las aristas que el re-point colapsa o borra, y sale ≠0
  ante cualquier drift. Aplicado 2026-10-05: 64 grupos, 0 fallos, R5b 64 → 0.

### 7.3 Phase 4 — Scoped audit + readiness gates

- `book-graph-rag audit` y `book-graph-rag gate` son **read-only**: no mutan el grafo.
- La política de gates vive en `gates.yaml` (raíz del repo, sobreescribible vía
  `GATES_POLICY_PATH`). El gate `expose-mcp` es **provisional y audit-only**;
  el smoke de retrieval se decide en Phase 5/6.
- Los gates usan los mismos códigos de salida que el audit: 0 pass, 10 violaciones,
  11 incomplete, 12 unreachable, 13 failed.

### 7.4 Phase 5 — Evaluación y readiness gates

- `book-graph-rag evaluate` y `book-graph-rag gate` para readiness gates son
  **read-only**: no mutan el grafo. Solo producen reportes (`data/evaluation/`)
  y códigos de salida.
- En el OrangePi usar `~/.local/bin/uv` (uv instalado localmente para el usuario
  `gonzalo`) y luego `uv sync --extra community` para sincronizar las dependencias
  opcionales de comunidad requeridas por el readiness gate.
- Los umbrales son **mechanism-first**: los archivos `data/evaluation/*_baseline.json`
  llevan `thresholds_finalized: false` hasta una delta posterior de Phase 5 que fije
  los valores numéricos. Mientras tanto, el readiness gate reporta `INCOMPLETE` (11)
  aunque las métricas medidas pasen las comprobaciones relativas al baseline.

### 7.5 MCP en producción — contrato de operación

- El servicio MCP es la unidad systemd **`mcp-server.service`**. El nombre
  `book-graph-rag-mcp` es el console script, NO la unidad: `systemctl status
  book-graph-rag-mcp` responde "could not be found". Runbook completo:
  `docs/ops/mcp-service.md`.
- Reiniciar: `sudo systemctl restart mcp-server` (sudo pide password en ese host;
  `status`/`is-active`/`cat` no requieren sudo). Verificar: sourcear `.env` y correr
  `set -a; . ./.env; set +a; uv run --no-sync python scripts-ops/mcp_smoke_book4.py`;
  un `401` significa que la auth funciona y no exportaste `MCP_ACCESS_TOKEN`
  (nunca imprimir el token).
- **NUNCA** lanzar una instancia manual en background: mientras otro proceso sostiene
  `100.106.85.109:8003`, cada reintento del unit falla al bindear y entra en loop de
  restarts cada 5 s (incidente histórico de 43,558 reinicios). No confíes en `deploy/mcp-server.service`
  como descripción del unit vivo: ver §1 del runbook (`systemctl cat mcp-server`).
- Reinicio **obligatorio** tras editar `catalog.yaml`: `CatalogScopeResolver` cachea
  el catálogo por la vida del proceso.

### 7.6 Bindings de red — nunca `0.0.0.0`

- **Neo4j (`bookgraph-neo4j`)**: los puertos `7474`/`7687` se publican SOLO en una
  interfaz explícita. El host IP sale de `NEO4J_BIND_HOST` en `docker-compose.yml`
  (default `127.0.0.1` en dev; en el OrangePi el `.env` fija la IP Tailscale
  `100.106.85.109`). Si la IP no existe al arrancar, docker rechaza el bind:
  fail-closed, nunca cae al wildcard.
- **MCP SSE (`mcp-server.service`)**: ya usa `Environment=MCP_BIND_HOST=100.106.85.109`
  (R7). Ver §7.5 y `docs/ops/mcp-service.md`.
- Regla general: ningún servicio de este proyecto publica puertos en `0.0.0.0` ni
  `[::]`; el camino de consumo remoto es el MCP sobre Tailscale, no el puerto crudo.
- Guard: `tests/test_deploy_artifacts.py` falla si el compose vuelve a publicar
  puertos sin host IP explícito.
- Cualquier recreación de `bookgraph-neo4j` (`docker compose up -d`) o edición del
  `.env` del host exige aprobación humana explícita (regla de oro de §7).
